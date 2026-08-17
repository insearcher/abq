"""Filesystem locations abq reads and writes.

These are functions rather than constants so that `ABQ_HOME` and the providers'
own environment variables are honoured whenever they change, not only at import.
"""

from __future__ import annotations

import os


def abq_home() -> str:
    return os.path.expanduser(os.environ.get("ABQ_HOME", "~/.abq"))


def registry_path() -> str:
    return os.path.join(abq_home(), "registry.json")


def history_path() -> str:
    return os.path.join(abq_home(), "history.jsonl")


def claude_home() -> str:
    return os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))


def codex_home() -> str:
    return os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")
