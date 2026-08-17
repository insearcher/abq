"""A minimal RFC 6455 client, because Codex speaks WebSocket over a Unix socket.

`codex app-server --listen unix://…` looks like a plain socket but expects the
WebSocket upgrade; writing bare JSON to it gets the connection dropped with no
error frame. Nothing here is general-purpose — it does exactly what the
app-server needs (text frames, ping/pong, close) and nothing more, which keeps
abq dependency-free.
"""

from __future__ import annotations

import base64
import os
import re
import socket
import struct
import threading

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(RuntimeError):
    pass


class WebSocket:
    def __init__(self, sock: socket.socket, host: str = "localhost", path: str = "/",
                 headers: dict | None = None, timeout: float = 30.0) -> None:
        self._sock = sock
        self._sock.settimeout(timeout)
        self._buffer = b""
        self._send_lock = threading.Lock()
        self._handshake(host, path, headers or {})

    @classmethod
    def connect_unix(cls, path: str, **kwargs) -> "WebSocket":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(path)
        return cls(sock, **kwargs)

    @classmethod
    def connect_tcp(cls, host: str, port: int, **kwargs) -> "WebSocket":
        sock = socket.create_connection((host, port))
        return cls(sock, host=f"{host}:{port}", **kwargs)

    def _handshake(self, host: str, path: str, headers: dict) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        request = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            *(f"{name}: {value}" for name, value in headers.items()),
        ]
        self._sock.sendall(("\r\n".join(request) + "\r\n\r\n").encode())

        while b"\r\n\r\n" not in self._buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WebSocketError("server closed the connection during handshake")
            self._buffer += chunk
        head, self._buffer = self._buffer.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if not re.match(r"HTTP/1\.1 101", status):
            raise WebSocketError(f"handshake rejected: {status}")

    def settimeout(self, timeout: float | None) -> None:
        self._sock.settimeout(timeout)

    def _read_exactly(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed by peer")
            self._buffer += chunk
        head, self._buffer = self._buffer[:count], self._buffer[count:]
        return head

    def receive(self) -> str | None:
        """Next text message, or None when the peer closes."""
        payload = b""
        started = False
        while True:
            first, second = self._read_exactly(2)
            final = first & 0x80
            opcode = first & 0x0F
            masked = second & 0x80
            length = second & 0x7F
            if length == 126:
                (length,) = struct.unpack(">H", self._read_exactly(2))
            elif length == 127:
                (length,) = struct.unpack(">Q", self._read_exactly(8))
            mask = self._read_exactly(4) if masked else None
            data = self._read_exactly(length) if length else b""
            if mask:
                data = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))

            if opcode == OP_CLOSE:
                self._send_frame(b"", OP_CLOSE)
                return None
            if opcode == OP_PING:
                self._send_frame(data, OP_PONG)
                continue
            if opcode == OP_PONG:
                continue
            if opcode in (OP_TEXT, OP_BINARY):
                payload, started = data, True
            elif opcode == OP_CONT and started:
                payload += data
            if final and started:
                return payload.decode("utf-8", errors="replace")

    def _send_frame(self, data: bytes, opcode: int) -> None:
        header = bytearray([0x80 | opcode])
        size = len(data)
        if size < 126:
            header.append(0x80 | size)
        elif size < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", size)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", size)
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
        with self._send_lock:
            self._sock.sendall(bytes(header) + masked)

    def send(self, text: str) -> None:
        self._send_frame(text.encode("utf-8"), OP_TEXT)

    def close(self) -> None:
        try:
            self._send_frame(b"", OP_CLOSE)
        except OSError:
            pass
        finally:
            self._sock.close()
