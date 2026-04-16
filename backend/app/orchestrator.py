from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime
from typing import Any

import yaml

from .context_manager import ContextManager
from .models import (
    AgentRole,
    AgentState,
    AgentStatus,
    AgentUsage,
    OutputEntry,
    PipelineStage,
    WSEvent,
)
from .providers import ProviderMessage, get_adapter
from .ws_manager import WSManager

logger = logging.getLogger(__name__)

# Fallback pricing per million tokens — used when the provider adapter
# does not report cost_usd.
_PRICING: dict[str, dict[str, float]] = {
    "opus": {"input": 15.0, "output": 75.0},
    "sonnet": {"input": 3.0, "output": 15.0},
    "haiku": {"input": 0.8, "output": 4.0},
}


def _estimate_cost(model: str, usage: AgentUsage) -> float:
    key = model.split("-")[0] if "-" in model else model
    p = _PRICING.get(key, {"input": 3.0, "output": 15.0})
    return (
        usage.input_tokens * p["input"]
        + usage.output_tokens * p["output"]
    ) / 1_000_000


class Orchestrator:
    """Manages agent lifecycles and coordinates multi-agent pipelines."""

    def __init__(
        self,
        ws: WSManager,
        ctx: ContextManager,
        config_dir: str,
        project_dir: str | None = None,
    ) -> None:
        self.ws = ws
        self.ctx = ctx
        self.config_dir = config_dir
        self.project_dir = project_dir or ctx.workspace_dir

        self.roles: dict[str, AgentRole] = {}
        self.agents: dict[str, AgentState] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._message_queues: dict[str, asyncio.Queue[str]] = {}
        self._role_map: dict[str, AgentRole] = {}
        self._pending_permissions: dict[str, asyncio.Future[bool]] = {}

    # ------------------------------------------------------------------ #
    #  Role management                                                    #
    # ------------------------------------------------------------------ #

    def load_roles(self, path: str | None = None) -> dict[str, AgentRole]:
        if path is None:
            path = os.path.join(self.config_dir, "roles.yaml")
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        self.roles = {}
        for rid, rdata in data.get("roles", {}).items():
            self.roles[rid] = AgentRole(id=rid, **rdata)
        logger.info("Loaded %d roles from %s", len(self.roles), path)
        return self.roles

    def get_roles_yaml(self) -> str:
        path = os.path.join(self.config_dir, "roles.yaml")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def save_roles_yaml(self, content: str) -> None:
        path = os.path.join(self.config_dir, "roles.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        self.load_roles(path)

    # ------------------------------------------------------------------ #
    #  Agent CRUD                                                         #
    # ------------------------------------------------------------------ #

    def create_agent(self, role_id: str, agent_id: str | None = None) -> AgentState:
        role = self.roles.get(role_id)
        if role is None:
            raise ValueError(f"Unknown role: {role_id}")
        if agent_id is None:
            agent_id = f"{role_id}-{uuid.uuid4().hex[:6]}"
        if agent_id in self.agents:
            raise ValueError(f"Agent already exists: {agent_id}")

        ctx_file = self.ctx.create(agent_id, role.name)
        state = AgentState(
            id=agent_id,
            role_id=role_id,
            role_name=role.name,
            context_file=ctx_file,
        )
        self.agents[agent_id] = state
        self._role_map[agent_id] = role
        self._message_queues[agent_id] = asyncio.Queue()
        logger.info("Created agent %s (role=%s, provider=%s)", agent_id, role_id, role.provider)
        return state

    def delete_agent(self, agent_id: str) -> None:
        self.stop_agent(agent_id)
        self.ctx.delete(agent_id)
        self.agents.pop(agent_id, None)
        self._role_map.pop(agent_id, None)
        self._message_queues.pop(agent_id, None)

    def get_agent(self, agent_id: str) -> AgentState:
        agent = self.agents.get(agent_id)
        if agent is None:
            raise ValueError(f"Unknown agent: {agent_id}")
        return agent

    # ------------------------------------------------------------------ #
    #  Agent execution                                                    #
    # ------------------------------------------------------------------ #

    def start_agent(
        self,
        agent_id: str,
        prompt: str,
        context_from: list[str] | None = None,
    ) -> None:
        agent = self.get_agent(agent_id)
        if agent.status == AgentStatus.RUNNING:
            raise ValueError(f"Agent {agent_id} is already running")
        task = asyncio.create_task(self._run_agent(agent_id, prompt, context_from))
        self._tasks[agent_id] = task

    def stop_agent(self, agent_id: str) -> None:
        task = self._tasks.pop(agent_id, None)
        if task and not task.done():
            task.cancel()
        self._cleanup_pending_permissions(agent_id, reason="stopped")
        agent = self.agents.get(agent_id)
        if agent:
            agent.status = AgentStatus.IDLE

    # ------------------------------------------------------------------ #
    #  Permission handling                                                 #
    # ------------------------------------------------------------------ #

    async def request_permission(
        self,
        agent_id: str,
        request_id: str,
        tool_name: str,
        tool_input: dict,
    ) -> bool:
        """Broadcast a permission request to the UI and wait for a response."""
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending_permissions[request_id] = future

        await self._emit(agent_id, "agent_permission_request", {
            "request_id": request_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
        })

        # Log to output stream so the user sees what's pending
        agent = self.agents[agent_id]
        entry = OutputEntry(type="permission", content=f"Requesting permission: {tool_name}")
        agent.output_log.append(entry)
        await self._emit(agent_id, "agent_output", {
            "type": "permission",
            "text": entry.content,
            "timestamp": entry.timestamp.isoformat(),
        })

        resolution = "timeout"
        allowed = False
        try:
            allowed = await asyncio.wait_for(future, timeout=300)
            resolution = "allow" if allowed else "deny"
            return allowed
        except asyncio.TimeoutError:
            logger.warning("Permission request %s timed out", request_id)
            return False
        except asyncio.CancelledError:
            resolution = "cancelled"
            raise
        finally:
            self._pending_permissions.pop(request_id, None)
            # Fire-and-forget so a CancelledError on the agent task cannot
            # swallow the UI-sync event and leave the panel holding a
            # stale request.
            self._broadcast_resolution(agent_id, request_id, allowed, resolution)

    def _broadcast_resolution(
        self,
        agent_id: str,
        request_id: str,
        allowed: bool,
        resolution: str,
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — nothing to notify.  This only happens when the
            # helper is triggered from a non-async context during shutdown.
            return
        loop.create_task(self._emit(agent_id, "agent_permission_resolved", {
            "request_id": request_id,
            "allow": allowed,
            "resolution": resolution,
        }))

    def _cleanup_pending_permissions(self, agent_id: str, reason: str = "agent_stopped") -> None:
        """Deny any permissions still waiting when the agent ends."""
        prefix = f"{agent_id}-"
        for request_id in list(self._pending_permissions.keys()):
            if not request_id.startswith(prefix):
                continue
            future = self._pending_permissions.pop(request_id, None)
            if future and not future.done():
                future.set_result(False)
            self._broadcast_resolution(agent_id, request_id, False, reason)

    async def resolve_permission(self, request_id: str, allow: bool) -> None:
        """Resolve a pending permission request (called from API endpoint).

        The WS broadcast fires from ``request_permission``'s finally block,
        so here we only have to set the future — every connected client
        will receive the resolution regardless of which tab clicked Allow.
        """
        future = self._pending_permissions.get(request_id)
        if future and not future.done():
            future.set_result(allow)

    # ------------------------------------------------------------------ #
    #  Messaging                                                           #
    # ------------------------------------------------------------------ #

    async def send_message(self, agent_id: str, content: str) -> None:
        agent = self.get_agent(agent_id)
        if agent.status == AgentStatus.IDLE and agent.session_id:
            self.start_agent(agent_id, content)
        elif agent.status == AgentStatus.RUNNING:
            self._message_queues[agent_id].put_nowait(content)
        else:
            self.start_agent(agent_id, content)

    async def _run_agent(
        self,
        agent_id: str,
        prompt: str,
        context_from: list[str] | None = None,
    ) -> None:
        agent = self.agents[agent_id]
        role = self._role_map[agent_id]

        agent.status = AgentStatus.RUNNING
        agent.started_at = datetime.now()
        agent.current_task = prompt[:200]
        agent.usage = AgentUsage()
        await self._emit_status(agent_id)
        self.ctx.update_status(agent_id, "running", prompt[:200])

        # Log the user prompt so it appears in the output stream
        user_entry = OutputEntry(type="user", content=prompt[:2000])
        agent.output_log.append(user_entry)
        await self._emit(agent_id, "agent_output", {
            "type": "user",
            "text": prompt[:2000],
            "timestamp": user_entry.timestamp.isoformat(),
        })

        # Build full prompt with context from other agents
        full_prompt = prompt
        if context_from:
            ctx_section = self.ctx.build_context_prompt(context_from)
            if ctx_section:
                full_prompt = f"{ctx_section}\n\n---\n\nYour task:\n{prompt}"

        try:
            adapter = get_adapter(role.provider)
            result_text = ""

            # Build permission callback for this agent
            async def _perm_cb(tool_name: str, tool_input: dict) -> bool:
                rid = f"{agent_id}-{uuid.uuid4().hex[:8]}"
                return await self.request_permission(agent_id, rid, tool_name, tool_input)

            async for msg in adapter.run(
                prompt=full_prompt,
                system_prompt=role.system_prompt,
                model=role.model,
                tools=role.tools,
                cwd=str(self.project_dir),
                max_turns=role.max_turns,
                session_id=agent.session_id,
                effort=role.effort,
                permission_callback=_perm_cb,
            ):
                await self._handle_provider_message(agent_id, role.model, msg)

                if msg.type == "result":
                    if msg.content:
                        result_text = msg.content
                    if msg.session_id:
                        agent.session_id = msg.session_id
                    if msg.cost_usd is not None:
                        agent.usage.cost_usd = msg.cost_usd
                    if msg.usage:
                        agent.usage.input_tokens = msg.usage.get("input_tokens", agent.usage.input_tokens)
                        agent.usage.output_tokens = msg.usage.get("output_tokens", agent.usage.output_tokens)
                        agent.usage.cache_read_tokens = msg.usage.get("cache_read_input_tokens", 0)
                        agent.usage.cache_creation_tokens = msg.usage.get("cache_creation_input_tokens", 0)
                    await self._emit(agent_id, "agent_usage", agent.usage.model_dump())

            # Save result to context file
            if result_text:
                self.ctx.set_result(agent_id, role.name, prompt[:200], result_text)
                await self._emit(agent_id, "context_update", {"content": self.ctx.read(agent_id)})

            agent.status = AgentStatus.COMPLETED
            agent.finished_at = datetime.now()

            # Process queued messages
            queue = self._message_queues.get(agent_id)
            if queue and not queue.empty():
                next_msg = queue.get_nowait()
                await self._run_agent(agent_id, next_msg)
                return

        except asyncio.CancelledError:
            agent.status = AgentStatus.IDLE
            logger.info("Agent %s cancelled", agent_id)
        except Exception as e:
            agent.status = AgentStatus.ERROR
            agent.output_log.append(OutputEntry(type="error", content=str(e)))
            await self._emit(agent_id, "agent_error", {"error": str(e)})
            logger.exception("Agent %s failed", agent_id)
        finally:
            self._tasks.pop(agent_id, None)
            self._cleanup_pending_permissions(agent_id, reason="agent_ended")
            self.ctx.update_status(agent_id, agent.status.value)
            await self._emit_status(agent_id)

    async def _handle_provider_message(
        self, agent_id: str, model: str, msg: ProviderMessage
    ) -> None:
        agent = self.agents[agent_id]

        if msg.type == "usage":
            # input_tokens: REPLACE — represents current context window size
            agent.usage.input_tokens = msg.usage.get("input_tokens", agent.usage.input_tokens)
            # output_tokens: ACCUMULATE — each turn generates new output
            agent.usage.output_tokens += msg.usage.get("output_tokens", 0)
            agent.usage.cache_read_tokens = msg.usage.get("cache_read_input_tokens", agent.usage.cache_read_tokens)
            agent.usage.cache_creation_tokens = msg.usage.get("cache_creation_input_tokens", agent.usage.cache_creation_tokens)
            if msg.cost_usd is not None:
                agent.usage.cost_usd = msg.cost_usd
            else:
                agent.usage.cost_usd = _estimate_cost(model, agent.usage)
            await self._emit(agent_id, "agent_usage", agent.usage.model_dump())
            return

        if msg.type == "error":
            entry = OutputEntry(type="error", content=msg.content)
            agent.output_log.append(entry)
            await self._emit(agent_id, "agent_error", {"error": msg.content})
            return

        # text / tool_use / tool_result — go to the output stream
        # "result" is skipped here because it duplicates the last "text" content
        if msg.type in ("text", "tool_use", "tool_result") and msg.content:
            entry = OutputEntry(type=msg.type, content=msg.content)
            agent.output_log.append(entry)
            await self._emit(agent_id, "agent_output", {
                "type": msg.type,
                "text": msg.content,
                "timestamp": entry.timestamp.isoformat(),
            })

    # ------------------------------------------------------------------ #
    #  Pipeline execution                                                 #
    # ------------------------------------------------------------------ #

    async def run_pipeline(
        self,
        requirement: str,
        stages: list[PipelineStage] | None = None,
    ) -> None:
        if stages is None:
            stages = self._default_pipeline()

        await self.ws.broadcast_raw({
            "type": "pipeline_status",
            "data": {
                "status": "running",
                "requirement": requirement[:200],
                "stages": [s.model_dump() for s in stages],
                "current_stage": 0,
            },
        })

        stage_agents: list[list[str]] = []
        for stage in stages:
            agent_ids: list[str] = []
            for role_id in stage.agents:
                aid = self.create_agent(role_id).id
                agent_ids.append(aid)
            stage_agents.append(agent_ids)

        for i, (stage, agent_ids) in enumerate(zip(stages, stage_agents)):
            await self.ws.broadcast_raw({
                "type": "pipeline_status",
                "data": {
                    "status": "running",
                    "current_stage": i,
                    "stage_name": stage.name,
                },
            })

            prior_ids = [aid for aids in stage_agents[:i] for aid in aids]

            if i == 0:
                stage_prompt = requirement
            else:
                stage_prompt = (
                    f"Based on the previous agents' work (see their context documents), "
                    f"continue with the following requirement:\n\n{requirement}"
                )

            if stage.parallel:
                tasks = [
                    self._run_agent(aid, stage_prompt, context_from=prior_ids)
                    for aid in agent_ids
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
            else:
                for aid in agent_ids:
                    await self._run_agent(aid, stage_prompt, context_from=prior_ids)

        await self.ws.broadcast_raw({
            "type": "pipeline_status",
            "data": {"status": "completed", "current_stage": len(stages)},
        })

    def _default_pipeline(self) -> list[PipelineStage]:
        return [
            PipelineStage(name="analysis", agents=["pm"]),
            PipelineStage(name="design", agents=["td"]),
            PipelineStage(name="implementation", agents=["developer", "developer"], parallel=True),
            PipelineStage(name="review", agents=["reviewer"]),
        ]

    # ------------------------------------------------------------------ #
    #  Event helpers                                                      #
    # ------------------------------------------------------------------ #

    async def _emit(self, agent_id: str, event_type: str, data: dict[str, Any]) -> None:
        await self.ws.broadcast(WSEvent(type=event_type, agent_id=agent_id, data=data))

    async def _emit_status(self, agent_id: str) -> None:
        agent = self.agents[agent_id]
        await self._emit(agent_id, "agent_status", {
            "status": agent.status.value,
            "currentTask": agent.current_task,
            "sessionId": agent.session_id,
            "startedAt": agent.started_at.isoformat() if agent.started_at else None,
            "finishedAt": agent.finished_at.isoformat() if agent.finished_at else None,
        })
