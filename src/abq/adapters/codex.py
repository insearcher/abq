"""Codex adapter.

Transport: the Codex app-server, spoken as JSON-RPC 2.0 over a WebSocket. A
message becomes a real user turn in the target thread via `turn/start`, and it
renders in an attached TUI exactly like something the human typed.

The one structural constraint comes from Codex itself: a plain `codex` TUI runs
its agent in-process and is invisible to any app-server, so it cannot be joined
after the fact. A session has to be started against a shared app-server:

    codex app-server --listen unix://~/.abq/codex.sock     # once
    codex --remote unix://~/.abq/codex.sock                # each session

Verified against codex-cli 0.145.0-alpha.29. See COMPATIBILITY.md.
"""

from __future__ import annotations

import json
import os
import socket
import time
import uuid

from ..paths import abq_home
from ..registry import Agent
from .base import Unreachable
from .websocket import WebSocket, WebSocketError

CLIENT_NAME = "abq"
REQUEST_TIMEOUT_SEC = 20.0
#: How long to stay connected after handing over a turn, answering whatever the
#: server asks us. Leaving immediately can strand a turn that needs an answer.
GRACE_SEC = 1.5


def endpoint() -> str:
    """Where the shared Codex app-server listens."""
    configured = os.environ.get("ABQ_CODEX_ENDPOINT")
    return configured or f"unix://{os.path.join(abq_home(), 'codex.sock')}"


def _unix_path(address: str) -> str | None:
    if address.startswith("unix://"):
        return os.path.expanduser(address[len("unix://") :])
    return None


class Connection:
    """One short-lived app-server session."""

    def __init__(self, address: str, timeout: float = REQUEST_TIMEOUT_SEC) -> None:
        self.address = address
        self._next_id = 0
        path = _unix_path(address)
        try:
            if path is not None:
                self._ws = WebSocket.connect_unix(path, timeout=timeout)
            elif address.startswith("ws://"):
                host, _, port = address[len("ws://") :].partition(":")
                self._ws = WebSocket.connect_tcp(host, int(port or 80), timeout=timeout)
            else:
                raise Unreachable(f"unsupported endpoint {address!r}")
        except (OSError, WebSocketError) as exc:
            raise Unreachable(f"cannot reach the Codex app-server at {address}: {exc}") from exc
        self._initialize()

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._ws.close()
        except OSError:
            pass

    def _initialize(self) -> None:
        self.request(
            "initialize",
            {
                "clientInfo": {"name": CLIENT_NAME, "title": "abq bridge", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "initialized", "params": {}})

    def _send(self, message: dict) -> None:
        self._ws.send(json.dumps(message, ensure_ascii=False))

    def request(self, method: str, params: dict | None = None,
                timeout: float = REQUEST_TIMEOUT_SEC) -> dict:
        self._next_id += 1
        request_id = self._next_id
        self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )
        deadline = time.monotonic() + timeout
        while True:
            message = self._receive(deadline)
            if message.get("id") == request_id and (
                "result" in message or "error" in message
            ):
                if "error" in message:
                    error = message["error"]
                    raise Unreachable(
                        f"{method} failed: {error.get('message', error)}"
                    )
                return message.get("result") or {}
            self._handle_inbound(message)

    def _receive(self, deadline: float) -> dict:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Unreachable("timed out waiting for the Codex app-server")
        self._ws.settimeout(remaining)
        try:
            raw = self._ws.receive()
        except (socket.timeout, TimeoutError) as exc:
            raise Unreachable("timed out waiting for the Codex app-server") from exc
        except (OSError, WebSocketError) as exc:
            raise Unreachable(f"app-server connection lost: {exc}") from exc
        if raw is None:
            raise Unreachable("app-server closed the connection")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _handle_inbound(self, message: dict) -> None:
        """Answer what the server asks us; ignore the rest.

        The server blocks the turn while a request of its own is outstanding,
        so silence here would stall the very message we just delivered.
        """
        method, request_id = message.get("method"), message.get("id")
        if method is None or request_id is None:
            return  # a notification — not our concern for delivery
        if method == "currentTime/read":
            result = {"currentTimeAt": int(time.time())}
        elif "approval" in method.lower():
            # abq is a message courier, not the human: never approve on their behalf.
            result = {"decision": "denied"}
        else:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"{method} not handled by abq"},
                }
            )
            return
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def drain(self, seconds: float = GRACE_SEC) -> None:
        """Keep answering the server for a moment after handing over a turn."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                self._handle_inbound(self._receive(deadline))
            except Unreachable:
                return

    # -- protocol surface ------------------------------------------------

    def loaded_thread_ids(self) -> list[str]:
        return list(self.request("thread/loaded/list").get("data") or [])

    def start_turn(self, thread_id: str, text: str) -> dict:
        return self.request(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": text}]},
        )


class CodexAdapter:
    name = "codex"

    def detect_self(self) -> dict | None:
        thread_id = os.environ.get("CODEX_THREAD_ID")
        if not thread_id:
            return None
        return {"thread_id": thread_id, "endpoint": endpoint()}

    def is_reachable(self, agent: Agent) -> bool:
        address = agent.address.get("endpoint", endpoint())
        thread_id = agent.address.get("thread_id")
        try:
            with Connection(address, timeout=5) as connection:
                return thread_id in connection.loaded_thread_ids()
        except Unreachable:
            return False

    def pending(self, agent: Agent) -> int | None:
        return None  # Codex has no queue to report; a turn either starts or does not.

    def warnings(self) -> list[str]:
        path = _unix_path(endpoint())
        if path and not os.path.exists(path):
            return [
                f"no Codex app-server at {endpoint()} — start one with "
                f"`codex app-server --listen {endpoint()}` and launch sessions "
                f"as `codex --remote {endpoint()}`"
            ]
        return []

    def deliver(self, agent: Agent, text: str, sender: str) -> str:
        thread_id = agent.address.get("thread_id")
        if not thread_id:
            raise Unreachable("no thread recorded — re-run `abq join` in that session")
        address = agent.address.get("endpoint", endpoint())

        with Connection(address) as connection:
            loaded = connection.loaded_thread_ids()
            if thread_id not in loaded:
                raise Unreachable(
                    "the app-server does not have that thread loaded — the session "
                    "probably ended"
                )
            started = connection.start_turn(thread_id, text)
            connection.drain()
        turn = started.get("turn") or {}
        return str(turn.get("id") or uuid.uuid4())
