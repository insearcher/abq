"""The Codex adapter against a stand-in app-server.

The fake server does the WebSocket upgrade and answers JSON-RPC the way
`codex app-server` does, so these tests pin the handshake and the `turn/start`
payload without needing Codex installed.
"""

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import tempfile
import threading
import time

import pytest

from abq.adapters.base import Unreachable
from abq.adapters.codex import CodexAdapter, Connection
from abq.registry import Agent

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()


class FakeAppServer:
    """Speaks just enough of the app-server protocol to exercise the adapter."""

    def __init__(self, directory, loaded=("thread-1",), fail_turn=False):
        self.path = os.path.join(directory, "codex.sock")
        self.loaded = list(loaded)
        self.fail_turn = fail_turn
        self.requests = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(4)
        self._server.settimeout(5)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # -- websocket plumbing ------------------------------------------------

    @staticmethod
    def _recv_frame(conn, buffer):
        while len(buffer) < 2:
            buffer += conn.recv(4096)
        length = buffer[1] & 0x7F
        masked = buffer[1] & 0x80
        offset = 2
        if length == 126:
            while len(buffer) < 4:
                buffer += conn.recv(4096)
            (length,) = struct.unpack(">H", buffer[2:4])
            offset = 4
        mask = b""
        if masked:
            while len(buffer) < offset + 4:
                buffer += conn.recv(4096)
            mask = buffer[offset:offset + 4]
            offset += 4
        while len(buffer) < offset + length:
            buffer += conn.recv(4096)
        data = bytearray(buffer[offset:offset + length])
        if masked:
            for i in range(len(data)):
                data[i] ^= mask[i % 4]
        return bytes(data), buffer[offset + length:]

    @staticmethod
    def _send_frame(conn, text):
        payload = text.encode()
        header = bytearray([0x81])
        if len(payload) < 126:
            header.append(len(payload))
        else:
            header.append(126)
            header += struct.pack(">H", len(payload))
        conn.sendall(bytes(header) + payload)

    def _serve(self):
        while True:
            try:
                conn, _ = self._server.accept()
            except (socket.timeout, OSError):
                return
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn):
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(4096)
            key = ""
            for line in request.decode(errors="replace").split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {_accept_key(key)}\r\n\r\n"
                ).encode()
            )
            buffer = b""
            while True:
                raw, buffer = self._recv_frame(conn, buffer)
                if not raw:
                    return
                message = json.loads(raw)
                self.requests.append(message)
                reply = self._respond(message)
                if reply is not None:
                    self._send_frame(conn, json.dumps(reply))
        except (OSError, ValueError, json.JSONDecodeError):
            return

    def _respond(self, message):
        method, request_id = message.get("method"), message.get("id")
        if request_id is None:
            return None  # a notification
        if method == "initialize":
            return {"id": request_id, "result": {"userAgent": "fake/1.0"}}
        if method == "thread/loaded/list":
            return {"id": request_id, "result": {"data": self.loaded}}
        if method == "turn/start":
            if self.fail_turn:
                return {
                    "id": request_id,
                    "error": {"code": -32600, "message": "no such thread"},
                }
            return {"id": request_id, "result": {"turn": {"id": "turn-99"}}}
        return {"id": request_id, "error": {"code": -32601, "message": "unknown"}}

    def sent(self, method):
        return [r for r in self.requests if r.get("method") == method]

    def wait_for(self, predicate, timeout=5.0):
        """Wait for the server thread to record something.

        The client hands a frame to the socket and moves on, so asserting
        straight away races the server's reader thread.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = predicate(self)
            if found:
                return found
            time.sleep(0.02)
        return predicate(self)

    def close(self):
        self._server.close()


@pytest.fixture
def sockdir():
    path = tempfile.mkdtemp(prefix="abq-c", dir="/tmp")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def server(sockdir):
    fake = FakeAppServer(sockdir)
    yield fake
    fake.close()


def agent_for(server, thread_id="thread-1"):
    return Agent(
        alias="cx",
        provider="codex",
        cwd="/tmp",
        address={"thread_id": thread_id, "endpoint": f"unix://{server.path}"},
    )


def test_handshake_sends_client_info_and_initialized(server):
    with Connection(f"unix://{server.path}"):
        pass

    params = server.wait_for(lambda s: s.sent("initialize"))[0]["params"]
    assert params["clientInfo"]["name"] == "abq"
    assert params["capabilities"]["experimentalApi"] is True
    assert server.wait_for(
        lambda s: s.sent("initialized")
    ), "the server expects the initialized notification"


def test_deliver_starts_a_turn_with_the_text_as_user_input(server):
    msg_id = CodexAdapter().deliver(agent_for(server), "ping from api", sender="api")

    params = server.wait_for(lambda s: s.sent("turn/start"))[0]["params"]
    assert params["threadId"] == "thread-1"
    assert params["input"] == [{"type": "text", "text": "ping from api"}]
    assert msg_id == "turn-99"


def test_delivery_refuses_a_thread_the_server_does_not_have(server):
    with pytest.raises(Unreachable, match="session probably ended|not have that thread"):
        CodexAdapter().deliver(agent_for(server, "ghost-thread"), "hi", sender="api")

    assert server.sent("turn/start") == []


def test_server_side_error_surfaces(sockdir):
    server = FakeAppServer(sockdir, fail_turn=True)
    try:
        with pytest.raises(Unreachable, match="no such thread"):
            CodexAdapter().deliver(agent_for(server), "hi", sender="api")
    finally:
        server.close()


def test_missing_app_server_is_unreachable(sockdir):
    agent = Agent(
        alias="cx",
        provider="codex",
        cwd="/tmp",
        address={"thread_id": "t", "endpoint": f"unix://{sockdir}/absent.sock"},
    )

    with pytest.raises(Unreachable, match="cannot reach"):
        CodexAdapter().deliver(agent, "hi", sender="api")


def test_reachability_reflects_loaded_threads(server):
    adapter = CodexAdapter()
    assert adapter.is_reachable(agent_for(server)) is True
    assert adapter.is_reachable(agent_for(server, "not-loaded")) is False


def test_approval_requests_are_declined_not_ignored(server):
    """abq must never approve anything on the human's behalf."""
    connection = Connection(f"unix://{server.path}")
    try:
        connection._handle_inbound(
            {"id": 7, "method": "execCommandApproval", "params": {}}
        )
    finally:
        connection.close()

    # The fake server records what we send it, including our response frames.
    answers = server.wait_for(
        lambda s: [r for r in s.requests if r.get("id") == 7 and "result" in r]
    )
    assert answers and answers[0]["result"]["decision"] == "denied"


def test_detect_self_uses_the_thread_id_codex_exports(monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-abc")
    monkeypatch.delenv("ABQ_CODEX_ENDPOINT", raising=False)

    detected = CodexAdapter().detect_self()
    assert detected["thread_id"] == "thread-abc"
    assert detected["endpoint"].startswith("unix://")


def test_detect_self_outside_codex(monkeypatch):
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)

    assert CodexAdapter().detect_self() is None
