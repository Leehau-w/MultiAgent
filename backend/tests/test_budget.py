"""Smoke tests for BudgetTracker.

The tracker owes no heavy machinery — it reads caps from workflow.yaml
live, accumulates usage, and exposes a single imperative:
``check_can_start``. These tests drive it with stubbed agents and
synthetic workflow files.
"""

from __future__ import annotations

import pytest

from app.budget import BudgetExceeded, BudgetTracker
from app.models import AgentStatus, PipelineStage
from app.workflow import Budget, Workflow, save_workflow


def test_no_workflow_means_unlimited(project):
    tr = project.budget
    tr.check_can_start()  # must not raise
    snap = tr.snapshot()
    assert snap["caps"] == {}
    assert snap["exceeded"] is False


def test_cost_cap_trips_on_check(project):
    save_workflow(
        project.workspace_dir,
        Workflow(
            stages=[PipelineStage(name="s", agents=["pm"])],
            budget=Budget(max_total_cost_usd=1.0),
        ),
    )
    tr = project.budget
    tr.usage.cost_usd = 1.5

    with pytest.raises(BudgetExceeded) as ei:
        tr.check_can_start()
    assert ei.value.reason == "cost"
    # After tripping, further starts keep failing (sticky).
    assert tr.exceeded is True
    with pytest.raises(BudgetExceeded):
        tr.check_can_start()


def test_turns_cap_trips(project):
    save_workflow(
        project.workspace_dir,
        Workflow(
            stages=[PipelineStage(name="s", agents=["pm"])],
            budget=Budget(max_total_turns=3),
        ),
    )
    tr = project.budget
    tr.usage.turns = 3
    with pytest.raises(BudgetExceeded) as ei:
        tr.check_can_start()
    assert ei.value.reason == "turns"


def test_concurrent_cap_does_not_stick(project, roles):
    save_workflow(
        project.workspace_dir,
        Workflow(
            stages=[PipelineStage(name="s", agents=["pm"])],
            budget=Budget(max_concurrent_agents=1),
        ),
    )
    from app.models import AgentState

    project.agents["a1"] = AgentState(
        id="a1", role_id="writer", role_name="Writer",
        status=AgentStatus.RUNNING, context_file="x",
    )

    tr = project.budget
    with pytest.raises(BudgetExceeded) as ei:
        tr.check_can_start()
    assert ei.value.reason == "concurrent"
    # Concurrency is transient — lower the count and retry succeeds.
    project.agents["a1"].status = AgentStatus.IDLE
    tr.check_can_start()  # no raise


def test_record_turn_reports_trip(project):
    save_workflow(
        project.workspace_dir,
        Workflow(
            stages=[PipelineStage(name="s", agents=["pm"])],
            budget=Budget(max_total_cost_usd=0.50),
        ),
    )
    tr = project.budget
    # First turn stays under the cap.
    assert tr.record_turn(0.20, turn_delta=1) is False
    assert tr.exceeded is False
    # Second turn crosses the cap.
    assert tr.record_turn(0.40, turn_delta=1) is True
    assert tr.exceeded is True
    # A third update after tripping must not re-broadcast.
    assert tr.record_turn(0.10, turn_delta=1) is False


def test_snapshot_shape(project):
    save_workflow(
        project.workspace_dir,
        Workflow(
            stages=[PipelineStage(name="s", agents=["pm"])],
            budget=Budget(max_total_cost_usd=10.0, max_total_turns=100),
        ),
    )
    tr = project.budget
    tr.usage.cost_usd = 2.5
    tr.usage.turns = 7
    snap = tr.snapshot()
    assert snap["caps"] == {"max_total_cost_usd": 10.0, "max_total_turns": 100}
    assert snap["usage"]["cost_usd"] == 2.5
    assert snap["usage"]["turns"] == 7
    assert snap["exceeded"] is False


def test_start_resets_sticky_flag(project):
    save_workflow(
        project.workspace_dir,
        Workflow(
            stages=[PipelineStage(name="s", agents=["pm"])],
            budget=Budget(max_total_cost_usd=1.0),
        ),
    )
    tr = project.budget
    tr.usage.cost_usd = 2.0
    with pytest.raises(BudgetExceeded):
        tr.check_can_start()
    assert tr.exceeded is True
    # A fresh workflow run should clear the sticky state even if spend
    # has not been zeroed — the user has seen the warning and chosen
    # to continue.
    tr.start()
    assert tr.exceeded is False


def test_fresh_BudgetTracker_has_clean_state(project):
    tr = BudgetTracker(project)
    assert tr.usage.cost_usd == 0.0
    assert tr.usage.turns == 0
    assert tr.exceeded is False


def test_sticky_flag_clears_when_user_raises_cap(project):
    """Regression: once _exceeded was set, raising the cap in
    workflow.yaml had no effect — check_can_start kept failing with a
    stale detail ("cap 60m" after user had bumped it to 600m) until
    the tracker was manually reset. Now check_can_start re-evaluates
    against current usage before consulting the sticky flag.
    """
    save_workflow(
        project.workspace_dir,
        Workflow(
            stages=[PipelineStage(name="s", agents=["pm"])],
            budget=Budget(max_total_cost_usd=1.0),
        ),
    )
    tr = project.budget
    tr.usage.cost_usd = 1.5
    with pytest.raises(BudgetExceeded):
        tr.check_can_start()
    assert tr.exceeded is True

    # User raises the cap to cover current spend without resetting.
    save_workflow(
        project.workspace_dir,
        Workflow(
            stages=[PipelineStage(name="s", agents=["pm"])],
            budget=Budget(max_total_cost_usd=10.0),
        ),
    )
    tr.check_can_start()  # must not raise now
    assert tr.exceeded is False


def test_sticky_flag_clears_when_caps_removed(project):
    """If workflow.yaml is edited to drop the budget block entirely,
    any prior trip is no longer meaningful and must clear."""
    save_workflow(
        project.workspace_dir,
        Workflow(
            stages=[PipelineStage(name="s", agents=["pm"])],
            budget=Budget(max_total_turns=2),
        ),
    )
    tr = project.budget
    tr.usage.turns = 5
    with pytest.raises(BudgetExceeded):
        tr.check_can_start()
    assert tr.exceeded is True

    # Drop the budget block.
    save_workflow(
        project.workspace_dir,
        Workflow(stages=[PipelineStage(name="s", agents=["pm"])]),
    )
    tr.check_can_start()
    assert tr.exceeded is False


def test_sticky_detail_refreshes_with_current_numbers(project):
    """When the cap stays tripped across multiple checks, the detail
    string reported by BudgetExceeded reflects the *current* usage, not
    the snapshot at first trip. Users re-triggering the check after
    bumping spend should see the latest number."""
    save_workflow(
        project.workspace_dir,
        Workflow(
            stages=[PipelineStage(name="s", agents=["pm"])],
            budget=Budget(max_total_cost_usd=1.0),
        ),
    )
    tr = project.budget
    tr.usage.cost_usd = 1.5
    with pytest.raises(BudgetExceeded) as ei1:
        tr.check_can_start()
    assert "1.5000" in ei1.value.detail

    tr.usage.cost_usd = 3.0
    with pytest.raises(BudgetExceeded) as ei2:
        tr.check_can_start()
    assert "3.0000" in ei2.value.detail
