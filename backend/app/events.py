"""In-process event queue feeding the workflow trigger matcher.

Every agent lifecycle transition fires an :class:`Event` on the owning
project's queue. The orchestrator drains the queue after each turn and
runs the trigger matcher (:func:`app.workflow.match_triggers`) — if a
rule fires, it kicks off the next agent; if the rule hands off to the
coordinator, the coordinator's ``get_inbox`` tool exposes the recent
tail so the LLM can reason about what happened.

This module is intentionally tiny: events are plain dataclasses, the
queue is a deque, and there is no async machinery. The orchestrator
does the coordination; events are just the record.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


EventKind = Literal[
    "agent_completed",
    "agent_error",
    "user_message",
    "workflow_started",
    "workflow_completed",
    "budget_exceeded",
    "pipeline_started",
    "stage_completed",
]


@dataclass
class Event:
    """One entry in a project's event log.

    ``agent`` is set when the event is scoped to a specific worker;
    ``detail`` holds event-specific free-form fields (error ids, user
    prompt text, trigger names, etc.).
    """

    kind: EventKind
    agent: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "agent": self.agent,
            "ts": self.ts.isoformat(),
            "detail": self.detail,
        }


class EventQueue:
    """Bounded ring buffer plus an append-only completion set.

    Events decay after ``maxlen`` entries — callers reading ``tail`` get
    the most recent slice. The ``completed`` set is *not* pruned: the
    trigger matcher needs it to evaluate AND-join rules like
    ``on: [dev_backend.completed, dev_frontend.completed]`` even if
    hundreds of events have rolled through in between.
    """

    def __init__(self, maxlen: int = 500) -> None:
        self._events: deque[Event] = deque(maxlen=maxlen)
        self._completed: set[str] = set()

    # ----- write side -----

    def push(self, event: Event) -> None:
        self._events.append(event)
        if event.kind == "agent_completed" and event.agent:
            self._completed.add(event.agent)

    def clear_completed(self, agent_id: str) -> None:
        """Mark an agent as no longer completed — called when it's
        restarted so AND-joins on the next run don't fire immediately."""
        self._completed.discard(agent_id)

    # ----- read side -----

    def tail(self, limit: int = 20) -> list[Event]:
        if limit <= 0:
            return []
        if limit >= len(self._events):
            return list(self._events)
        return list(self._events)[-limit:]

    def completed_agents(self) -> set[str]:
        return set(self._completed)

    def __len__(self) -> int:
        return len(self._events)


__all__ = ["Event", "EventKind", "EventQueue"]
