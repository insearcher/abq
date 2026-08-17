"""The Claude adapter against a stand-in for a running session.

The fake server speaks the same newline-delimited JSON the real session does,
so these tests pin the frame shape that Claude Code accepts.
"""

import json
import os
import shutil
import socket
import tempfile
import threading
import queue

import pytest

from abq.adapters.base import Held, Unreachable
from abq.adapters.claude import ClaudeAdapter, ClaudeTurnError, ManagedClaude
from abq.registry import Agent


@pytest.fixture
def sockdir():
    """A directory short enough for a Unix socket path (104 bytes on macOS)."""
    path = tempfile.mkdtemp(prefix="abq-t", dir="/tmp")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class FakeSession:
    """Listens like a Claude Code session and records what it was sent."""

    def __init__(self, directory, name="4242.sock", receipt=None):
        self.path = os.path.join(str(directory), name)
        self.receipt = receipt
        self.frames = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(4)
        self._server.settimeout(3)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._server.accept()
            except (socket.timeout, OSError):
                return
            with conn:
                buffer = b""
                while b"\n" not in buffer:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buffer += chunk
            if not buffer:
                continue
            frame = json.loads(buffer.split(b"\n", 1)[0])
            self.frames.append(frame)
            if self.receipt is not None:
                self._reply(frame)

    def _reply(self, frame):
        sender = frame.get("from", "")
        if not sender.startswith("uds:"):
            return
        payload = dict(self.receipt, orig_msg_id=frame.get("msg_id"))
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(sender[4:])
            client.sendall((json.dumps(payload) + "\n").encode())
        except OSError:
            pass
        finally:
            client.close()

    def close(self):
        self._server.close()


def make_agent(session):
    return Agent(
        alias="peer",
        provider="claude",
        cwd="/tmp",
        address={"session_id": "abc-123", "socket_path": session.path},
    )


@pytest.fixture
def session(sockdir):
    server = FakeSession(sockdir)
    yield server
    server.close()


def test_deliver_sends_the_frame_claude_code_expects(session):
    adapter = ClaudeAdapter()
    msg_id = adapter.deliver(make_agent(session), "hello there", sender="api")

    frame = session.frames[0]
    assert frame["type"] == "user"
    assert frame["message"] == {"role": "user", "content": "hello there"}
    assert frame["msg_id"] == msg_id
    assert frame["session_id"] == "abc-123"


def test_reply_address_is_a_socket_next_to_the_target(session, sockdir):
    ClaudeAdapter().deliver(make_agent(session), "hi", sender="api")

    reply = session.frames[0]["from"]
    assert reply.startswith("uds:")
    assert os.path.dirname(reply[4:]) == sockdir


def test_unicode_is_not_escaped_away(session):
    ClaudeAdapter().deliver(make_agent(session), "привет 👋", sender="api")

    assert session.frames[0]["message"]["content"] == "привет 👋"


def test_held_receipt_becomes_a_held_error(sockdir):
    server = FakeSession(sockdir, receipt={"status": "held"})
    try:
        with pytest.raises(Held) as caught:
            ClaudeAdapter().deliver(make_agent(server), "hi", sender="api")
        assert "approve" in caught.value.hint
    finally:
        server.close()


def test_denied_receipt_is_a_failure(sockdir):
    server = FakeSession(sockdir, receipt={"status": "denied"})
    try:
        with pytest.raises(Unreachable):
            ClaudeAdapter().deliver(make_agent(server), "hi", sender="api")
    finally:
        server.close()


def test_missing_socket_is_unreachable(tmp_path):
    agent = Agent(
        alias="ghost",
        provider="claude",
        cwd="/tmp",
        address={"session_id": "x", "socket_path": str(tmp_path / "gone.sock")},
    )

    with pytest.raises(Unreachable):
        ClaudeAdapter().deliver(agent, "hi", sender="api")


def test_reachability(session, sockdir):
    adapter = ClaudeAdapter()
    assert adapter.is_reachable(make_agent(session)) is True

    dead = Agent(
        alias="d", provider="claude", cwd="/tmp",
        address={"socket_path": os.path.join(sockdir, "nope.sock")},
    )
    assert adapter.is_reachable(dead) is False


def test_detect_self_reads_the_session_environment(monkeypatch, tmp_path):
    sock = tmp_path / "77.sock"
    sock.write_text("")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-77")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", str(sock))

    assert ClaudeAdapter().detect_self() == {
        "session_id": "sess-77",
        "socket_path": str(sock),
    }


def test_detect_self_outside_a_session(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET", raising=False)
    monkeypatch.setattr("abq.adapters.claude._owning_pid", lambda: None)

    assert ClaudeAdapter().detect_self() is None


class _FakeOutput:
    def __init__(self):
        self.lines = queue.Queue()

    def __iter__(self):
        return self

    def __next__(self):
        line = self.lines.get(timeout=2)
        if line is None:
            raise StopIteration
        return line


class _FakeInput:
    def __init__(self, process):
        self.process = process

    def write(self, line):
        frame = json.loads(line)
        text = frame["message"]["content"][0]["text"]
        self.process.stdout.lines.put(json.dumps({"type": "assistant", "text": text}) + "\n")
        self.process.stdout.lines.put(
            json.dumps(
                {
                    "type": "result",
                    "session_id": "same-session",
                    "result": f"reply:{text}",
                    "is_error": False,
                }
            )
            + "\n"
        )
        return len(line)

    def flush(self):
        pass

    def close(self):
        self.process.returncode = 0
        self.process.stdout.lines.put(None)


class _FakeProcess:
    def __init__(self, command):
        self.command = command
        self.pid = 4242
        self.returncode = None
        self.stdout = _FakeOutput()
        self.stderr = []
        self.stdin = _FakeInput(self)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def test_managed_claude_keeps_one_stream_for_multiple_turns(monkeypatch):
    created = []

    def fake_popen(command, **kwargs):
        process = _FakeProcess(command)
        created.append(process)
        return process

    monkeypatch.setattr("abq.adapters.claude.subprocess.Popen", fake_popen)

    session = ManagedClaude(["--model", "fable", "--effort", "xhigh"])
    first = session.turn("one", timeout=1)
    second = session.turn("two", timeout=1)
    exit_code = session.close()

    assert first["result"]["result"] == "reply:one"
    assert second["result"]["result"] == "reply:two"
    assert first["result"]["session_id"] == second["result"]["session_id"]
    assert len(created) == 1
    assert "--model" in created[0].command
    assert exit_code == 0


def test_managed_claude_rejects_overrides_of_wire_flags():
    with pytest.raises(ValueError, match="controlled"):
        ManagedClaude(["--output-format=json"])


def test_managed_claude_preserves_partial_events_on_timeout(monkeypatch):
    def fake_popen(command, **kwargs):
        process = _FakeProcess(command)

        def write_without_result(line):
            frame = json.loads(line)
            text = frame["message"]["content"][0]["text"]
            process.stdout.lines.put(
                json.dumps({"type": "assistant", "text": text}) + "\n"
            )
            return len(line)

        process.stdin.write = write_without_result
        return process

    monkeypatch.setattr("abq.adapters.claude.subprocess.Popen", fake_popen)
    session = ManagedClaude([])

    with pytest.raises(ClaudeTurnError) as caught:
        session.turn("partial", timeout=0.02)

    assert caught.value.timed_out is True
    assert caught.value.events == [{"type": "assistant", "text": "partial"}]
    session.close()
