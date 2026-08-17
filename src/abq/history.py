"""Durable transcript of everything abq delivered.

Provider inboxes are ephemeral by design (Claude Code deletes a message once the
session picks it up, and the team directory dies with the session), so this file
is the only lasting record of a cross-agent conversation.
"""

from __future__ import annotations

import json
import os
import time
from typing import Iterable, Iterator

from .paths import abq_home, history_path
from .util import now_iso


def append(record: dict, path: str | None = None) -> None:
    path = path or history_path()
    os.makedirs(abq_home(), exist_ok=True)
    record.setdefault("ts", now_iso())
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def read(limit: int | None = None, path: str | None = None) -> list[dict]:
    path = path or history_path()
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return []
    if limit is not None:
        lines = lines[-limit:]
    return [json.loads(line) for line in lines if line.strip()]


def follow(path: str | None = None, poll: float = 0.5) -> Iterator[dict]:
    """Yield records as they are appended. Runs until interrupted."""
    path = path or history_path()
    position = os.path.getsize(path) if os.path.exists(path) else 0
    while True:
        if os.path.exists(path) and os.path.getsize(path) > position:
            with open(path, encoding="utf-8") as fh:
                fh.seek(position)
                for line in fh:
                    if line.strip():
                        yield json.loads(line)
                position = fh.tell()
        time.sleep(poll)


def format_line(record: dict, color: bool = True) -> str:
    stamp = record.get("ts", "")[11:19]
    sender = record.get("from", "?")
    target = record.get("to", "?")
    text = (record.get("text") or "").replace("\n", " ")
    if not color:
        return f"{stamp} {sender} -> {target}  {text}"
    return (
        f"\033[2m{stamp}\033[0m \033[36m{sender}\033[0m -> "
        f"\033[33m{target}\033[0m  {text}"
    )


def render(records: Iterable[dict], color: bool = True) -> str:
    return "\n".join(format_line(record, color) for record in records)
