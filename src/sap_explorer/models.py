"""Observações e transições sem referências COM vivas."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .events import SapActionEvent
from .fingerprint import ScreenFingerprint, fingerprint_screen
from .screen_reader import SapScreenState


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid4().hex


@dataclass(frozen=True)
class SapScreenObservation:
    state: SapScreenState
    id: str = field(default_factory=new_id)
    timestamp: str = field(default_factory=utc_now)

    @property
    def fingerprint(self) -> ScreenFingerprint:
        return fingerprint_screen(self.state)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "timestamp": self.timestamp,
                "fingerprint": self.fingerprint.to_dict(), "state": self.state.to_dict()}


@dataclass(frozen=True)
class SapTransition:
    before_state: SapScreenObservation | None
    actions: tuple[SapActionEvent, ...]
    after_state: SapScreenObservation | None
    action_ids: tuple[str, ...] = ()
    status: str = "complete"
    reason: str = "end_request"
    id: str = field(default_factory=new_id)
    timestamp: str = field(default_factory=utc_now)
    batch: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "timestamp": self.timestamp,
            "before_state": None if self.before_state is None else self.before_state.to_dict(),
            "after_state": None if self.after_state is None else self.after_state.to_dict(),
            "actions": [action.to_dict() for action in self.actions],
            "action_ids": list(self.action_ids), "status": self.status, "reason": self.reason,
            "batch": self.batch,
            "status_bar": None if self.after_state is None else self.after_state.state.to_dict()["status_bar"],
        }
