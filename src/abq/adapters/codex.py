"""Codex adapter.

Transport: the Codex app-server, spoken as JSON-RPC 2.0 over a WebSocket. A
message becomes a real user turn in the target thread via `turn/start`, and it
renders in an attached TUI exactly like something the human typed.

The one structural constraint comes from Codex itself: a plain `codex` TUI runs
its agent in-process and is invisible to any app-server, so it cannot be joined
after the fact. A session has to be started against a shared app-server. ABQ
prefers Codex's standard local app-server socket when it exists, while retaining
the original ABQ socket as a compatibility fallback. Lifecycle remains outside
the transport; one provider-owned way to create the socket is:

    codex app-server daemon bootstrap
    codex --remote unix://~/.codex/app-server-control/app-server-control.sock

Verified against codex-cli 0.145.0-alpha.29 through 0.149.0. See
COMPATIBILITY.md.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import time
import uuid
from collections import deque

from ..paths import abq_home
from ..registry import Agent
from .base import Unreachable
from .websocket import WebSocket, WebSocketError

CLIENT_NAME = "abq"
REQUEST_TIMEOUT_SEC = 20.0
#: How long to stay connected after handing over a turn, answering whatever the
#: server asks us. Leaving immediately can strand a turn that needs an answer.
GRACE_SEC = 1.5
# Notifications stay immediate because the socket wakes as soon as a frame
# arrives.  Only the restartable `thread/read` fallback uses this interval; a
# shorter value repeatedly downloads a growing turn while nothing changed.
TURN_POLL_SEC = 5.0
TERMINAL_TURN_STATUSES = {"completed", "interrupted", "failed"}

# Current v2 server requests have different response enums from the legacy
# approval methods.  Keep the exact wire values here instead of treating every
# method containing "approval" as interchangeable.
DECLINE_RESPONSES = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "execCommandApproval": {"decision": "abort"},
    "applyPatchApproval": {"decision": "abort"},
}


class ReceiveTimeout(Unreachable):
    """No app-server frame arrived before a transport deadline."""


class RpcFailure(Unreachable):
    """The app-server returned a JSON-RPC error response."""

    def __init__(self, method: str, error: dict) -> None:
        self.method = method
        self.error = error
        super().__init__(f"{method} failed: {error.get('message', error)}")


def endpoint() -> str:
    """Resolve the shared Codex app-server without starting provider state.

    An explicit caller override always wins. Otherwise prefer Codex's standard
    local app-server socket when present, then retain ABQ's original socket as
    the compatibility fallback.
    """
    configured = os.environ.get("ABQ_CODEX_ENDPOINT")
    if configured:
        return configured

    standard_socket = os.path.expanduser(
        "~/.codex/app-server-control/app-server-control.sock"
    )
    try:
        if stat.S_ISSOCK(os.stat(standard_socket).st_mode):
            return f"unix://{standard_socket}"
    except OSError:
        pass

    return f"unix://{os.path.join(abq_home(), 'codex.sock')}"


def _unix_path(address: str) -> str | None:
    if address.startswith("unix://"):
        return os.path.expanduser(address[len("unix://") :])
    return None


class Connection:
    """One short-lived app-server session."""

    def __init__(self, address: str, timeout: float = REQUEST_TIMEOUT_SEC) -> None:
        self.address = address
        self._next_id = 0
        self.notifications: deque[dict] = deque()
        self._completed_turns: dict[tuple[str, str], dict] = {}
        self.server_requests: list[dict] = []
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
                "clientInfo": {"name": CLIENT_NAME, "title": "abq bridge", "version": "0.1.1"},
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
                    raise RpcFailure(method, error)
                return message.get("result") or {}
            self._handle_inbound(message)

    def _receive(self, deadline: float) -> dict:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReceiveTimeout("timed out waiting for the Codex app-server")
        self._ws.settimeout(remaining)
        try:
            raw = self._ws.receive()
        except (socket.timeout, TimeoutError) as exc:
            raise ReceiveTimeout("timed out waiting for the Codex app-server") from exc
        except (OSError, WebSocketError) as exc:
            raise Unreachable(f"app-server connection lost: {exc}") from exc
        if raw is None:
            raise Unreachable("app-server closed the connection")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _handle_inbound(self, message: dict) -> None:
        """Queue notifications and fail closed on server requests.

        The server blocks the turn while a request of its own is outstanding,
        so silence here would stall the very message we just delivered.
        """
        method, request_id = message.get("method"), message.get("id")
        if method is None:
            return
        if request_id is None:
            self.notifications.append(message)
            if method == "turn/completed":
                params = message.get("params") or {}
                turn = params.get("turn") or {}
                thread_id, turn_id = params.get("threadId"), turn.get("id")
                if thread_id and turn_id:
                    self._completed_turns[(thread_id, turn_id)] = turn
            return
        self.server_requests.append(
            {"method": method, "params": message.get("params") or {}}
        )
        if method == "currentTime/read":
            result = {"currentTimeAt": int(time.time())}
        elif method in DECLINE_RESPONSES:
            # abq transports the request but has no authority to approve it.
            result = DECLINE_RESPONSES[method]
        elif method == "item/permissions/requestApproval":
            # This method has no decline variant.  Granting an empty profile is
            # the fail-closed response accepted by the current schema.
            result = {"permissions": {}, "scope": "turn"}
        elif "approval" in method.lower():
            # Experimental app-server versions may add approval request
            # methods before ABQ's compatibility table is updated.  Returning
            # a decline-shaped answer may fail the turn if the new schema uses
            # another enum, but it can never grant authority implicitly.
            result = {"decision": "decline"}
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
        return self.start_turn_with_params(
            thread_id, {"input": [{"type": "text", "text": text}]}
        )

    def start_thread(self, params: dict) -> dict:
        return self.request("thread/start", params)

    def resume_thread(self, thread_id: str, params: dict | None = None) -> dict:
        body = dict(params or {})
        body["threadId"] = thread_id
        return self.request("thread/resume", body)

    def read_thread(self, thread_id: str, include_turns: bool = True) -> dict:
        return self.request(
            "thread/read", {"threadId": thread_id, "includeTurns": include_turns}
        )

    def start_turn_with_params(self, thread_id: str, params: dict) -> dict:
        body = dict(params)
        supplied = body.get("threadId")
        if supplied is not None and supplied != thread_id:
            raise ValueError("turn params contain a different threadId")
        body["threadId"] = thread_id
        return self.request("turn/start", body)

    def interrupt_turn(self, thread_id: str, turn_id: str) -> dict:
        return self.request(
            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
        )

    def delete_thread(self, thread_id: str) -> dict:
        return self.request("thread/delete", {"threadId": thread_id})

    def _completed_notification(self, thread_id: str, turn_id: str) -> dict | None:
        matched = self._completed_turns.pop((thread_id, turn_id), None)
        if matched is None:
            return None
        kept: deque[dict] = deque()
        while self.notifications:
            notification = self.notifications.popleft()
            params = notification.get("params") or {}
            turn = params.get("turn") or {}
            if (
                notification.get("method") == "turn/completed"
                and params.get("threadId") == thread_id
                and turn.get("id") == turn_id
            ):
                continue
            else:
                kept.append(notification)
        self.notifications = kept
        return matched

    def _read_turn_if_terminal(self, thread_id: str, turn_id: str) -> dict | None:
        response = self.read_thread(thread_id, include_turns=True)
        for turn in (response.get("thread") or {}).get("turns") or []:
            if turn.get("id") != turn_id:
                continue
            if turn.get("status") in TERMINAL_TURN_STATUSES:
                return turn
            return None
        # `turn/start` can answer before the new turn is visible to
        # `thread/read`; absence is therefore a transient state, not proof that
        # the caller supplied a bad id.
        return None

    def wait_turn(self, thread_id: str, turn_id: str, timeout: float) -> dict:
        """Wait for a terminal turn, preserving unrelated notifications.

        Notifications give the lowest latency on the connection that started a
        turn.  Periodic `thread/read` makes the wait restartable from a new ABQ
        process after its previous tool call timed out.
        """
        if timeout < 0:
            raise ValueError("turn timeout cannot be negative")
        deadline = time.monotonic() + timeout
        while True:
            completed = self._completed_notification(thread_id, turn_id)
            if completed is not None:
                return completed

            try:
                terminal = self._read_turn_if_terminal(thread_id, turn_id)
            except RpcFailure as exc:
                # Ephemeral threads cannot be read on current Codex versions;
                # their initiating connection still receives notifications.
                if exc.method != "thread/read":
                    raise
                terminal = None
            if terminal is not None:
                return terminal

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Unreachable(
                    f"timed out waiting for turn {turn_id!r} in thread {thread_id!r}"
                )
            receive_deadline = time.monotonic() + min(TURN_POLL_SEC, remaining)
            try:
                self._handle_inbound(self._receive(receive_deadline))
            except ReceiveTimeout:
                pass


def agent_messages(turn: dict) -> list[str]:
    """Extract complete agent-message items without assigning them semantics."""
    return [
        item.get("text", "")
        for item in turn.get("items") or []
        if item.get("type") == "agentMessage" and isinstance(item.get("text"), str)
    ]


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
