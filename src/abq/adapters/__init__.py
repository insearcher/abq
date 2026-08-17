"""Provider adapters and provider detection."""

from __future__ import annotations

from .base import Adapter, Held, Unreachable
from .claude import ClaudeAdapter, ManagedClaude
from .codex import CodexAdapter, Connection

ADAPTERS: dict[str, Adapter] = {
    ClaudeAdapter.name: ClaudeAdapter(),
    CodexAdapter.name: CodexAdapter(),
}

#: Detection order matters: a Codex session started from a Claude session would
#: otherwise inherit Claude's environment and be misidentified.
DETECTION_ORDER = (CodexAdapter.name, ClaudeAdapter.name)


def detect_current_session() -> tuple[str, dict] | None:
    """Identify the agent session this process is running inside.

    Returns `(provider, address)` or None when called from a plain shell.
    """
    for name in DETECTION_ORDER:
        address = ADAPTERS[name].detect_self()
        if address:
            return name, address
    return None


__all__ = [
    "ADAPTERS",
    "Adapter",
    "ClaudeAdapter",
    "CodexAdapter",
    "Connection",
    "Held",
    "ManagedClaude",
    "Unreachable",
    "detect_current_session",
]
