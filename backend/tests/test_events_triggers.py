"""Tests for EventQueue + workflow trigger matching."""

from __future__ import annotations

from app.events import Event, EventQueue
from app.models import PipelineStage
from app.workflow import (
    Trigger,
    TriggerAction,
    Workflow,
    match_triggers,
    save_workflow,
    load_workflow,
)


# ------------------------------------------------------------------ #
#  EventQueue                                                          #
# ------------------------------------------------------------------ #


def test_push_and_tail_return_newest():
    q = EventQueue()
    q.push(Event(kind="agent_completed", agent="a"))
    q.push(Event(kind="agent_error", agent="b"))
    q.push(Event(kind="user_message"))
    tail = q.tail(2)
    assert [e.kind for e in tail] == ["agent_error", "user_message"]


def test_completed_set_tracks_completions():
    q = EventQueue()
    q.push(Event(kind="agent_error", agent="a"))  # not a completion
    q.push(Event(kind="agent_completed", agent="a"))
    q.push(Event(kind="agent_completed", agent="b"))
    assert q.completed_agents() == {"a", "b"}
    q.clear_completed("a")
    assert q.completed_agents() == {"b"}


def test_ring_buffer_maxlen_drops_oldest():
    q = EventQueue(maxlen=3)
    for i in range(5):
        q.push(Event(kind="user_message", detail={"i": i}))
    assert len(q) == 3
    assert [e.detail["i"] for e in q.tail(3)] == [2, 3, 4]


# ------------------------------------------------------------------ #
#  Trigger matcher                                                     #
# ------------------------------------------------------------------ #


def _wf(triggers: list[Trigger]) -> Workflow:
    return Workflow(
        stages=[PipelineStage(name="s", agents=["pm"])],
        triggers=triggers,
    )


def test_simple_on_completed_fires():
    wf = _wf([Trigger(on=["pm.completed"], start=["td"], context_from=["pm"])])
    evt = Event(kind="agent_completed", agent="pm")
    actions = match_triggers(wf, evt, completed_agents={"pm"})
    assert len(actions) == 1
    assert actions[0].start_agents == ["td"]
    assert actions[0].context_from == ["pm"]


def test_unrelated_event_does_not_fire():
    wf = _wf([Trigger(on=["pm.completed"], start=["td"])])
    evt = Event(kind="agent_completed", agent="td")
    actions = match_triggers(wf, evt, completed_agents={"td"})
    assert actions == []


def test_and_join_requires_all_completed():
    wf = _wf([Trigger(on=["dev_be.completed", "dev_fe.completed"], start=["reviewer"])])
    # Only dev_be done so far.
    evt1 = Event(kind="agent_completed", agent="dev_be")
    actions = match_triggers(wf, evt1, completed_agents={"dev_be"})
    assert actions == []
    # Now dev_fe completes.
    evt2 = Event(kind="agent_completed", agent="dev_fe")
    actions = match_triggers(wf, evt2, completed_agents={"dev_be", "dev_fe"})
    assert len(actions) == 1
    assert actions[0].start_agents == ["reviewer"]


def test_and_join_does_not_fire_on_unrelated_event():
    wf = _wf([Trigger(on=["a.completed", "b.completed"], start=["c"])])
    evt = Event(kind="agent_completed", agent="d")
    # Both a and b already done, but the triggering event is neither.
    actions = match_triggers(wf, evt, completed_agents={"a", "b", "d"})
    assert actions == []


def test_decide_coordinator_routes_to_coordinator():
    wf = _wf([Trigger(on=["reviewer.completed"], decide="coordinator")])
    evt = Event(kind="agent_completed", agent="reviewer")
    actions = match_triggers(wf, evt, completed_agents={"reviewer"})
    assert actions[0].decide == "coordinator"
    assert actions[0].start_agents == []


def test_on_error_predicate_matches_error_event():
    wf = _wf([Trigger(on=["dev.error"], decide="coordinator")])
    evt = Event(kind="agent_error", agent="dev", detail={"category": "api_error"})
    actions = match_triggers(wf, evt, completed_agents=set())
    assert actions[0].decide == "coordinator"


def test_parallel_start_fans_out():
    wf = _wf([Trigger(on=["td.completed"], start=["dev_be", "dev_fe"], context_from=["td"])])
    evt = Event(kind="agent_completed", agent="td")
    actions = match_triggers(wf, evt, completed_agents={"td"})
    assert actions[0].start_agents == ["dev_be", "dev_fe"]


def test_invalid_predicate_is_ignored(caplog):
    wf = _wf([Trigger(on=["this is garbage"], start=["x"])])
    evt = Event(kind="agent_completed", agent="x")
    actions = match_triggers(wf, evt, completed_agents={"x"})
    assert actions == []


# ------------------------------------------------------------------ #
#  YAML persistence                                                    #
# ------------------------------------------------------------------ #


def test_workflow_yaml_roundtrips_triggers_and_coordinator(project):
    from app.workflow import CoordinatorConfig

    wf = Workflow(
        stages=[PipelineStage(name="s", agents=["pm"])],
        coordinator=CoordinatorConfig(enabled=True, role_id="coordinator"),
        triggers=[
            Trigger(on=["pm.completed"], start=["td"], context_from=["pm"]),
            Trigger(on=["td.completed"], start=["dev_be", "dev_fe"]),
        ],
    )
    save_workflow(project.workspace_dir, wf)
    loaded = load_workflow(project.workspace_dir)
    assert loaded is not None
    assert loaded.coordinator is not None
    assert loaded.coordinator.enabled is True
    assert len(loaded.triggers) == 2
    assert loaded.triggers[1].start == ["dev_be", "dev_fe"]


def test_trigger_action_model():
    a = TriggerAction(start_agents=["x"], context_from=["y"], trigger_index=3)
    assert a.start_agents == ["x"]
    assert a.context_from == ["y"]
    assert a.trigger_index == 3
    assert a.decide is None
