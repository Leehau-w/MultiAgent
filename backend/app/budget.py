"""Per-project budget tracker used by the orchestrator.

The workflow.yaml ``budget:`` block caps total cost, turns, wall-clock
minutes, and concurrent agents for a project. A ``BudgetTracker`` lives
on each :class:`Project` and is consulted **before** every agent start
and updated **after** every turn. When a cap is hit the tracker flips
into a sticky "exceeded" state — new agent starts are refused and the
orchestrator broadcasts ``budget_exceeded`` so the UI can halt.

Design notes
------------
* Caps are pulled live from ``workflow.yaml`` via :func:`load_workflow`
  so edits take effect without restarting the backend.
* The tracker does not own a clock it can reset on demand. Wall-clock
  is measured from :meth:`start`; callers should invoke :meth:`start`
  when a workflow (or pipeline) run begins. If not called the tracker
  simply skips wall-clock checks.
* ``cost`` and ``turns`` accumulate across manual ``start_agent`` calls
  too — treating a project's lifetime as "one run" is pragmatic for
  now; a future iteration can reset counters per workflow run if the
  distinction matters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from .workflow import Budget, load_workflow

if TYPE_CHECKING:
    from .project import Project

logger = logging.getLogger(__name__)


@dataclass
class BudgetUsage:
    cost_usd: float = 0.0
    turns: int = 0
    concurrent_peak: int = 0
    started_at: datetime | None = None

    def wall_clock_minutes(self) -> float:
        if self.started_at is None:
            return 0.0
        return (datetime.now() - self.started_at).total_seconds() / 60.0


class BudgetExceeded(Exception):
    """Raised by :meth:`BudgetTracker.check_can_start` when a start
    would cross a limit. Carries the field that triggered the block
    (``cost`` | ``turns`` | ``wall_clock`` | ``concurrent``)."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class BudgetTracker:
    def __init__(self, project: "Project") -> None:
        self._project = project
        self.usage = BudgetUsage()
        self._exceeded: tuple[str, str] | None = None

    # -------- cap lookup --------

    def _caps(self) -> Budget | None:
        wf = load_workflow(self._project.workspace_dir)
        return wf.budget if wf else None

    # -------- lifecycle --------

    def start(self) -> None:
        """Mark the beginning of a run so wall-clock checks have a
        reference point. Resets only the clock — cumulative cost /
        turns survive so users can see what's been spent."""
        self.usage.started_at = datetime.now()
        self._exceeded = None

    def reset(self) -> None:
        """Fully zero out the tracker. Called when the user clears the
        project, not routinely."""
        self.usage = BudgetUsage()
        self._exceeded = None

    # -------- read-side --------

    @property
    def exceeded(self) -> bool:
        return self._exceeded is not None

    @property
    def exceeded_reason(self) -> str | None:
        return self._exceeded[0] if self._exceeded else None

    @property
    def exceeded_detail(self) -> str | None:
        return self._exceeded[1] if self._exceeded else None

    def snapshot(self) -> dict:
        """Return a serializable snapshot for the UI budget bar."""
        caps = self._caps()
        caps_dump = caps.model_dump(exclude_none=True) if caps else {}
        concurrent = sum(
            1 for a in self._project.agents.values()
            if a.status.value == "running"
        )
        return {
            "caps": caps_dump,
            "usage": {
                "cost_usd": round(self.usage.cost_usd, 6),
                "turns": self.usage.turns,
                "wall_clock_min": round(self.usage.wall_clock_minutes(), 2),
                "concurrent": concurrent,
                "concurrent_peak": self.usage.concurrent_peak,
            },
            "exceeded": self.exceeded,
            "exceeded_reason": self.exceeded_reason,
            "exceeded_detail": self.exceeded_detail,
        }

    # -------- pre-start gate --------

    def check_can_start(self) -> None:
        """Raise :class:`BudgetExceeded` if adding one more running
        agent would cross a cap. Called from :meth:`Project.start_agent`."""
        # Cumulative check first — it may clear a stale sticky flag if
        # the user raised the cap in workflow.yaml since the last trip.
        self._check_cumulative()  # cost / turns / wall-clock

        if self._exceeded:
            raise BudgetExceeded(*self._exceeded)

        caps = self._caps()
        if caps is None:
            return
        if caps.max_concurrent_agents is not None:
            running = sum(
                1 for a in self._project.agents.values()
                if a.status.value == "running"
            )
            if running >= caps.max_concurrent_agents:
                # Concurrency is a soft gate — don't mark sticky-exceeded,
                # just refuse this particular start so the user can wait
                # for a slot.
                raise BudgetExceeded(
                    "concurrent",
                    f"concurrent {running} >= cap {caps.max_concurrent_agents}",
                )

    def _check_cumulative(self) -> None:
        """Evaluate cost / turns / wall-clock caps against the current
        workflow.yaml. Sets the sticky-exceeded flag when a cap is
        crossed, and clears it when the tripping condition no longer
        holds (e.g. the user raised the cap mid-run, or usage was
        reset). The flag is never cleared to stale data: we re-check
        every cap against current usage before concluding "not exceeded".
        """
        caps = self._caps()
        if caps is None:
            # No caps configured at all — nothing to enforce, so a
            # previous trip is no longer meaningful.
            self._exceeded = None
            return

        trip: tuple[str, str] | None = None
        if caps.max_total_cost_usd is not None and self.usage.cost_usd >= caps.max_total_cost_usd:
            trip = (
                "cost",
                f"cost ${self.usage.cost_usd:.4f} >= cap ${caps.max_total_cost_usd}",
            )
        elif caps.max_total_turns is not None and self.usage.turns >= caps.max_total_turns:
            trip = (
                "turns",
                f"turns {self.usage.turns} >= cap {caps.max_total_turns}",
            )
        elif caps.max_wall_clock_min is not None and self.usage.started_at is not None:
            mins = self.usage.wall_clock_minutes()
            if mins >= caps.max_wall_clock_min:
                trip = (
                    "wall_clock",
                    f"wall-clock {mins:.1f}m >= cap {caps.max_wall_clock_min}m",
                )

        if trip is None:
            # No cap currently tripped — clear any stale sticky flag so a
            # user who raised the cap in workflow.yaml sees the change
            # take effect without needing to reset the tracker.
            self._exceeded = None
        else:
            # First time we notice the trip, raise so the caller can
            # broadcast ``budget_exceeded``. If the flag was already
            # set for the same reason, update the detail to the latest
            # numbers but still raise — the cap is still being violated.
            self._exceeded = trip
            raise BudgetExceeded(*trip)

    # -------- post-turn update --------

    def record_turn(self, cost_delta: float, turn_delta: int = 1) -> bool:
        """Accumulate usage after a turn completes. Returns ``True``
        iff this update tripped a cumulative cap — the caller is
        expected to broadcast ``budget_exceeded``. Concurrency caps
        are NOT evaluated here; those belong to :meth:`check_can_start`.
        """
        self.usage.cost_usd += max(cost_delta, 0.0)
        self.usage.turns += turn_delta
        running = sum(
            1 for a in self._project.agents.values()
            if a.status.value == "running"
        )
        if running > self.usage.concurrent_peak:
            self.usage.concurrent_peak = running

        if self._exceeded:
            return False  # already tripped — don't double-broadcast
        try:
            self._check_cumulative()
        except BudgetExceeded:
            return True
        return False


__all__ = ["Budget", "BudgetExceeded", "BudgetTracker", "BudgetUsage"]
