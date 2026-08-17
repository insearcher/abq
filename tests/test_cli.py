import io
import json

import pytest

from abq import cli
from abq.adapters.base import Held, Unreachable
from abq.registry import Agent, Registry


class FakeAdapter:
    name = "fake"

    def __init__(self, outcome="ok"):
        self.outcome = outcome
        self.delivered = []

    def detect_self(self):
        return {"session_id": "me"}

    def is_reachable(self, agent):
        return self.outcome != "gone"

    def pending(self, agent):
        return None

    def warnings(self):
        return []

    def deliver(self, agent, text, sender):
        self.delivered.append((agent.alias, text, sender))
        if self.outcome == "held":
            raise Held("msg-1", "approve it there")
        if self.outcome == "gone":
            raise Unreachable("session closed")
        return "msg-1"


@pytest.fixture
def bus(tmp_path, monkeypatch):
    """A registry with two peers and a swappable adapter."""
    monkeypatch.setenv("ABQ_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "DETECTION_ORDER", ("fake",), raising=False)

    adapter = FakeAdapter()
    monkeypatch.setitem(cli.ADAPTERS, "fake", adapter)
    monkeypatch.setattr(
        "abq.adapters.DETECTION_ORDER", ("fake",)
    )

    registry = Registry()
    registry.join(Agent(alias="api", provider="fake", cwd="/a", address={"session_id": "me"}))
    registry.join(Agent(alias="web", provider="fake", cwd="/b", address={"session_id": "you"}))
    return adapter


def run(argv):
    return cli.main(argv)


def test_send_delivers_and_reports(bus, capsys):
    assert run(["send", "web", "ping"]) == 0

    alias, text, sender = bus.delivered[0]
    assert alias == "web"
    assert "ping" in text
    assert sender == "api"  # detected from the current session
    assert "delivered" in capsys.readouterr().out


def test_message_names_its_sender(bus):
    run(["send", "web", "ping"])

    assert bus.delivered[0][1].startswith("[abq] message from 'api':")


def test_footer_tells_the_recipient_how_to_answer(bus):
    run(["send", "web", "ping"])

    body = bus.delivered[0][1]
    assert "You are 'web'" in body
    assert 'abq send api "text"' in body


def test_footer_is_honest_when_the_sender_is_a_plain_shell(bus):
    run(["send", "web", "ping", "--from", "human"])

    assert "not a registered session" in bus.delivered[0][1]


def test_no_hint_sends_the_bare_message(bus):
    run(["send", "web", "ping", "--no-hint"])

    assert "You are" not in bus.delivered[0][1]


def test_held_delivery_is_reported_as_held_not_success(bus, capsys):
    bus.outcome = "held"

    assert run(["send", "web", "ping"]) == 0
    assert "HELD" in capsys.readouterr().out


def test_unreachable_peer_is_an_error(bus, capsys):
    bus.outcome = "gone"

    assert run(["send", "web", "ping"]) == 1
    assert "unreachable" in capsys.readouterr().err


def test_unknown_alias_is_an_error(bus, capsys):
    assert run(["send", "nobody", "ping"]) == 1
    assert "not registered" in capsys.readouterr().err


def test_send_to_all_skips_the_sender(bus):
    run(["send", "@all", "sync"])

    assert [alias for alias, _, _ in bus.delivered] == ["web"]


def test_history_records_what_was_sent_not_the_footer(bus, capsys):
    run(["send", "web", "ping"])
    capsys.readouterr()

    run(["history", "-n", "5"])
    out = capsys.readouterr().out
    assert "api -> web  ping" in out
    assert "You are" not in out


def test_who_marks_the_current_session(bus, capsys):
    run(["who"])

    out = capsys.readouterr().out
    assert "api (you)" in out
    assert "web" in out


def test_compose_without_footer_is_just_the_message():
    body = cli.compose(
        "text", sender="a", recipient="b", sender_is_a_session=True, footer=False
    )

    assert body == "[abq] message from 'a':\n\ntext"


def test_follow_flushes_each_line(bus, monkeypatch, capsys):
    """Piping `-f` must stream, not sit in a block buffer."""
    flushes = []
    real_print = cli.print if hasattr(cli, "print") else print

    def spy(*args, **kwargs):
        flushes.append(kwargs.get("flush", False))
        return real_print(*args, **kwargs)

    monkeypatch.setattr("builtins.print", spy)
    monkeypatch.setattr(
        cli.history, "follow", lambda: iter([{"from": "a", "to": "b", "text": "hi"}])
    )

    cli.main(["history", "-n", "0", "-f"])

    # The banner may be unflushed; the streamed record must not be.
    assert flushes and flushes[-1] is True


def test_return_cli_round_trip_is_rereadable(bus, capsys):
    assert run(["return-open", "--ttl", "60"]) == 0
    token = capsys.readouterr().out.strip()

    assert run(["return-send", token, "result text"]) == 0
    capsys.readouterr()
    assert run(["return-wait", token, "--timeout", "0"]) == 0
    assert capsys.readouterr().out.strip() == "result text"
    assert run(["return-wait", token, "--timeout", "0"]) == 0
    assert capsys.readouterr().out.strip() == "result text"


def test_return_cli_pending_has_distinct_exit_code(bus, capsys):
    run(["return-open", "--ttl", "60"])
    token = capsys.readouterr().out.strip()

    assert run(["return-wait", token, "--timeout", "0"]) == 3
    assert "pending" in capsys.readouterr().err


def test_codex_start_passes_caller_json_without_policy_defaults(bus, monkeypatch, capsys):
    calls = []

    class FakeConnection:
        def __init__(self, endpoint):
            calls.append(("endpoint", endpoint))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def start_thread(self, params):
            calls.append(("params", params))
            return {"thread": {"id": "thread-1"}}

    monkeypatch.setattr(cli, "Connection", FakeConnection)
    params = {"model": "gpt-5.6-sol", "approvalPolicy": "never", "sandbox": "read-only"}

    assert run(["codex", "start", "--endpoint", "unix:///tmp/c.sock", "--params", json.dumps(params)]) == 0
    assert calls == [("endpoint", "unix:///tmp/c.sock"), ("params", params)]
    assert json.loads(capsys.readouterr().out)["thread"]["id"] == "thread-1"


def test_codex_run_keeps_one_connection_and_deletes_only_when_requested(
    bus, monkeypatch, capsys
):
    calls = []

    class FakeConnection:
        server_requests = [{"method": "item/commandExecution/requestApproval"}]

        def __init__(self, endpoint):
            calls.append(("connect", endpoint))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            calls.append(("close",))

        def start_thread(self, params):
            calls.append(("start_thread", params))
            return {"thread": {"id": "thread-1"}}

        def start_turn_with_params(self, thread_id, params):
            calls.append(("start_turn", thread_id, params))
            return {"turn": {"id": "turn-1"}}

        def wait_turn(self, thread_id, turn_id, timeout):
            calls.append(("wait", thread_id, turn_id, timeout))
            return {
                "id": turn_id,
                "status": "completed",
                "items": [{"type": "agentMessage", "text": "done"}],
            }

        def delete_thread(self, thread_id):
            calls.append(("delete", thread_id))
            return {}

    monkeypatch.setattr(cli, "Connection", FakeConnection)

    assert run(
        [
            "codex",
            "run",
            "--endpoint",
            "unix:///tmp/c.sock",
            "--thread-params",
            '{"approvalPolicy":"on-request"}',
            "--turn-params",
            '{"input":[]}',
            "--timeout",
            "30",
            "--delete-thread",
        ]
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert calls == [
        ("connect", "unix:///tmp/c.sock"),
        ("start_thread", {"approvalPolicy": "on-request"}),
        ("start_turn", "thread-1", {"input": []}),
        ("wait", "thread-1", "turn-1", 30.0),
        ("delete", "thread-1"),
        ("close",),
    ]
    assert output["agent_messages"] == ["done"]
    assert output["thread_deleted"] is True
    assert output["server_requests"] == FakeConnection.server_requests


def test_claude_run_passes_provider_flags_and_hides_events_by_default(
    bus, monkeypatch, capsys
):
    calls = []

    class FakeManagedClaude:
        def __init__(self, provider_args, cwd=None):
            calls.append((provider_args, cwd))

        def turn(self, text, timeout):
            calls.append((text, timeout))
            return {
                "result": {"type": "result", "result": "ok", "is_error": False},
                "events": [{"type": "system", "private": "large"}],
            }

        def close(self):
            return 0

    monkeypatch.setattr(cli, "ManagedClaude", FakeManagedClaude)

    assert run(
        [
            "claude",
            "run",
            "--timeout",
            "30",
            "--cwd",
            "/work",
            "review",
            "--",
            "--model",
            "fable",
            "--effort",
            "xhigh",
        ]
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert calls == [
        (["--model", "fable", "--effort", "xhigh"], "/work"),
        ("review", 30.0),
    ]
    assert output["result"]["result"] == "ok"
    assert "events" not in output


def test_claude_run_reads_large_prompt_from_stdin(bus, monkeypatch, capsys):
    calls = []
    prompt = "x" * (256 * 1024)

    class FakeManagedClaude:
        def __init__(self, provider_args, cwd=None):
            pass

        def turn(self, text, timeout):
            calls.append(text)
            return {
                "result": {"type": "result", "result": "ok", "is_error": False},
                "events": [],
            }

        def close(self, timeout=10):
            return 0

    monkeypatch.setattr(cli, "ManagedClaude", FakeManagedClaude)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(prompt))

    assert run(["claude", "run", "--timeout", "30", "-"]) == 0
    assert calls == [prompt]
    capsys.readouterr()


def test_claude_run_reads_prompt_from_file(bus, monkeypatch, tmp_path, capsys):
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("private prompt", encoding="utf-8")
    calls = []

    class FakeManagedClaude:
        def __init__(self, provider_args, cwd=None):
            pass

        def turn(self, text, timeout):
            calls.append(text)
            return {
                "result": {"type": "result", "result": "ok", "is_error": False},
                "events": [],
            }

        def close(self, timeout=10):
            return 0

    monkeypatch.setattr(cli, "ManagedClaude", FakeManagedClaude)

    assert run(
        ["claude", "run", "--timeout", "30", f"@{prompt_file}"]
    ) == 0
    assert calls == ["private prompt"]
    capsys.readouterr()


def test_claude_run_preserves_partial_failure_envelope(bus, monkeypatch, capsys):
    class FakeManagedClaude:
        def __init__(self, provider_args, cwd=None):
            pass

        def turn(self, text, timeout):
            raise cli.ClaudeTurnError(
                "timed out waiting for Claude result",
                events=[{"type": "assistant", "text": "partial"}],
                stderr="provider diagnostic",
                timed_out=True,
            )

        def close(self, timeout=10):
            return -15

    monkeypatch.setattr(cli, "ManagedClaude", FakeManagedClaude)

    assert run(["claude", "run", "--timeout", "0.01", "review"]) == 124
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["status"] == "transport_error"
    assert output["events"] == [{"type": "assistant", "text": "partial"}]
    assert output["stderr"] == "provider diagnostic"
    assert output["exit_code"] == -15
    assert "timed out" in captured.err


def test_claude_provider_error_and_transport_error_have_distinct_codes(
    bus, monkeypatch, capsys
):
    class ProviderError:
        def __init__(self, provider_args, cwd=None):
            pass

        def turn(self, text, timeout):
            return {
                "result": {"type": "result", "result": "no", "is_error": True},
                "events": [],
            }

        def close(self, timeout=10):
            return 0

    monkeypatch.setattr(cli, "ManagedClaude", ProviderError)
    assert run(["claude", "run", "--timeout", "30", "review"]) == 1
    capsys.readouterr()

    class TransportError:
        def __init__(self, provider_args, cwd=None):
            raise Unreachable("cannot start")

    monkeypatch.setattr(cli, "ManagedClaude", TransportError)
    assert run(["claude", "run", "--timeout", "30", "review"]) == 2
