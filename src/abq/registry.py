"""Alias registry: which local agent session hides behind which name."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Iterator

from .paths import abq_home, registry_path
from .util import atomic_write_json, now_iso


@dataclass
class Agent:
    """One registered session."""

    alias: str
    provider: str
    cwd: str
    joined_at: str = field(default_factory=now_iso)
    # Provider-specific addressing. Claude: session_id, team. Codex: thread_id, endpoint.
    address: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable identity of the underlying session, for self-detection."""
        return self.address.get("session_id") or self.address.get("thread_id") or ""


class Registry:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or registry_path()
        self.agents: dict[str, Agent] = {}
        self.load()

    def load(self) -> None:
        """Read the registry, skipping entries this version cannot understand.

        The file outlives any single version of abq, so an unreadable or
        foreign entry must never take the whole tool down.
        """
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}

        fields = {f for f in Agent.__dataclass_fields__ if f != "alias"}
        self.agents = {}
        for alias, info in (raw.get("agents") or {}).items():
            if not isinstance(info, dict) or "provider" not in info:
                continue
            known = {key: value for key, value in info.items() if key in fields}
            try:
                self.agents[alias] = Agent(alias=alias, **known)
            except TypeError:
                continue

    def save(self) -> None:
        os.makedirs(abq_home(), exist_ok=True)
        payload = {
            "version": 1,
            "agents": {
                alias: {k: v for k, v in asdict(agent).items() if k != "alias"}
                for alias, agent in self.agents.items()
            },
        }
        atomic_write_json(self.path, payload)

    # -- lookup ---------------------------------------------------------

    def get(self, alias: str) -> Agent | None:
        return self.agents.get(alias)

    def by_session(self, session_key: str) -> Agent | None:
        if not session_key:
            return None
        for agent in self.agents.values():
            if agent.key == session_key:
                return agent
        return None

    def __iter__(self) -> Iterator[Agent]:
        return iter(sorted(self.agents.values(), key=lambda a: a.alias))

    def __len__(self) -> int:
        return len(self.agents)

    # -- mutation -------------------------------------------------------

    def join(self, agent: Agent) -> Agent | None:
        """Register an alias, dropping any previous alias of the same session.

        Returns the alias that was replaced, if any.
        """
        previous = self.by_session(agent.key)
        if previous is not None and previous.alias != agent.alias:
            del self.agents[previous.alias]
        self.agents[agent.alias] = agent
        self.save()
        return previous

    def leave(self, alias: str) -> bool:
        if self.agents.pop(alias, None) is None:
            return False
        self.save()
        return True
