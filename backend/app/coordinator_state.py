"""Structured, externalized memory for the coordinator role.

The coordinator is re-invoked fresh on every workflow event. Carrying
reasoning between invocations through long SDK sessions doesn't scale —
the window balloons to hundreds of thousands of tokens with redundant
transcripts. Instead, memory lives in four structured blocks on disk:

* ``facts``          — append-only, timestamped observations
* ``hypothesis``     — one freeform paragraph, overwritten each turn
* ``open_questions`` — mutable checklist of unresolved items
* ``decisions``      — append-only, timestamped choices + rationale

Each invocation receives the full state as context and returns an update.
``apply_update`` merges those semantics: lists append, freeform fields
replace. A YAML file per project (``coordinator_state.yaml``) holds the
state so users can inspect and hand-edit it to nudge the coordinator.

Schema versioning: ``version: 1`` is written on every save. Future
migrations should read the version and transform up on load.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

STATE_FILENAME = "coordinator_state.yaml"
CURRENT_VERSION = 1


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class FactEntry(BaseModel):
    ts: datetime = Field(default_factory=_now_utc)
    kind: str
    agent: str | None = None
    summary: str


class DecisionEntry(BaseModel):
    ts: datetime = Field(default_factory=_now_utc)
    decision: str
    rationale: str | None = None


class CoordinatorState(BaseModel):
    """Serialized shape of ``coordinator_state.yaml``."""

    version: int = CURRENT_VERSION
    facts: list[FactEntry] = Field(default_factory=list)
    hypothesis: str = ""
    open_questions: list[str] = Field(default_factory=list)
    decisions: list[DecisionEntry] = Field(default_factory=list)


class StateUpdate(BaseModel):
    """Delta the coordinator hands back via ``update_state`` tool.

    Semantics:
    * ``facts_append`` / ``decisions_append`` are merged into the existing
      lists; timestamps are filled in if the coordinator omits them.
    * ``hypothesis`` and ``open_questions`` replace the prior values when
      provided (``None`` = leave unchanged).
    """

    facts_append: list[FactEntry] = Field(default_factory=list)
    decisions_append: list[DecisionEntry] = Field(default_factory=list)
    hypothesis: str | None = None
    open_questions: list[str] | None = None


def state_path(workspace_dir: str) -> str:
    return os.path.join(workspace_dir, STATE_FILENAME)


def load_state(workspace_dir: str) -> CoordinatorState:
    """Read and parse the YAML file. Returns a fresh empty state when the
    file is missing, empty, or unparseable — missing state should never
    crash the coordinator, and a user who wants to reset can just delete
    the file.
    """
    path = state_path(workspace_dir)
    if not os.path.isfile(path):
        return CoordinatorState()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("coordinator_state.yaml at %s unreadable: %s", path, exc)
        return CoordinatorState()
    if not isinstance(raw, dict):
        return CoordinatorState()
    try:
        return CoordinatorState(**raw)
    except ValidationError as exc:
        logger.warning("coordinator_state.yaml at %s rejected: %s", path, exc)
        return CoordinatorState()


def save_state(workspace_dir: str, state: CoordinatorState) -> str:
    """Atomically write *state* to disk. Returns the path."""
    os.makedirs(workspace_dir, exist_ok=True)
    path = state_path(workspace_dir)
    data = state.model_dump(mode="json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, path)
    return path


def apply_update(state: CoordinatorState, update: StateUpdate) -> CoordinatorState:
    """Return a new state with *update* merged in.

    Behaviour:
    * append-only lists extend in place
    * hypothesis / open_questions replace when the update sets them
    * missing timestamps on appended entries default to now (UTC)
    """
    def _ts(v: datetime | None) -> datetime:
        return v if v is not None else datetime.now(timezone.utc)

    facts = list(state.facts)
    for f in update.facts_append:
        facts.append(FactEntry(
            ts=_ts(f.ts),
            kind=f.kind,
            agent=f.agent,
            summary=f.summary,
        ))
    decisions = list(state.decisions)
    for d in update.decisions_append:
        decisions.append(DecisionEntry(
            ts=_ts(d.ts),
            decision=d.decision,
            rationale=d.rationale,
        ))
    return CoordinatorState(
        version=CURRENT_VERSION,
        facts=facts,
        decisions=decisions,
        hypothesis=update.hypothesis if update.hypothesis is not None else state.hypothesis,
        open_questions=(
            list(update.open_questions)
            if update.open_questions is not None
            else list(state.open_questions)
        ),
    )


def delete_state(workspace_dir: str) -> bool:
    """Remove the state file if present. Returns True iff a file was deleted."""
    path = state_path(workspace_dir)
    if not os.path.isfile(path):
        return False
    os.remove(path)
    return True


def parse_update_from_tool(args: dict[str, Any]) -> StateUpdate:
    """Build a ``StateUpdate`` from the loose tool args Claude hands over.

    The MCP tool argument schema uses plain lists of dicts because JSON
    schemas nested models don't always round-trip cleanly through the SDK.
    We validate here so tool errors surface as Pydantic validation messages
    the coordinator can correct on a retry.
    """
    return StateUpdate(
        facts_append=[FactEntry(**x) for x in args.get("facts_append", []) or []],
        decisions_append=[
            DecisionEntry(**x) for x in args.get("decisions_append", []) or []
        ],
        hypothesis=args.get("hypothesis"),
        open_questions=args.get("open_questions"),
    )


__all__ = [
    "CoordinatorState",
    "DecisionEntry",
    "FactEntry",
    "StateUpdate",
    "apply_update",
    "delete_state",
    "load_state",
    "parse_update_from_tool",
    "save_state",
    "state_path",
]
