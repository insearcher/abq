"""Resumable one-shot return addresses backed by a local file spool.

The token is only an address.  What a recipient should return, when it should
return it, and what the caller does with the payload all belong to the calling
skill.  A pending address expires after its configured TTL.  A payload
published before that deadline receives a fresh retention window of the same
length, so a late publisher cannot lose its result at the original deadline.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
import uuid
from contextlib import contextmanager

from .paths import returns_path
from .util import FileLock

DEFAULT_TTL_SEC = 3600.0
MAX_PAYLOAD_BYTES = 1024 * 1024
POLL_INTERVAL_SEC = 0.1
TOKEN_RE = re.compile(r"^[0-9a-f]{48}$")


class ReturnChannelError(RuntimeError):
    """The return address is invalid, expired, closed, or already fulfilled."""


class ReturnPending(ReturnChannelError):
    """No payload arrived before the requested wait ended."""


def _root() -> str:
    root = returns_path()
    os.makedirs(root, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def _channel_path(token: str) -> str:
    if not TOKEN_RE.fullmatch(token):
        raise ReturnChannelError("invalid return token")
    return os.path.join(_root(), token)


@contextmanager
def _channel_lock(channel: str, timeout: float = 3.0):
    try:
        with FileLock(channel, timeout=timeout):
            yield
    except TimeoutError as exc:
        raise ReturnChannelError("return channel is busy") from exc


def _read_meta(channel: str) -> dict:
    try:
        with open(os.path.join(channel, "meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise ReturnChannelError("return channel does not exist or is corrupt") from exc
    if not isinstance(meta, dict):
        raise ReturnChannelError("return channel does not exist or is corrupt")
    return meta


def _read_result(channel: str) -> dict | None:
    try:
        with open(os.path.join(channel, "result.json"), encoding="utf-8") as fh:
            record = json.load(fh)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        raise ReturnChannelError("return payload is corrupt") from exc
    if not isinstance(record, dict) or not isinstance(record.get("text"), str):
        raise ReturnChannelError("return payload has an invalid shape")
    return record


def _ttl(meta: dict) -> float:
    try:
        configured = float(meta.get("ttl_seconds"))
    except (TypeError, ValueError):
        try:
            configured = float(meta["expires_at"]) - float(meta["created_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ReturnChannelError("return channel metadata is corrupt") from exc
    if configured <= 0:
        raise ReturnChannelError("return channel metadata is corrupt")
    return configured


def _expires_at(meta: dict, record: dict | None) -> float:
    try:
        if record is not None and record.get("expires_at") is None:
            return float(record["published_at"]) + _ttl(meta)
        return float((record or meta).get("expires_at", 0))
    except (KeyError, TypeError, ValueError) as exc:
        raise ReturnChannelError("return channel metadata is corrupt") from exc


def _expire_if_needed(channel: str, meta: dict, record: dict | None, now: float) -> None:
    if _expires_at(meta, record) <= now:
        shutil.rmtree(channel, ignore_errors=True)
        raise ReturnChannelError("return channel expired")


def cleanup_expired(now: float | None = None) -> int:
    """Remove expired channel directories and return the number removed."""
    now = time.time() if now is None else now
    removed = 0
    root = _root()
    try:
        names = os.listdir(root)
    except FileNotFoundError:
        return 0
    for name in names:
        if not TOKEN_RE.fullmatch(name):
            continue
        channel = os.path.join(root, name)
        try:
            with _channel_lock(channel, timeout=0.2):
                meta = _read_meta(channel)
                record = _read_result(channel)
                if _expires_at(meta, record) <= now:
                    shutil.rmtree(channel, ignore_errors=True)
                    removed += 1
        except ReturnChannelError:
            continue
    return removed


def open_channel(ttl: float = DEFAULT_TTL_SEC) -> str:
    if ttl <= 0:
        raise ReturnChannelError("return channel TTL must be positive")
    cleanup_expired()
    root = _root()
    while True:
        token = secrets.token_hex(24)
        channel = os.path.join(root, token)
        try:
            os.mkdir(channel, 0o700)
            break
        except FileExistsError:
            continue
    now = time.time()
    meta_path = os.path.join(channel, "meta.json")
    fd = os.open(meta_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "version": 1,
                "created_at": now,
                "expires_at": now + ttl,
                "ttl_seconds": ttl,
            },
            fh,
        )
        fh.flush()
        os.fsync(fh.fileno())
    return token


def send(token: str, text: str) -> str:
    payload = text.encode("utf-8")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ReturnChannelError(
            f"return payload exceeds {MAX_PAYLOAD_BYTES} bytes"
        )
    channel = _channel_path(token)
    with _channel_lock(channel):
        meta = _read_meta(channel)
        now = time.time()
        existing = _read_result(channel)
        _expire_if_needed(channel, meta, existing, now)
        if existing is not None:
            raise ReturnChannelError("return channel already has a payload")
        result_path = os.path.join(channel, "result.json")
        temp_path = os.path.join(
            channel, f"result.tmp-{os.getpid()}-{uuid.uuid4().hex}"
        )
        record = {
            "id": str(uuid.uuid4()),
            "text": text,
            "published_at": now,
            "expires_at": now + _ttl(meta),
        }
        try:
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(record, fh, ensure_ascii=False)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.link(temp_path, result_path)
            finally:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass
        except FileExistsError as exc:
            raise ReturnChannelError("return channel already has a payload") from exc
        except OSError as exc:
            raise ReturnChannelError(f"cannot publish return payload: {exc}") from exc
    return record["id"]


def read(token: str) -> dict | None:
    channel = _channel_path(token)
    with _channel_lock(channel):
        meta = _read_meta(channel)
        record = _read_result(channel)
        _expire_if_needed(channel, meta, record, time.time())
        return record


def wait(token: str, timeout: float, poll: float = POLL_INTERVAL_SEC) -> dict:
    if timeout < 0:
        raise ReturnChannelError("wait timeout cannot be negative")
    if poll <= 0:
        raise ReturnChannelError("poll interval must be positive")
    deadline = time.monotonic() + timeout
    while True:
        record = read(token)
        if record is not None:
            return record
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReturnPending("return payload is still pending")
        time.sleep(min(poll, remaining))


def close(token: str) -> None:
    channel = _channel_path(token)
    with _channel_lock(channel):
        if not os.path.isdir(channel):
            raise ReturnChannelError("return channel does not exist")
        shutil.rmtree(channel)
