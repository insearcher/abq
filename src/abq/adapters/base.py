"""Provider adapter contract.

Every undocumented or version-fragile detail of a coding agent lives behind this
interface, so a breaking change upstream is repaired in exactly one file.
"""

from __future__ import annotations

from typing import Protocol

from ..registry import Agent


class Unreachable(RuntimeError):
    """The target session is gone, or its transport refused the message."""


class Held(RuntimeError):
    """The message reached the session but waits for the user to approve it.

    Not a failure: the text is sitting in the receiving session, and one
    confirmation there releases it to the model.
    """

    def __init__(self, msg_id: str, hint: str = "") -> None:
        super().__init__("held pending approval in the receiving session")
        self.msg_id = msg_id
        self.hint = hint


class Adapter(Protocol):
    #: Value stored in `Agent.provider`.
    name: str

    def detect_self(self) -> dict | None:
        """Address of the session this process is running inside, if any.

        Returns the provider-specific address dict used by `deliver`, or None
        when the current process is not inside a session of this provider.
        """

    def deliver(self, agent: Agent, text: str, sender: str) -> str:
        """Push `text` into the running session. Returns a message id.

        Raises `Unreachable` if the session cannot be reached.
        """

    def is_reachable(self, agent: Agent) -> bool:
        """Cheap liveness probe used by `abq who`."""

    def pending(self, agent: Agent) -> int | None:
        """Messages queued but not yet consumed, when the provider exposes it."""

    def warnings(self) -> list[str]:
        """Local configuration that will get in the way of delivery."""
