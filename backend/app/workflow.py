"""Declarative pipeline definitions stored per-project as ``workflow.yaml``.

The pipeline orchestrator defaults to a hardcoded four-stage pipeline
(pm → td → developer×2 → reviewer). Track B adds a declarative escape
hatch: a project can ship a ``workflow.yaml`` in its workspace and the
orchestrator will pick it up automatically. The UI can read/write the
same file so users never have to leave the app.

Schema (v1)::

    version: 1          # optional, defaults to 1
    stages:
      - name: analysis
        agents: [pm]
        parallel: false  # optional, defaults to false
      - name: implementation
        agents: [developer, developer]
        parallel: true

Parsing is lenient but structured: unknown top-level keys are ignored so
the file can grow without breaking older backends, but each stage must
have a non-empty name and at least one agent role.
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from .models import PipelineStage

if TYPE_CHECKING:
    from .events import Event

logger = logging.getLogger(__name__)

WORKFLOW_FILENAME = "workflow.yaml"

# ``agent.event`` — e.g. ``pm.completed`` or ``dev_backend.error``.
_TRIGGER_RE = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*\.\s*([a-z_]+)\s*$")


class CoordinatorConfig(BaseModel):
    """Per-project coordinator settings declared in ``workflow.yaml``."""

    enabled: bool = False
    role_id: str = "coordinator"
    allow_spawn: bool = False
    max_spawned_agents: int = 5


class Trigger(BaseModel):
    """One declarative rule. Shape::

        - on: pm.completed              # single predicate, or list for AND-join
          start: td                     # or a list of agent ids / role ids
          context_from: [pm]            # optional
          decide: coordinator           # mutually exclusive with start
    """

    on: list[str] = Field(default_factory=list)
    start: list[str] = Field(default_factory=list)
    context_from: list[str] = Field(default_factory=list)
    decide: str | None = None

    @field_validator("on", mode="before")
    @classmethod
    def _coerce_on(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return list(v)

    @field_validator("start", mode="before")
    @classmethod
    def _coerce_start(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return list(v)

    @field_validator("context_from", mode="before")
    @classmethod
    def _coerce_context_from(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return list(v)


class Budget(BaseModel):
    """Upper limits for one project's pipeline run. Each field is
    optional — ``None`` means unlimited. The orchestrator consults
    these before starting any new agent and broadcasts
    ``budget_exceeded`` when the first limit is hit.
    """

    max_total_cost_usd: float | None = None
    max_total_turns: int | None = None
    max_wall_clock_min: int | None = None
    max_concurrent_agents: int | None = None
    # How many times the coordinator can request_rework on the same stage
    # before the orchestrator stops auto-retrying and escalates via
    # [STAGE_RETRY_EXHAUSTED]. ``None`` → let the orchestrator pick a
    # sensible default (currently 3 when stage-gate is active).
    max_stage_retries: int | None = None


class Workflow(BaseModel):
    """A parsed ``workflow.yaml``.

    ``stages`` is the same ``PipelineStage`` shape the orchestrator
    already consumes, so the loader is a thin translation layer with no
    new runtime concepts. ``budget`` is optional; absence = no limits.
    ``triggers`` and ``coordinator`` drive the reactive workflow engine
    — stages are still the canonical source of truth for default runs,
    and triggers add the event-driven dispatch on top.
    """

    version: int = 1
    stages: list[PipelineStage] = Field(default_factory=list)
    budget: Budget | None = None
    coordinator: CoordinatorConfig | None = None
    triggers: list[Trigger] = Field(default_factory=list)

    @field_validator("stages")
    @classmethod
    def _non_empty(cls, v: list[PipelineStage]) -> list[PipelineStage]:
        if not v:
            raise ValueError("workflow.yaml must define at least one stage")
        for s in v:
            if not s.name.strip():
                raise ValueError("stage name cannot be empty")
            if not s.agents:
                raise ValueError(f"stage {s.name!r} must list at least one agent")
        return v


def workflow_path(workspace_dir: str) -> str:
    """Absolute path to this project's ``workflow.yaml``."""
    return os.path.join(workspace_dir, WORKFLOW_FILENAME)


def load_workflow(workspace_dir: str) -> Workflow | None:
    """Read ``workspace_dir/workflow.yaml`` and return a parsed ``Workflow``.

    Returns ``None`` if the file is absent, empty, or malformed — the
    caller is expected to fall back to the built-in default pipeline in
    that case. Parse errors are logged but do not raise, so a broken
    file can never wedge the backend.
    """
    path = workflow_path(workspace_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("workflow.yaml at %s unreadable: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("workflow.yaml at %s is not a mapping; ignoring", path)
        return None
    try:
        return Workflow(**raw)
    except ValidationError as exc:
        logger.warning("workflow.yaml at %s rejected: %s", path, exc)
        return None


def save_workflow(workspace_dir: str, wf: Workflow) -> str:
    """Write *wf* to ``workspace_dir/workflow.yaml`` and return the path.

    The write is atomic: we stage to a ``.tmp`` sibling and replace so
    a crash mid-write can't leave a half-written file.
    """
    os.makedirs(workspace_dir, exist_ok=True)
    path = workflow_path(workspace_dir)
    data: dict[str, Any] = {
        "version": wf.version,
        "stages": [s.model_dump(exclude_none=True) for s in wf.stages],
    }
    if wf.budget is not None:
        data["budget"] = wf.budget.model_dump(exclude_none=True)
    if wf.coordinator is not None:
        data["coordinator"] = wf.coordinator.model_dump()
    if wf.triggers:
        data["triggers"] = [
            t.model_dump(exclude_defaults=True, exclude_none=True) for t in wf.triggers
        ]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)
    return path


def delete_workflow(workspace_dir: str) -> bool:
    """Remove ``workflow.yaml`` if present. Returns True iff a file was deleted."""
    path = workflow_path(workspace_dir)
    if not os.path.isfile(path):
        return False
    os.remove(path)
    return True


# --------------------------------------------------------------------------- #
#   Trigger matching                                                           #
# --------------------------------------------------------------------------- #


def _predicate_matches(predicate: str, event: "Event") -> bool:
    """Does ``predicate`` (shape ``agent.event``) describe *event*?"""
    m = _TRIGGER_RE.match(predicate)
    if not m:
        logger.warning("invalid trigger predicate %r — ignoring", predicate)
        return False
    agent, kind = m.group(1), m.group(2)
    if event.agent != agent:
        return False
    # Predicate shorthand ``completed`` / ``error`` → full kinds.
    event_kind = event.kind.removeprefix("agent_")
    return event_kind == kind


def _all_predicates_satisfied(
    predicates: list[str], event: "Event", completed_agents: set[str]
) -> bool:
    """For AND-joins: every predicate must be satisfied, either by the
    current event or by the historical completion set. The event itself
    must match at least one predicate — otherwise a completely unrelated
    event would re-fire an AND-join trigger every time both sides were
    already completed."""
    if not predicates:
        return False
    current_matches = [p for p in predicates if _predicate_matches(p, event)]
    if not current_matches:
        return False
    for pred in predicates:
        if pred in current_matches:
            continue
        m = _TRIGGER_RE.match(pred)
        if not m:
            return False
        agent, kind = m.group(1), m.group(2)
        if kind != "completed":
            # Only ``completed`` participates in AND-joins — errors /
            # user-messages are point-in-time and don't carry forward.
            return False
        if agent not in completed_agents:
            return False
    return True


class TriggerAction(BaseModel):
    """One dispatch instruction produced by :func:`match_triggers`.

    Exactly one of ``start_agents`` / ``decide`` is populated. The
    orchestrator picks the right handler based on which field is set.
    """

    start_agents: list[str] = Field(default_factory=list)
    context_from: list[str] = Field(default_factory=list)
    decide: str | None = None
    trigger_index: int = 0


def match_triggers(
    workflow: Workflow,
    event: "Event",
    completed_agents: set[str],
) -> list[TriggerAction]:
    """Return every trigger whose predicate is satisfied by *event*.

    First-match-wins semantics are layered on by the caller (orchestrator)
    so we can inspect the full list for diagnostics. AND-joins consult
    *completed_agents* so a late-arriving second leg of a fan-in fires
    the join even though only one leg emits the triggering event.
    """
    out: list[TriggerAction] = []
    for idx, trig in enumerate(workflow.triggers):
        if not _all_predicates_satisfied(trig.on, event, completed_agents):
            continue
        if trig.decide:
            out.append(TriggerAction(
                decide=trig.decide,
                context_from=list(trig.context_from),
                trigger_index=idx,
            ))
        else:
            out.append(TriggerAction(
                start_agents=list(trig.start),
                context_from=list(trig.context_from),
                trigger_index=idx,
            ))
    return out
