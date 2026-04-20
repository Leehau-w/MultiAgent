"""Smoke tests for the v0.2.0 coordinator path.

Covers the non-LLM surface: the MCP tool wrappers call the right Project
methods, the [AGENT_DONE] notification carries an absolute context path, the
runtime header reflects which action protocol is active, and persistence
round-trips agent state through a backend restart.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from app.coordinator_tools import (
    build_coordinator_mcp_server,
    build_coordinator_tools,
)
from app.models import AgentState, AgentStatus, ProjectMeta
from app.project import Project
from app.ws_manager import WSManager


# ------------------------------------------------------------------ #
#  MCP tool surface                                                   #
# ------------------------------------------------------------------ #


def _register_agents(project: Project) -> None:
    project.agents["c1"] = AgentState(
        id="c1", role_id="coordinator", role_name="Coordinator", context_file="c"
    )
    project.agents["w1"] = AgentState(
        id="w1", role_id="writer", role_name="Writer", context_file="w"
    )
    project._role_map["c1"] = project.roles["coordinator"]
    project._role_map["w1"] = project.roles["writer"]


def _tool_handlers(project):
    """Return ``{tool_name: async handler}`` for the coordinator's MCP tools."""
    return {t.name: t.handler for t in build_coordinator_tools(project)}


def test_build_server_exposes_expected_tools(project):
    _register_agents(project)
    server = build_coordinator_mcp_server(project)
    assert server is not None, "SDK should be installed in dev env"
    assert server["name"] == "coord"
    names = sorted(_tool_handlers(project).keys())
    assert names == [
        "approve_stage",
        "get_agent_status",
        "get_inbox",
        "list_agents",
        "mark_done",
        "notify_user",
        "read_context",
        "request_rework",
        "restart_agent",
        "spawn_agent",
        "spawn_and_rework",
        "start_agent",
        "update_state",
    ]


def test_start_agent_tool_routes_to_send_message(project):
    _register_agents(project)
    handlers = _tool_handlers(project)

    with patch.object(project, "send_message", new=AsyncMock()) as send:
        result = asyncio.run(
            handlers["start_agent"]({"agent_id": "w1", "prompt": "write a limerick"})
        )

    assert "isError" not in result
    send.assert_awaited_once_with("w1", "write a limerick")


def test_start_agent_tool_rejects_unknown_agent(project):
    _register_agents(project)
    handlers = _tool_handlers(project)

    result = asyncio.run(handlers["start_agent"]({"agent_id": "ghost", "prompt": "hi"}))
    assert result.get("isError") is True
    assert "No such agent" in result["content"][0]["text"]


def _enable_spawn(project: Project) -> None:
    """Write a minimal workflow.yaml so ``_spawn_allowed`` returns True."""
    import yaml
    os.makedirs(project.workspace_dir, exist_ok=True)
    with open(
        os.path.join(project.workspace_dir, "workflow.yaml"), "w", encoding="utf-8"
    ) as f:
        yaml.safe_dump(
            {
                "stages": [{"name": "s", "agents": ["writer"]}],
                "coordinator": {"enabled": True, "allow_spawn": True},
            },
            f,
        )


def test_spawn_agent_tool_creates_and_starts(project):
    _register_agents(project)
    _enable_spawn(project)
    handlers = _tool_handlers(project)

    with patch.object(project, "start_agent") as start:
        result = asyncio.run(
            handlers["spawn_agent"](
                {
                    "role_id": "writer",
                    "agent_id": "w2",
                    "prompt": "write a haiku",
                }
            )
        )

    assert "isError" not in result
    assert "w2" in project.agents
    assert project.agents["w2"].role_id == "writer"
    start.assert_called_once_with("w2", "write a haiku")


def test_spawn_agent_tool_refused_when_allow_spawn_false(project):
    """allow_spawn defaults to false (no workflow.yaml) — spawn must refuse."""
    _register_agents(project)
    handlers = _tool_handlers(project)

    result = asyncio.run(
        handlers["spawn_agent"](
            {"role_id": "writer", "agent_id": "w2", "prompt": "nope"}
        )
    )
    assert result.get("isError") is True
    assert "allow_spawn" in result["content"][0]["text"]
    assert "w2" not in project.agents


def test_mark_done_sets_coordinator_completed(project):
    _register_agents(project)
    project.agents["c1"].status = AgentStatus.RUNNING
    handlers = _tool_handlers(project)

    asyncio.run(handlers["mark_done"]({}))
    assert project.agents["c1"].status == AgentStatus.COMPLETED
    assert project.agents["c1"].current_task is None


def test_notify_user_persists_and_broadcasts(project):
    _register_agents(project)
    handlers = _tool_handlers(project)

    with patch.object(project, "broadcast_raw", new=AsyncMock()) as bcast:
        result = asyncio.run(
            handlers["notify_user"](
                {
                    "level": "blocker",
                    "message": "stage review needs your call",
                    "action_required": True,
                }
            )
        )

    assert "isError" not in result
    bcast.assert_awaited_once()
    payload = bcast.await_args.args[0]
    assert payload["type"] == "coordinator_notify_user"
    assert payload["data"]["level"] == "blocker"
    assert payload["data"]["message"] == "stage review needs your call"
    assert payload["data"]["action_required"] is True
    assert payload["data"]["id"] and payload["data"]["timestamp"]

    from app.notifications import read_notifications

    stored = read_notifications(project.workspace_dir)
    assert len(stored) == 1
    assert stored[0].level == "blocker"
    assert stored[0].message == "stage review needs your call"
    assert stored[0].action_required is True


def test_notify_user_rejects_unknown_level(project):
    _register_agents(project)
    handlers = _tool_handlers(project)

    result = asyncio.run(
        handlers["notify_user"]({"level": "critical", "message": "nope"})
    )
    assert result.get("isError") is True
    assert "level" in result["content"][0]["text"].lower()


def test_send_user_message_wraps_for_coordinator(project):
    _register_agents(project)
    with patch.object(project, "send_message", new=AsyncMock()) as send:
        asyncio.run(project.send_user_message("c1", "what's going on?"))
    send.assert_awaited_once_with("c1", "[USER_MESSAGE] what's going on?")


def test_send_user_message_refuses_non_coordinator(project):
    """Routing a user-channel message to a worker is a bug — workers don't
    have the [USER_MESSAGE] inbox convention and would treat the wrapped
    text as a task prompt. Must raise instead of silently dropping.
    """
    _register_agents(project)
    with patch.object(project, "send_message", new=AsyncMock()) as send:
        with pytest.raises(ValueError, match="not a coordinator"):
            asyncio.run(project.send_user_message("w1", "do a thing"))
    send.assert_not_awaited()


def test_mark_done_abort_case_insensitive(project):
    """ABORT: prefix should tolerate case and spacing variants."""
    from app.project import GateVerdict

    _register_agents(project)
    handlers = _tool_handlers(project)

    for variant in ("ABORT: real reason", "abort: lowercase", "Abort :spaced", "[ABORT: bracketed"):
        project.pipeline.current_stage_name = "review"
        project.pipeline.gate_verdict = None
        project.pipeline.gate_verdict_ready.clear()
        asyncio.run(handlers["mark_done"]({"reason": variant}))
        assert isinstance(project.pipeline.gate_verdict, GateVerdict)
        assert project.pipeline.gate_verdict.action == "ABORT"


# ------------------------------------------------------------------ #
#  Runtime header                                                     #
# ------------------------------------------------------------------ #


def test_runtime_header_mcp_mode_mentions_tools(project):
    _register_agents(project)
    h = project._coordinator_runtime_header(mcp_available=True)
    assert "start_agent(agent_id, prompt)" in h
    assert "spawn_agent(role_id, agent_id, prompt)" in h
    assert "mark_done()" in h
    # Should steer away from the text-directive path.
    assert "Do not emit `>>>`" in h


def test_runtime_header_fallback_mode_uses_directives(project):
    _register_agents(project)
    h = project._coordinator_runtime_header(mcp_available=False)
    assert ">>> START" in h
    assert ">>> SPAWN" in h
    assert ">>> DONE" in h


def test_runtime_header_includes_absolute_paths(project):
    _register_agents(project)
    h = project._coordinator_runtime_header(mcp_available=True)
    assert os.path.isabs(project.coordinator_state_path)
    assert project.coordinator_state_path in h
    # Worker roster line contains the worker's absolute context path.
    assert project._context_file_path("w1") in h


# ------------------------------------------------------------------ #
#  [AGENT_DONE] notification                                          #
# ------------------------------------------------------------------ #


def test_notify_coordinator_uses_absolute_context_path(project):
    _register_agents(project)
    project.agents["c1"].status = AgentStatus.RUNNING  # so send_message queues

    with patch.object(project, "send_message", new=AsyncMock()) as send:
        asyncio.run(project._notify_coordinator("w1", "wrote limerick"))

    send.assert_awaited_once()
    coord_id, msg = send.await_args.args
    assert coord_id == "c1"
    assert "[AGENT_DONE] w1 finished:" in msg
    assert project._context_file_path("w1") in msg
    # Relative path syntax must be gone — it doesn't resolve from the
    # agent's cwd.
    assert "workspace/" not in msg or os.path.isabs(project.workspace_dir)


def test_notify_coordinator_skips_when_source_is_coordinator(project):
    _register_agents(project)
    with patch.object(project, "send_message", new=AsyncMock()) as send:
        asyncio.run(project._notify_coordinator("c1", "I finished"))
    send.assert_not_awaited()


# ------------------------------------------------------------------ #
#  Persistence roundtrip                                              #
# ------------------------------------------------------------------ #


def test_agents_persist_across_restart(roles, workspace):
    os.makedirs(workspace, exist_ok=True)
    meta = ProjectMeta(id="proj-persist", name="p", project_dir=os.getcwd())

    first = Project(meta, WSManager(), roles, workspace)
    first.create_agent("coordinator", "c1")
    first.create_agent("writer", "w1")
    first.agents["w1"].session_id = "s-1234"
    first.agents["w1"].status = AgentStatus.RUNNING  # should reset on rehydrate
    first._save()

    # agents.json must be on disk now.
    agents_file = os.path.join(first.workspace_dir, "agents.json")
    assert os.path.isfile(agents_file)
    raw = json.loads(open(agents_file, encoding="utf-8").read())
    assert {a["id"] for a in raw["agents"]} == {"c1", "w1"}

    # Fresh instance — simulates a backend restart.
    second = Project(meta, WSManager(), roles, workspace)
    second.rehydrate()

    assert set(second.agents) == {"c1", "w1"}
    # session_id survives so the worker can resume its Claude session.
    assert second.agents["w1"].session_id == "s-1234"
    # Anything that was RUNNING pre-restart is reset — the task isn't
    # actually running anymore.
    assert second.agents["w1"].status != AgentStatus.RUNNING


def test_stream_log_survives_restart(roles, workspace):
    from app.models import OutputEntry

    os.makedirs(workspace, exist_ok=True)
    meta = ProjectMeta(id="proj-stream", name="p", project_dir=os.getcwd())

    first = Project(meta, WSManager(), roles, workspace)
    first.create_agent("writer", "w1")
    first._log_entry("w1", OutputEntry(type="text", content="hello"))
    first._log_entry("w1", OutputEntry(type="text", content="world"))

    tail = first.streams.tail("w1", limit=500)
    assert [e.content for e in tail] == ["hello", "world"]

    second = Project(meta, WSManager(), roles, workspace)
    tail2 = second.streams.tail("w1", limit=500)
    assert [e.content for e in tail2] == ["hello", "world"]
