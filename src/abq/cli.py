"""Command line interface.

The agents themselves are the main callers: they run `abq send ...` through
whatever shell tool they have. Humans mostly use `abq who` and `abq history`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import history, returns
from .adapters import ADAPTERS, Held, Unreachable, detect_current_session
from .adapters.claude import ClaudeTurnError, ManagedClaude
from .adapters.codex import Connection, agent_messages, endpoint as codex_endpoint
from .registry import Agent, Registry

BRIEF = """\
abq — messaging between the coding-agent sessions running on this machine
(Claude Code and Codex, in any combination).

You (the agent) use it through your shell tool:
  abq who                    who is reachable right now, and which one is you
  abq send <alias> "text"    write to another agent; it arrives in their session
                             on its own, they do not poll anything
  abq send @all "text"       write to everyone else
  abq history -n 20          the shared transcript

Rules:
- Incoming messages reach you between your own steps. Just answer them.
- Reply with `abq send`; your native messaging tools cannot reach these sessions.
- If the recipient is mid-task, delivery waits for their next pause. Do not resend.
- If `abq who` does not list you, run `abq join <your-alias>` first.
"""


def compose(
    text: str,
    *,
    sender: str,
    recipient: str,
    sender_is_a_session: bool,
    footer: bool = True,
) -> str:
    """Wrap a message so the receiving agent knows who is talking to it.

    Providers differ in what they show: Claude Code labels the peer, while a
    Codex turn is indistinguishable from something the human typed. Naming the
    sender in the body keeps the experience the same on both sides.
    """
    body = f"[abq] message from '{sender}':\n\n{text}"
    if not footer:
        return body
    if sender_is_a_session:
        reply = f'Reply with: `abq send {sender} "text"`.'
    else:
        # A human typing from a plain shell has no session to answer.
        reply = (
            f"'{sender}' is not a registered session, so there is nobody to "
            f"reply to — just act on this."
        )
    return f"{body}\n\n[abq] You are '{recipient}'. {reply} Others: `abq who`."


def resolve_self(registry: Registry) -> Agent | None:
    """Which registered agent is the caller, if any."""
    detected = detect_current_session()
    if detected is None:
        return None
    _, address = detected
    for key in ("session_id", "thread_id"):
        if key in address:
            found = registry.by_session(address[key])
            if found is not None:
                return found
    return None


def cmd_join(args: argparse.Namespace) -> int:
    detected = detect_current_session()
    if detected is None:
        print(
            "abq join: no agent session detected around this process.\n"
            "Run it from inside a Claude Code or Codex session, or pass --provider "
            "with the provider's own identifier.",
            file=sys.stderr,
        )
        return 2
    provider, address = detected
    agent = Agent(
        alias=args.alias,
        provider=provider,
        cwd=os.getcwd(),
        address=address,
    )
    registry = Registry()
    replaced = registry.join(agent)

    note = f", replacing '{replaced.alias}'" if replaced else ""
    print(f"abq: '{args.alias}' registered as {provider}{note}.")
    for warning in ADAPTERS[provider].warnings():
        print(f"abq: note — {warning}")
    others = [a.alias for a in registry if a.alias != args.alias]
    print(
        f"abq: others here: {', '.join(others) if others else 'nobody yet'}. "
        f'Send with: abq send <alias> "text"'
    )
    return 0


def cmd_leave(args: argparse.Namespace) -> int:
    if not Registry().leave(args.alias):
        print(f"abq: '{args.alias}' is not registered", file=sys.stderr)
        return 1
    print(f"abq: '{args.alias}' removed")
    return 0


def cmd_who(args: argparse.Namespace) -> int:
    registry = Registry()
    if not len(registry):
        print("abq: nobody registered yet — run `abq join <alias>` in each session")
        return 0
    me = resolve_self(registry)
    rows = []
    for agent in registry:
        adapter = ADAPTERS.get(agent.provider)
        if adapter is None:
            status = f"unknown provider {agent.provider!r}"
        elif adapter.is_reachable(agent):
            queued = adapter.pending(agent)
            status = "reachable" + (f", {queued} queued" if queued else "")
        else:
            status = "unreachable (session closed?)"
        label = agent.alias + (" (you)" if me and agent.alias == me.alias else "")
        rows.append((label, agent.provider, status, agent.cwd))

    width = max(len(row[0]) for row in rows)
    for label, provider, status, cwd in rows:
        print(f"{label:<{width}}  {provider:<7} {status:<28} {cwd}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    registry = Registry()
    me = resolve_self(registry)
    sender = args.sender or (me.alias if me else "operator")

    if args.to == "@all":
        targets = [a for a in registry if a.alias != sender]
        if not targets:
            print("abq: @all, but nobody else is registered", file=sys.stderr)
            return 1
    else:
        target = registry.get(args.to)
        if target is None:
            print(
                f"abq: '{args.to}' is not registered (see `abq who`)", file=sys.stderr
            )
            return 1
        targets = [target]

    delivered = 0
    for agent in targets:
        adapter = ADAPTERS.get(agent.provider)
        if adapter is None:
            print(
                f"abq: no adapter for provider {agent.provider!r}", file=sys.stderr
            )
            continue
        body = compose(
            args.text,
            sender=sender,
            recipient=agent.alias,
            sender_is_a_session=registry.get(sender) is not None,
            footer=not args.no_hint,
        )
        held = False
        try:
            msg_id = adapter.deliver(agent, body, sender=sender)
        except Held as exc:
            msg_id, held = exc.msg_id, True
        except Unreachable as exc:
            print(f"abq: {agent.alias} unreachable: {exc}", file=sys.stderr)
            continue
        history.append(
            {
                "from": sender,
                "to": agent.alias,
                "provider": agent.provider,
                "text": args.text,
                "msg_id": msg_id,
                **({"status": "held"} if held else {}),
            }
        )
        delivered += 1
        if held:
            print(
                f"abq: -> {agent.alias} reached the session but is HELD "
                f"({msg_id[:8]}): it needs one approval there before the agent "
                f"sees it."
            )
        else:
            print(f"abq: -> {agent.alias} delivered ({msg_id[:8]})")

    return 0 if delivered else 1


def cmd_history(args: argparse.Namespace) -> int:
    color = sys.stdout.isatty()
    for record in history.read(limit=args.n):
        print(history.format_line(record, color))
    if not args.follow:
        return 0
    print("\033[2m-- following, Ctrl-C to stop --\033[0m" if color else "-- following --")
    try:
        for record in history.follow():
            # Flush every line: piping `-f` into a file or another command would
            # otherwise hold the output in a block buffer and stream nothing.
            print(history.format_line(record, color), flush=True)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    print(BRIEF)
    return 0


def cmd_return_open(args: argparse.Namespace) -> int:
    try:
        print(returns.open_channel(ttl=args.ttl))
    except returns.ReturnChannelError as exc:
        print(f"abq return-open: {exc}", file=sys.stderr)
        return 2
    return 0


def cmd_return_send(args: argparse.Namespace) -> int:
    try:
        message_id = returns.send(args.token, args.text)
    except returns.ReturnChannelError as exc:
        print(f"abq return-send: {exc}", file=sys.stderr)
        return 1
    print(message_id)
    return 0


def cmd_return_wait(args: argparse.Namespace) -> int:
    try:
        record = returns.wait(args.token, timeout=args.timeout, poll=args.poll)
    except returns.ReturnPending as exc:
        print(f"abq return-wait: {exc}", file=sys.stderr)
        return 3
    except returns.ReturnChannelError as exc:
        print(f"abq return-wait: {exc}", file=sys.stderr)
        return 1
    print(record["text"])
    return 0


def cmd_return_close(args: argparse.Namespace) -> int:
    try:
        returns.close(args.token)
    except returns.ReturnChannelError as exc:
        print(f"abq return-close: {exc}", file=sys.stderr)
        return 1
    return 0


def _json_object(value: str, label: str) -> dict:
    try:
        if value == "-":
            parsed = json.load(sys.stdin)
        elif value.startswith("@"):
            with open(value[1:], encoding="utf-8") as fh:
                parsed = json.load(fh)
        else:
            parsed = json.loads(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def _text_input(value: str, label: str) -> str:
    try:
        if value == "-":
            return sys.stdin.read()
        if value.startswith("@"):
            with open(value[1:], encoding="utf-8") as fh:
                return fh.read()
    except OSError as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    return value


def _print_json(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def cmd_codex_start(args: argparse.Namespace) -> int:
    try:
        params = _json_object(args.params, "thread params")
        with Connection(args.endpoint or codex_endpoint()) as connection:
            response = connection.start_thread(params)
    except (ValueError, Unreachable) as exc:
        print(f"abq codex start: {exc}", file=sys.stderr)
        return 1
    _print_json(response)
    return 0


def cmd_codex_run(args: argparse.Namespace) -> int:
    """Own one Codex thread connection from creation through terminal result."""
    try:
        thread_params = _json_object(args.thread_params, "thread params")
        turn_params = _json_object(args.turn_params, "turn params")
        with Connection(args.endpoint or codex_endpoint()) as connection:
            started_thread = connection.start_thread(thread_params)
            thread = started_thread.get("thread") or {}
            thread_id = thread.get("id")
            if not thread_id:
                raise Unreachable("thread/start returned no thread id")
            started_turn = connection.start_turn_with_params(thread_id, turn_params)
            turn_id = (started_turn.get("turn") or {}).get("id")
            if not turn_id:
                raise Unreachable("turn/start returned no turn id")
            terminal = connection.wait_turn(thread_id, turn_id, args.timeout)
            output = _turn_result(connection, thread_id, terminal)
            output["thread_start"] = started_thread
            if args.delete_thread:
                connection.delete_thread(thread_id)
                output["thread_deleted"] = True
    except (ValueError, Unreachable) as exc:
        print(f"abq codex run: {exc}", file=sys.stderr)
        return 1
    _print_json(output)
    return 0


def _turn_result(connection: Connection, thread_id: str, turn: dict) -> dict:
    return {
        "thread_id": thread_id,
        "turn": turn,
        "agent_messages": agent_messages(turn),
        "server_requests": connection.server_requests,
    }


def cmd_codex_turn(args: argparse.Namespace) -> int:
    try:
        params = _json_object(args.params, "turn params")
        with Connection(args.endpoint or codex_endpoint()) as connection:
            response = connection.start_turn_with_params(args.thread_id, params)
            turn = response.get("turn") or {}
            turn_id = turn.get("id")
            if not turn_id:
                raise Unreachable("turn/start returned no turn id")
            if args.wait is None:
                output = {
                    "thread_id": args.thread_id,
                    "turn": turn,
                    "server_requests": connection.server_requests,
                }
            else:
                terminal = connection.wait_turn(args.thread_id, turn_id, args.wait)
                output = _turn_result(connection, args.thread_id, terminal)
    except (ValueError, Unreachable) as exc:
        print(f"abq codex turn: {exc}", file=sys.stderr)
        return 1
    _print_json(output)
    return 0


def cmd_codex_wait(args: argparse.Namespace) -> int:
    try:
        with Connection(args.endpoint or codex_endpoint()) as connection:
            turn = connection.wait_turn(args.thread_id, args.turn_id, args.timeout)
            output = _turn_result(connection, args.thread_id, turn)
    except (ValueError, Unreachable) as exc:
        print(f"abq codex wait: {exc}", file=sys.stderr)
        return 1
    _print_json(output)
    return 0


def cmd_codex_interrupt(args: argparse.Namespace) -> int:
    try:
        with Connection(args.endpoint or codex_endpoint()) as connection:
            response = connection.interrupt_turn(args.thread_id, args.turn_id)
    except Unreachable as exc:
        print(f"abq codex interrupt: {exc}", file=sys.stderr)
        return 1
    _print_json(response)
    return 0


def cmd_codex_delete(args: argparse.Namespace) -> int:
    try:
        with Connection(args.endpoint or codex_endpoint()) as connection:
            response = connection.delete_thread(args.thread_id)
    except Unreachable as exc:
        print(f"abq codex delete: {exc}", file=sys.stderr)
        return 1
    _print_json(response)
    return 0


def cmd_claude_run(args: argparse.Namespace) -> int:
    provider_args = list(args.provider_args)
    if provider_args[:1] == ["--"]:
        provider_args.pop(0)
    session = None
    try:
        text = _text_input(args.text, "Claude prompt")
        session = ManagedClaude(provider_args, cwd=args.cwd)
        output = session.turn(text, timeout=args.timeout)
        exit_code = session.close()
    except ClaudeTurnError as exc:
        exit_code = session.close(timeout=0) if session is not None else None
        _print_json(
            {
                "provider": "claude",
                "status": "transport_error",
                "error": str(exc),
                "events": exc.events,
                "stderr": exc.stderr_output,
                "exit_code": exit_code,
            }
        )
        print(f"abq claude run: {exc}", file=sys.stderr)
        return 124 if exc.timed_out else 2
    except (ValueError, Unreachable) as exc:
        if session is not None:
            session.close(timeout=0)
        print(f"abq claude run: {exc}", file=sys.stderr)
        return 2
    envelope = {
        "provider": "claude",
        "result": output["result"],
        "exit_code": exit_code,
    }
    if args.include_events:
        envelope["events"] = output["events"]
    _print_json(envelope)
    return 1 if output["result"].get("is_error") else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="abq",
        description="Local transport between Claude Code and Codex.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("join", help="register this session under an alias")
    p.add_argument("alias")
    p.set_defaults(func=cmd_join)

    p = sub.add_parser("leave", help="drop an alias from the registry")
    p.add_argument("alias")
    p.set_defaults(func=cmd_leave)

    p = sub.add_parser("who", help="list registered agents and their reachability")
    p.set_defaults(func=cmd_who)

    p = sub.add_parser("send", help="send a message to an agent (or @all)")
    p.add_argument("to")
    p.add_argument("text")
    p.add_argument("--from", dest="sender", help="override the detected sender alias")
    p.add_argument("--no-hint", action="store_true", help="omit the reply footer")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("history", help="show the shared transcript")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("brief", help="print usage instructions aimed at an agent")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("return-open", help="create a resumable one-shot return address")
    p.add_argument("--ttl", type=float, default=returns.DEFAULT_TTL_SEC)
    p.set_defaults(func=cmd_return_open)

    p = sub.add_parser("return-send", help="publish one payload to a return address")
    p.add_argument("token")
    p.add_argument("text")
    p.set_defaults(func=cmd_return_send)

    p = sub.add_parser("return-wait", help="wait for or re-read a return payload")
    p.add_argument("token")
    p.add_argument("--timeout", type=float, required=True)
    p.add_argument("--poll", type=float, default=returns.POLL_INTERVAL_SEC)
    p.set_defaults(func=cmd_return_wait)

    p = sub.add_parser("return-close", help="remove a return address and its payload")
    p.add_argument("token")
    p.set_defaults(func=cmd_return_close)

    p = sub.add_parser("codex", help="provider-native Codex thread transport")
    codex = p.add_subparsers(dest="codex_command", required=True)

    def add_endpoint(command: argparse.ArgumentParser) -> None:
        command.add_argument("--endpoint", help="Codex app-server endpoint")

    command = codex.add_parser("start", help="start a thread from caller JSON params")
    command.add_argument("--params", required=True, help="JSON, @file, or - for stdin")
    add_endpoint(command)
    command.set_defaults(func=cmd_codex_start)

    command = codex.add_parser(
        "run", help="own a new thread through one turn and its terminal result"
    )
    command.add_argument(
        "--thread-params", required=True, help="JSON, @file, or - for stdin"
    )
    command.add_argument(
        "--turn-params", required=True, help="JSON or @file"
    )
    command.add_argument("--timeout", type=float, required=True)
    command.add_argument(
        "--delete-thread",
        action="store_true",
        help="delete the thread after a terminal result; never implied",
    )
    add_endpoint(command)
    command.set_defaults(func=cmd_codex_run)

    command = codex.add_parser("turn", help="start a turn and optionally wait for it")
    command.add_argument("thread_id")
    command.add_argument("--params", required=True, help="JSON, @file, or - for stdin")
    command.add_argument(
        "--wait", type=float, metavar="SECONDS", help="wait for the terminal turn"
    )
    add_endpoint(command)
    command.set_defaults(func=cmd_codex_turn)

    command = codex.add_parser("wait", help="resume waiting for a terminal turn")
    command.add_argument("thread_id")
    command.add_argument("turn_id")
    command.add_argument("--timeout", type=float, required=True)
    add_endpoint(command)
    command.set_defaults(func=cmd_codex_wait)

    command = codex.add_parser("interrupt", help="interrupt an active turn")
    command.add_argument("thread_id")
    command.add_argument("turn_id")
    add_endpoint(command)
    command.set_defaults(func=cmd_codex_interrupt)

    command = codex.add_parser("delete", help="delete a caller-selected thread")
    command.add_argument("thread_id")
    add_endpoint(command)
    command.set_defaults(func=cmd_codex_delete)

    p = sub.add_parser("claude", help="provider-native Claude stream transport")
    claude = p.add_subparsers(dest="claude_command", required=True)
    command = claude.add_parser("run", help="start a stream, send one turn, wait for result")
    command.add_argument("--timeout", type=float, required=True)
    command.add_argument("--cwd")
    command.add_argument("--include-events", action="store_true")
    command.add_argument("text", help="prompt text, @file, or - for stdin")
    command.add_argument(
        "provider_args",
        nargs=argparse.REMAINDER,
        help="opaque Claude flags after --; ABQ does not choose provider policy",
    )
    command.set_defaults(func=cmd_claude_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
