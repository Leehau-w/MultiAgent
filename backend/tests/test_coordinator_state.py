"""Tests for the externalized coordinator memory blocks."""

from __future__ import annotations

from datetime import datetime, timezone

from app.coordinator_state import (
    CoordinatorState,
    DecisionEntry,
    FactEntry,
    StateUpdate,
    apply_update,
    delete_state,
    load_state,
    parse_update_from_tool,
    save_state,
    state_path,
)


def test_load_missing_returns_fresh(project):
    state = load_state(project.workspace_dir)
    assert isinstance(state, CoordinatorState)
    assert state.facts == []
    assert state.hypothesis == ""
    assert state.open_questions == []
    assert state.decisions == []


def test_save_roundtrip(project):
    state = CoordinatorState(
        facts=[FactEntry(ts=datetime.now(timezone.utc), kind="test", summary="hi")],
        hypothesis="things are fine",
        open_questions=["q1", "q2"],
        decisions=[
            DecisionEntry(ts=datetime.now(timezone.utc), decision="go", rationale="because"),
        ],
    )
    save_state(project.workspace_dir, state)
    loaded = load_state(project.workspace_dir)
    assert loaded.hypothesis == "things are fine"
    assert loaded.open_questions == ["q1", "q2"]
    assert len(loaded.facts) == 1
    assert loaded.facts[0].summary == "hi"
    assert loaded.decisions[0].decision == "go"


def test_apply_update_appends_facts_and_decisions(project):
    base = CoordinatorState(
        facts=[FactEntry(ts=datetime.now(timezone.utc), kind="seed", summary="old")],
        hypothesis="old hypo",
        open_questions=["keep?"],
        decisions=[],
    )
    update = StateUpdate(
        facts_append=[FactEntry(ts=datetime.now(timezone.utc), kind="new", summary="fresh")],
        decisions_append=[
            DecisionEntry(ts=datetime.now(timezone.utc), decision="ship", rationale="rf")
        ],
    )
    result = apply_update(base, update)
    assert len(result.facts) == 2
    assert result.facts[1].summary == "fresh"
    assert result.decisions[0].decision == "ship"
    # hypothesis + open_questions untouched when update leaves them None
    assert result.hypothesis == "old hypo"
    assert result.open_questions == ["keep?"]


def test_apply_update_replaces_hypothesis_and_questions(project):
    base = CoordinatorState(hypothesis="old", open_questions=["a"])
    update = StateUpdate(hypothesis="new", open_questions=["b", "c"])
    result = apply_update(base, update)
    assert result.hypothesis == "new"
    assert result.open_questions == ["b", "c"]


def test_parse_update_from_tool_accepts_tsless_entries(project):
    """Coordinators omit timestamps — entries should still validate and
    get stamped with now()."""
    update = parse_update_from_tool(
        {
            "facts_append": [{"kind": "k", "summary": "s"}],
            "decisions_append": [{"decision": "d"}],
            "hypothesis": "h",
            "open_questions": ["q"],
        }
    )
    result = apply_update(CoordinatorState(), update)
    assert result.facts[0].summary == "s"
    assert result.facts[0].ts is not None
    assert result.decisions[0].decision == "d"
    assert result.hypothesis == "h"
    assert result.open_questions == ["q"]


def test_delete_state_removes_file(project):
    save_state(project.workspace_dir, CoordinatorState(hypothesis="x"))
    assert delete_state(project.workspace_dir) is True
    assert delete_state(project.workspace_dir) is False


def test_state_path_is_inside_workspace(project):
    p = state_path(project.workspace_dir)
    assert p.startswith(project.workspace_dir)
    assert p.endswith("coordinator_state.yaml")
