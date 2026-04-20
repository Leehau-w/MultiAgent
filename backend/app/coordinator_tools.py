"""In-process MCP tools exposed to Coordinator agents.

The coordinator role orchestrates other agents. The original protocol was a
text-directive format (``>>> START <agent_id> <prompt>``) parsed post-turn
from the assistant's output. That protocol is fragile: code fences, block
quotes or even chatty narration can emit spurious directives, and the
coordinator has no feedback on whether the directive succeeded.

This module defines an in-process MCP server the coordinator can call while
it reasons. Each tool closes over the :class:`Project` instance it was built
for, so tool calls act on the correct project without passing identifiers
around at the SDK boundary.

Non-Claude providers don't support SDK MCP servers — for those the legacy
``>>>`` directive parser in :mod:`app.project` still handles routing.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

try:
    from claude_agent_sdk import create_sdk_mcp_server, tool
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False

from .coordinator_state import (
    apply_update,
    load_state,
    parse_update_from_tool,
    save_state,
)
from .notifications import append_notification

if TYPE_CHECKING:
    from .project import Project

logger = logging.getLogger(__name__)


# Name the coordinator MCP server is registered under. Claude surfaces tools
# as ``mcp__<server>__<tool>``, so the prefix users and approval gates see is
# ``mcp__coord__``.
COORDINATOR_SERVER_NAME = "coord"


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def build_coordinator_tools(project: "Project") -> list[Any]:
    """Return the ``SdkMcpTool`` objects the coordinator can invoke, wiring
    each handler to *project*. Exposed separately from the server so tests
    can call handlers directly without going through the MCP request path.

    Returns ``[]`` when ``claude-agent-sdk`` is not installed.
    """
    if not _SDK_AVAILABLE:
        return []

    @tool(
        "start_agent",
        "Send a prompt to an existing agent. Resumes the agent if it was idle; "
        "queues the prompt if it is currently running. Use this to re-task a "
        "worker after [AGENT_DONE] or to hand it a follow-up.",
        {"agent_id": str, "prompt": str},
    )
    async def _start_agent(args: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(args.get("agent_id", "")).strip()
        prompt = str(args.get("prompt", "")).strip()
        if not agent_id or not prompt:
            return _err("start_agent requires non-empty agent_id and prompt")
        if agent_id not in project.agents:
            known = ", ".join(project.agents) or "(none)"
            return _err(f"No such agent: {agent_id}. Known agents: {known}")
        try:
            await project.send_message(agent_id, prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] coord start_agent failed: %s", project.id, exc)
            return _err(f"Dispatch failed: {exc}")
        return _ok(f"Dispatched to {agent_id}.")

    @tool(
        "spawn_agent",
        "Create a fresh agent of a given role and run it with a starting prompt. "
        "The agent_id must be unique within the project.",
        {"role_id": str, "agent_id": str, "prompt": str},
    )
    async def _spawn_agent(args: dict[str, Any]) -> dict[str, Any]:
        role_id = str(args.get("role_id", "")).strip()
        agent_id = str(args.get("agent_id", "")).strip()
        prompt = str(args.get("prompt", "")).strip()
        if not role_id or not agent_id or not prompt:
            return _err("spawn_agent requires role_id, agent_id, prompt")
        try:
            project.create_agent(role_id, agent_id)
            project.start_agent(agent_id, prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] coord spawn_agent failed: %s", project.id, exc)
            return _err(f"Spawn failed: {exc}")
        return _ok(f"Spawned {agent_id} (role={role_id}).")

    @tool(
        "mark_done",
        "Declare that the coordinator's work is finished. After this the "
        "runtime stops forwarding [AGENT_DONE] events, so call it only when "
        "you truly have no further routing decisions to make. When a "
        "stage-gate pipeline is active and the ``reason`` starts with "
        "'ABORT:' (case-sensitive), the pipeline is terminated with "
        "status=failed and the reason surfaces to the user as the failure "
        "message — use this for unrecoverable failures that rework cannot fix.",
        {"reason": str},
    )
    async def _mark_done(args: dict[str, Any]) -> dict[str, Any]:
        from .models import AgentStatus  # local import to avoid cycle at import
        from .project import GateVerdict  # local import to avoid cycle

        reason = str(args.get("reason", "")).strip() if isinstance(args, dict) else ""
        is_abort = reason.startswith("ABORT:")

        # Route ABORT through the gate-verdict channel when a stage review
        # is actually pending — the orchestrator treats it as a failed
        # pipeline terminator rather than a "coordinator is done" signal.
        if (
            is_abort
            and project.pipeline.current_stage_name is not None
            and project.pipeline.gate_verdict is None
        ):
            project.pipeline.gate_verdict = GateVerdict(
                action="ABORT", summary=reason,
            )
            project.pipeline.gate_verdict_ready.set()
            logger.info(
                "[%s] coord aborted pipeline at stage %s: %s",
                project.id,
                project.pipeline.current_stage_name,
                reason[:120],
            )
            return _ok(
                f"Pipeline aborted at stage {project.pipeline.current_stage_name}: "
                f"{reason[:200]}"
            )

        coord_id = project._find_coordinator(project._coord_role_id())
        if coord_id:
            agent = project.agents.get(coord_id)
            if agent:
                agent.status = AgentStatus.COMPLETED
                agent.current_task = None
        if reason:
            logger.info("[%s] coord marked done: %s", project.id, reason[:120])
        return _ok(
            f"Marked done{' — ABORT reason recorded' if is_abort else ''}. "
            "You will not be re-notified."
        )

    @tool(
        "update_state",
        "Persist the coordinator's reasoning to coordinator_state.yaml. "
        "Call this exactly once per invocation to record what you learned. "
        "facts_append and decisions_append are append-only lists — each "
        "entry needs a short summary / decision string. hypothesis and "
        "open_questions REPLACE the existing values when provided.",
        {
            "facts_append": list,
            "decisions_append": list,
            "hypothesis": str,
            "open_questions": list,
        },
    )
    async def _update_state(args: dict[str, Any]) -> dict[str, Any]:
        try:
            update = parse_update_from_tool(args)
        except Exception as exc:  # noqa: BLE001
            return _err(f"update_state rejected: {exc}")
        state = load_state(project.workspace_dir)
        new_state = apply_update(state, update)
        try:
            save_state(project.workspace_dir, new_state)
        except OSError as exc:
            return _err(f"update_state could not write state: {exc}")
        return _ok(
            f"State saved: facts={len(new_state.facts)} "
            f"decisions={len(new_state.decisions)} "
            f"open_questions={len(new_state.open_questions)}"
        )

    @tool(
        "read_context",
        "Read a worker agent's full context MD — the document the agent "
        "has been writing as it works. Use this to inspect findings before "
        "routing the next agent.",
        {"agent_id": str},
    )
    async def _read_context(args: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(args.get("agent_id", "")).strip()
        if not agent_id:
            return _err("read_context requires agent_id")
        if agent_id not in project.agents:
            return _err(f"No such agent: {agent_id}")
        try:
            content = project.ctx.read(agent_id)
        except Exception as exc:  # noqa: BLE001
            return _err(f"Could not read context for {agent_id}: {exc}")
        if not content:
            return _ok(f"(context for {agent_id} is empty)")
        return _ok(content)

    @tool(
        "list_agents",
        "List every agent in this project with its role, status, and "
        "current task. Use as a snapshot before deciding what to do next.",
        {},
    )
    async def _list_agents(_args: dict[str, Any]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for aid, agent in project.agents.items():
            rows.append({
                "agent_id": aid,
                "role_id": agent.role_id,
                "status": agent.status.value,
                "current_task": agent.current_task,
                "session_id": agent.session_id,
            })
        return _ok(json.dumps(rows, ensure_ascii=False, indent=2))

    @tool(
        "get_agent_status",
        "Return the status, usage totals, and last task for one agent. "
        "Cheaper than list_agents when you already know the id.",
        {"agent_id": str},
    )
    async def _get_agent_status(args: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(args.get("agent_id", "")).strip()
        if not agent_id:
            return _err("get_agent_status requires agent_id")
        agent = project.agents.get(agent_id)
        if agent is None:
            return _err(f"No such agent: {agent_id}")
        payload = {
            "agent_id": agent_id,
            "role_id": agent.role_id,
            "status": agent.status.value,
            "current_task": agent.current_task,
            "usage": agent.usage.model_dump(),
            "started_at": (
                agent.started_at.isoformat() if agent.started_at else None
            ),
            "finished_at": (
                agent.finished_at.isoformat() if agent.finished_at else None
            ),
        }
        return _ok(json.dumps(payload, ensure_ascii=False, indent=2))

    @tool(
        "get_inbox",
        "Return the most recent workflow events for this project — agent "
        "completions, errors, user messages. Read-only; call once at the "
        "top of your turn to orient yourself.",
        {"limit": int},
    )
    async def _get_inbox(args: dict[str, Any]) -> dict[str, Any]:
        try:
            limit = int(args.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, 200))
        events = getattr(project, "events", None)
        if events is None:
            return _ok("[]")
        tail = events.tail(limit)
        return _ok(json.dumps([e.to_dict() for e in tail], ensure_ascii=False, indent=2))

    @tool(
        "approve_stage",
        "Approve the currently-gated stage and advance the pipeline. Call "
        "exactly once per [STAGE_COMPLETE] review after you've verified the "
        "agents' outputs meet the acceptance criteria. stage_name must match "
        "the stage reported in the most recent [STAGE_COMPLETE] inbox message.",
        {"stage_name": str, "summary": str},
    )
    async def _approve_stage(args: dict[str, Any]) -> dict[str, Any]:
        from .project import GateVerdict  # local import to avoid cycle

        stage_name = str(args.get("stage_name", "")).strip()
        summary = str(args.get("summary", "")).strip()
        if not stage_name:
            return _err("approve_stage requires a non-empty stage_name")
        current = project.pipeline.current_stage_name
        if current is None:
            return _err(
                "No stage is currently awaiting a gate verdict. "
                "approve_stage is only valid in response to [STAGE_COMPLETE]."
            )
        if stage_name != current:
            return _err(
                f"stage_name={stage_name!r} does not match the stage "
                f"currently under review ({current!r})."
            )
        if project.pipeline.gate_verdict is not None:
            return _err(
                "A verdict has already been recorded for this stage. "
                "Wait for the next [STAGE_COMPLETE] before gating again."
            )
        project.pipeline.gate_verdict = GateVerdict(
            action="APPROVE", summary=summary,
        )
        project.pipeline.gate_verdict_ready.set()
        logger.info(
            "[%s] coord approved stage %s (%s)",
            project.id, stage_name, summary[:80],
        )
        return _ok(f"Approved stage {stage_name}.")

    @tool(
        "request_rework",
        "Send listed agents back to revise their output with a specific "
        "instruction; the pipeline re-runs them and re-emits [STAGE_COMPLETE] "
        "for another review. agents must contain at least one agent id that "
        "appeared in the [STAGE_COMPLETE] message; instruction should state "
        "exactly what must change (not a vague 'try again').",
        {"stage_name": str, "agents": list, "instruction": str},
    )
    async def _request_rework(args: dict[str, Any]) -> dict[str, Any]:
        from .project import GateVerdict  # local import to avoid cycle

        stage_name = str(args.get("stage_name", "")).strip()
        raw_agents = args.get("agents") or []
        if not isinstance(raw_agents, list):
            return _err("request_rework 'agents' must be a list of agent ids")
        agents = [str(a).strip() for a in raw_agents if str(a).strip()]
        instruction = str(args.get("instruction", "")).strip()
        if not stage_name:
            return _err("request_rework requires a non-empty stage_name")
        if not agents:
            return _err("request_rework requires at least one agent id")
        if not instruction:
            return _err(
                "request_rework requires a concrete instruction — state what "
                "specifically must change before re-review."
            )
        current = project.pipeline.current_stage_name
        if current is None:
            return _err(
                "No stage is currently awaiting a gate verdict. "
                "request_rework is only valid in response to [STAGE_COMPLETE]."
            )
        if stage_name != current:
            return _err(
                f"stage_name={stage_name!r} does not match the stage "
                f"currently under review ({current!r})."
            )
        if project.pipeline.gate_verdict is not None:
            return _err(
                "A verdict has already been recorded for this stage. "
                "Wait for the next [STAGE_COMPLETE] before gating again."
            )
        unknown = [a for a in agents if a not in project.agents]
        if unknown:
            return _err(
                f"Unknown agent ids in rework request: {unknown}. "
                f"Pick from the roster in the latest [STAGE_COMPLETE] message."
            )
        project.pipeline.gate_verdict = GateVerdict(
            action="RETRY", agents=agents, instruction=instruction,
        )
        project.pipeline.gate_verdict_ready.set()
        logger.info(
            "[%s] coord requested rework on stage %s agents=%s",
            project.id, stage_name, agents,
        )
        return _ok(
            f"Rework requested on {len(agents)} agent(s) in stage {stage_name}."
        )

    @tool(
        "notify_user",
        "Push a proactive message to the user outside the chat flow. Appears "
        "as a toast in the frontend. Use level='info' for status pings "
        "(auto-dismiss), 'warning' for things the user should see but not "
        "necessarily act on, and 'blocker' when the pipeline cannot progress "
        "without user input. Set action_required=True on blockers the user "
        "must acknowledge (e.g. stage rework exhausted — needs override or "
        "abort). Keep messages short and concrete; put detail in coord.md or "
        "the state file, not the toast.",
        {"level": str, "message": str, "action_required": bool},
    )
    async def _notify_user(args: dict[str, Any]) -> dict[str, Any]:
        level = str(args.get("level", "")).strip().lower()
        message = str(args.get("message", "")).strip()
        action_required = bool(args.get("action_required", False))
        if level not in ("info", "warning", "blocker"):
            return _err(
                f"notify_user: level must be one of 'info', 'warning', "
                f"'blocker' (got {level!r})"
            )
        if not message:
            return _err("notify_user: message must be non-empty")

        entry = append_notification(
            project.workspace_dir,
            level=level,  # type: ignore[arg-type]
            message=message,
            action_required=action_required,
        )
        await project.broadcast_raw({
            "type": "coordinator_notify_user",
            "data": {
                "id": entry.id,
                "level": entry.level,
                "message": entry.message,
                "action_required": entry.action_required,
                "timestamp": entry.ts.isoformat(),
            },
        })
        logger.info(
            "[%s] coord notify_user level=%s action=%s message=%s",
            project.id, level, action_required, message[:120],
        )
        return _ok(
            f"Notification pushed (level={level}, "
            f"action_required={action_required})."
        )

    @tool(
        "spawn_and_rework",
        "Create a fresh agent of the given role AND schedule a stage rework "
        "that includes it. Use when a stage gap requires a different role "
        "(e.g. a reviewer to catch what the developer missed). The project's "
        "workflow.coordinator.allow_spawn must be true — otherwise the call "
        "is refused and you must request_rework with existing agents. "
        "include_existing lists any existing agents that should also rework "
        "(leave empty to re-run only the new agent).",
        {
            "role_id": str,
            "agent_id": str,
            "prompt": str,
            "include_existing": list,
        },
    )
    async def _spawn_and_rework(args: dict[str, Any]) -> dict[str, Any]:
        from .project import GateVerdict  # local import to avoid cycle
        from .workflow import load_workflow

        role_id = str(args.get("role_id", "")).strip()
        agent_id = str(args.get("agent_id", "")).strip()
        prompt = str(args.get("prompt", "")).strip()
        raw_existing = args.get("include_existing") or []
        if not isinstance(raw_existing, list):
            return _err("spawn_and_rework 'include_existing' must be a list")
        include_existing = [str(a).strip() for a in raw_existing if str(a).strip()]
        if not role_id or not agent_id or not prompt:
            return _err("spawn_and_rework requires role_id, agent_id, prompt")
        current = project.pipeline.current_stage_name
        if current is None:
            return _err(
                "No stage is currently awaiting a gate verdict. "
                "spawn_and_rework is only valid in response to [STAGE_COMPLETE]."
            )
        if project.pipeline.gate_verdict is not None:
            return _err(
                "A verdict has already been recorded for this stage. "
                "Wait for the next [STAGE_COMPLETE] before gating again."
            )
        # allow_spawn guard
        wf = load_workflow(project.workspace_dir)
        allow = bool(
            wf is not None
            and wf.coordinator is not None
            and wf.coordinator.allow_spawn
        )
        if not allow:
            return _err(
                "spawn_and_rework refused: workflow.coordinator.allow_spawn "
                "is false.  Use request_rework with an existing agent, or "
                "ask the user to enable spawning."
            )
        unknown_existing = [a for a in include_existing if a not in project.agents]
        if unknown_existing:
            return _err(
                f"Unknown ids in include_existing: {unknown_existing}. "
                f"Pick from the roster in the latest [STAGE_COMPLETE] message."
            )
        try:
            project.create_agent(role_id, agent_id)
        except ValueError as exc:
            return _err(f"Spawn failed: {exc}")

        project.pipeline.gate_verdict = GateVerdict(
            action="RETRY",
            agents=[agent_id, *include_existing],
            instruction=prompt,
            spawned_agents=[agent_id],
        )
        project.pipeline.gate_verdict_ready.set()
        logger.info(
            "[%s] coord spawn_and_rework: +%s (role=%s) in stage %s",
            project.id, agent_id, role_id, current,
        )
        return _ok(
            f"Spawned {agent_id} (role={role_id}) and scheduled rework of "
            f"{len([agent_id, *include_existing])} agent(s) in stage {current}."
        )

    return [
        _start_agent,
        _spawn_agent,
        _mark_done,
        _update_state,
        _read_context,
        _list_agents,
        _get_agent_status,
        _get_inbox,
        _approve_stage,
        _request_rework,
        _notify_user,
        _spawn_and_rework,
    ]


def build_coordinator_mcp_server(project: "Project") -> Any | None:
    """Return an ``McpSdkServerConfig`` wiring the coordinator tools to *project*.

    Returns ``None`` when ``claude-agent-sdk`` is not installed — callers
    should fall back to the directive parser for non-Claude providers.
    """
    if not _SDK_AVAILABLE:
        return None
    return create_sdk_mcp_server(
        name=COORDINATOR_SERVER_NAME,
        version="1.0.0",
        tools=build_coordinator_tools(project),
    )
