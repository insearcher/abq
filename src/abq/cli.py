"""Command line interface.

The agents themselves are the main callers: they run `abq send ...` through
whatever shell tool they have. Humans mostly use `abq who` and `abq history`.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import history
from .adapters import ADAPTERS, Held, Unreachable, detect_current_session
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="abq",
        description="Messaging between running Claude Code and Codex sessions.",
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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
