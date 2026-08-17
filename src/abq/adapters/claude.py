"""Claude Code adapter.

Transport: the per-session Unix socket Claude Code opens for cross-session
messaging. Each session listens on `<tmpdir>/cc-socks/<pid>.sock` (mode 0600)
and exports the path as `$CLAUDE_CODE_MESSAGING_SOCKET`. Frames are
newline-delimited JSON; an inbound message is:

    {"type": "user", "message": {"role": "user", "content": "..."}}

There is no token handshake — authorisation is the 0600 socket, i.e. the same
local user. Sending `session_id` is optional but we always do: the receiver
drops the frame when it does not match, which is exactly the guard we want if
the operating system recycled the pid onto a different session.

Delivery is gated by the receiver's `crossSessionInbound` setting. With
"accept" the text reaches the model right away; with "hold" it waits for the
user to approve it in the receiving session, and the receiver sends back a
`held` receipt. We surface that status rather than pretending the send
succeeded.

Verified against Claude Code 2.1.227, 2.1.229, and 2.1.233. See
COMPATIBILITY.md.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import threading
import time
import uuid

from ..paths import claude_home
from ..registry import Agent
from .base import Held, Unreachable

SOCKET_DIR_NAME = "cc-socks"
RECEIPT_WAIT_SEC = 1.5
CONNECT_TIMEOUT_SEC = 5.0
MANAGED_WIRE_FLAGS = (
    "-p",
    "--input-format",
    "stream-json",
    "--output-format",
    "stream-json",
    "--verbose",
    "--replay-user-messages",
)
MANAGED_RESERVED_FLAGS = {
    "-p",
    "--print",
    "--input-format",
    "--output-format",
    "--verbose",
    "--replay-user-messages",
}
STREAM_CLOSED = object()


class ClaudeTurnError(Unreachable):
    """A failed managed turn with the stream evidence received before failure."""

    def __init__(
        self,
        message: str,
        *,
        events: list[dict],
        stderr: str,
        timed_out: bool = False,
    ) -> None:
        self.events = list(events)
        self.stderr_output = stderr
        self.timed_out = timed_out
        super().__init__(message)


def _socket_base_dir() -> str:
    return (
        os.environ.get("XDG_RUNTIME_DIR")
        or os.environ.get("CLAUDE_CODE_TMPDIR")
        or "/tmp"
    )


def socket_path_for_pid(pid: int) -> str:
    return os.path.join(_socket_base_dir(), SOCKET_DIR_NAME, f"{pid}.sock")


def _owning_pid() -> int | None:
    """Walk up the process tree to the Claude Code process running this shell."""
    pid = os.getpid()
    for _ in range(8):
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        if not out:
            return None
        parent, _, command = out.partition(" ")
        try:
            pid = int(parent)
        except ValueError:
            return None
        if "claude" in command:
            return pid
        if pid <= 1:
            return None
    return None


def inbound_policy() -> str | None:
    """The user-level `crossSessionInbound` setting, when one is configured.

    Command-line and managed settings outrank this file and are not readable
    from here, so treat the answer as advisory.
    """
    try:
        with open(os.path.join(claude_home(), "settings.json"), encoding="utf-8") as fh:
            return json.load(fh).get("crossSessionInbound")
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return None


class _ReceiptListener:
    """Reply socket the receiver connects back to with a delivery status.

    It has to live in the same directory as the target socket: the receiver
    refuses reply addresses outside its own socket directory.
    """

    def __init__(self, base_dir: str, wait: float = RECEIPT_WAIT_SEC) -> None:
        self.path = os.path.join(base_dir, f"abq-reply-{uuid.uuid4().hex[:12]}.sock")
        self.wait = wait
        self.receipt: dict | None = None
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        os.chmod(self.path, 0o600)
        self._server.listen(2)
        self._server.settimeout(wait)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._server.accept()
            conn.settimeout(2)
            buffer = b""
            while b"\n" not in buffer and len(buffer) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buffer += chunk
            conn.close()
            line = buffer.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
            if line:
                self.receipt = json.loads(line)
        except (socket.timeout, OSError, json.JSONDecodeError):
            pass
        finally:
            self._server.close()
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass

    def result(self) -> dict | None:
        self._thread.join(self.wait + 0.5)
        return self.receipt


class ClaudeAdapter:
    name = "claude"

    def detect_self(self) -> dict | None:
        session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
        socket_path = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
        if not socket_path:
            pid = _owning_pid()
            if pid is None:
                return None
            socket_path = socket_path_for_pid(pid)
            if not os.path.exists(socket_path):
                return None
        if not session_id:
            return None
        return {"session_id": session_id, "socket_path": socket_path}

    def is_reachable(self, agent: Agent) -> bool:
        path = agent.address.get("socket_path")
        if not path or not os.path.exists(path):
            return False
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(1)
        try:
            probe.connect(path)
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def pending(self, agent: Agent) -> int | None:
        return None  # Claude Code does not expose a queue depth.

    def warnings(self) -> list[str]:
        policy = inbound_policy()
        if policy in {"hold", "refuse"}:
            return [
                f'this machine sets crossSessionInbound="{policy}", so incoming '
                "messages wait for your approval in the receiving session; start "
                "a session with --settings '{\"crossSessionInbound\":\"accept\"}' "
                "to let them through"
            ]
        return []

    def deliver(self, agent: Agent, text: str, sender: str) -> str:
        path = agent.address.get("socket_path")
        if not path:
            raise Unreachable("no socket recorded — re-run `abq join` in that session")
        if not os.path.exists(path):
            raise Unreachable(f"socket is gone ({path})")

        msg_id = str(uuid.uuid4())
        listener: _ReceiptListener | None = None
        try:
            listener = _ReceiptListener(os.path.dirname(path))
        except OSError:
            listener = None  # receipts are a nicety, not a requirement

        frame = {
            "type": "user",
            "from": f"uds:{listener.path}" if listener else sender,
            "msg_id": msg_id,
            "message": {"role": "user", "content": text},
        }
        if agent.address.get("session_id"):
            frame["session_id"] = agent.address["session_id"]

        payload = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(CONNECT_TIMEOUT_SEC)
        try:
            client.connect(path)
            client.sendall(payload)
            try:
                client.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        except OSError as exc:
            raise Unreachable(f"{type(exc).__name__}: {exc}") from exc
        finally:
            client.close()

        if listener is not None:
            # Silence is the success case: the accept path sends no receipt.
            receipt = listener.result()
            status = (receipt or {}).get("status")
            if status == "held":
                raise Held(
                    msg_id,
                    'the receiving session has crossSessionInbound="hold" — '
                    "approve it there, or start that session with "
                    "--settings '{\"crossSessionInbound\":\"accept\"}'",
                )
            if status in {"denied", "expired"}:
                raise Unreachable(f"receiver reported {status}")
        return msg_id


class ManagedClaude:
    """A caller-owned Claude stream-json process that accepts multiple turns.

    ABQ supplies only the wire-format flags.  Model, effort, permissions,
    system prompt, tools, fallback, and every other provider policy remain
    opaque caller-provided arguments.
    """

    def __init__(
        self,
        provider_args: list[str],
        *,
        cwd: str | None = None,
        executable: str = "claude",
    ) -> None:
        for arg in provider_args:
            flag = arg.split("=", 1)[0]
            if flag in MANAGED_RESERVED_FLAGS:
                raise ValueError(f"{flag} is controlled by the ABQ stream transport")
        self.command = [executable, *MANAGED_WIRE_FLAGS, *provider_args]
        try:
            self._process = subprocess.Popen(
                self.command,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise Unreachable(f"cannot start Claude Code: {exc}") from exc
        self._stdout: queue.Queue[dict | Exception | object] = queue.Queue()
        self._stderr: list[str] = []
        self._turn_lock = threading.Lock()
        self._closed = False
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def __enter__(self) -> "ManagedClaude":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def pid(self) -> int:
        return self._process.pid

    def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        try:
            for line in self._process.stdout:
                if not line.strip():
                    continue
                try:
                    self._stdout.put(json.loads(line))
                except json.JSONDecodeError as exc:
                    self._stdout.put(exc)
        finally:
            self._stdout.put(STREAM_CLOSED)

    def _read_stderr(self) -> None:
        assert self._process.stderr is not None
        for line in self._process.stderr:
            # Enough context for a useful failure without allowing an unbounded
            # long-running provider log to consume the parent process.
            if sum(len(part) for part in self._stderr) < 1024 * 1024:
                self._stderr.append(line)

    def stderr(self) -> str:
        return "".join(self._stderr)

    def turn(self, text: str, timeout: float) -> dict:
        if timeout < 0:
            raise ValueError("Claude turn timeout cannot be negative")
        if self._closed or self._process.poll() is not None:
            raise Unreachable(
                f"Claude stream is closed (exit {self._process.poll()}): {self.stderr()}"
            )
        message = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": text}],
            },
            "parent_tool_use_id": None,
            "session_id": "",
        }
        deadline = time.monotonic() + timeout
        events: list[dict] = []

        def fail(message: str, *, timed_out: bool = False) -> ClaudeTurnError:
            return ClaudeTurnError(
                message,
                events=events,
                stderr=self.stderr(),
                timed_out=timed_out,
            )

        with self._turn_lock:
            assert self._process.stdin is not None
            try:
                self._process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                self._process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise fail(f"Claude stream rejected the turn: {exc}") from exc
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise fail("timed out waiting for Claude result", timed_out=True)
                try:
                    event = self._stdout.get(timeout=remaining)
                except queue.Empty as exc:
                    raise fail(
                        "timed out waiting for Claude result", timed_out=True
                    ) from exc
                if event is STREAM_CLOSED:
                    raise fail("Claude stream closed before a result")
                if isinstance(event, Exception):
                    raise fail(f"Claude emitted invalid stream-json: {event}")
                events.append(event)
                if event.get("type") == "result":
                    return {"result": event, "events": events}

    def close(self, timeout: float = 10.0) -> int:
        if self._closed:
            return self._process.poll() if self._process.poll() is not None else 0
        self._closed = True
        if self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except OSError:
                pass
        try:
            return self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                return self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                return self._process.wait(timeout=2)
