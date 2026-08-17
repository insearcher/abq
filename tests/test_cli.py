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
