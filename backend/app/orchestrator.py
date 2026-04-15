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
from .ws_manager import WSManager

logger = logging.getLogger(__name__)

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        query,
    )
except ImportError:
    logger.warning(
        "claude-agent-sdk not installed. Install with: pip install claude-agent-sdk"
    )
    query = None  # type: ignore[assignment]

# Pricing per million tokens (approximate, USD) — used as fallback when
# ResultMessage.total_cost_usd is not available.
_PRICING: dict[str, dict[str, float]] = {
    "opus": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_creation": 18.75},
    "sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_creation": 3.75},
    "haiku": {"input": 0.8, "output": 4.0, "cache_read": 0.08, "cache_creation": 1.0},
}


def _estimate_cost(model: str, usage: AgentUsage) -> float:
    key = model.split("-")[0] if "-" in model else model
    p = _PRICING.get(key, _PRICING["sonnet"])
    return (
        usage.input_tokens * p["input"]
        + usage.output_tokens * p["output"]
        + usage.cache_read_tokens * p["cache_read"]
        + usage.cache_creation_tokens * p["cache_creation"]
    ) / 1_000_000


class Orchestrator:
    """Manages agent lifecycles and coordinates multi-agent pipelines."""

    def __init__(self, ws: WSManager, ctx: ContextManager, config_dir: str, project_dir: str | None = None) -> None:
        self.ws = ws
        self.ctx = ctx
        self.config_dir = config_dir
        self.project_dir = project_dir or ctx.workspace_dir

        self.roles: dict[str, AgentRole] = {}
        self.agents: dict[str, AgentState] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._message_queues: dict[str, asyncio.Queue[str]] = {}
        self._role_map: dict[str, AgentRole] = {}  # agent_id -> role

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
        logger.info("Created agent %s (role=%s)", agent_id, role_id)
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
        agent = self.agents.get(agent_id)
        if agent:
            agent.status = AgentStatus.IDLE

    async def send_message(self, agent_id: str, content: str) -> None:
        """Send a user message to an agent.

        If the agent is idle (has a previous session), resume it with this message.
        If running, queue the message for delivery after the current turn.
        """
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
        if query is None:
            raise RuntimeError(
                "claude-agent-sdk is not installed. "
                "Install with: pip install claude-agent-sdk"
            )

        agent = self.agents[agent_id]
        role = self._role_map[agent_id]

        agent.status = AgentStatus.RUNNING
        agent.started_at = datetime.now()
        agent.current_task = prompt[:200]
        agent.usage = AgentUsage()
        await self._emit_status(agent_id)

        self.ctx.update_status(agent_id, "running", prompt[:200])

        # Build the full prompt with context from other agents
        full_prompt = prompt
        if context_from:
            ctx_section = self.ctx.build_context_prompt(context_from)
            if ctx_section:
                full_prompt = f"{ctx_section}\n\n---\n\nYour task:\n{prompt}"

        try:
            options = ClaudeAgentOptions(
                allowed_tools=role.tools,
                model=role.model,
                system_prompt=role.system_prompt,
                cwd=str(self.project_dir),
                max_turns=role.max_turns,
                resume=agent.session_id if agent.session_id else None,
            )

            result_text = ""
            async for message in query(prompt=full_prompt, options=options):
                await self._handle_message(agent_id, role.model, message)

                if isinstance(message, ResultMessage):
                    agent.session_id = message.session_id
                    if message.result:
                        result_text = message.result
                    # Use SDK-reported cost & usage when available
                    if message.total_cost_usd is not None:
                        agent.usage.cost_usd = message.total_cost_usd
                    if message.usage:
                        agent.usage.input_tokens = message.usage.get("input_tokens", 0)
                        agent.usage.output_tokens = message.usage.get("output_tokens", 0)
                        agent.usage.cache_read_tokens = message.usage.get("cache_read_input_tokens", 0)
                        agent.usage.cache_creation_tokens = message.usage.get("cache_creation_input_tokens", 0)
                    await self._emit(agent_id, "agent_usage", agent.usage.model_dump())

            # Save the final result to the context MD file
            if result_text:
                self.ctx.set_result(agent_id, role.name, prompt[:200], result_text)
                await self._emit(agent_id, "context_update", {"content": self.ctx.read(agent_id)})

            agent.status = AgentStatus.COMPLETED
            agent.finished_at = datetime.now()

            # Check if there are queued messages
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
            self.ctx.update_status(agent_id, agent.status.value)
            await self._emit_status(agent_id)

    async def _handle_message(self, agent_id: str, model: str, message: Any) -> None:
        agent = self.agents[agent_id]

        # --- AssistantMessage ---
        if isinstance(message, AssistantMessage):
            # Per-message usage tracking
            if message.usage:
                u = message.usage
                agent.usage.input_tokens += u.get("input_tokens", 0)
                agent.usage.output_tokens += u.get("output_tokens", 0)
                agent.usage.cache_read_tokens += u.get("cache_read_input_tokens", 0)
                agent.usage.cache_creation_tokens += u.get("cache_creation_input_tokens", 0)
                agent.usage.cost_usd = _estimate_cost(model, agent.usage)
                await self._emit(agent_id, "agent_usage", agent.usage.model_dump())

            # Content blocks
            for block in message.content:
                text = ""
                entry_type = "text"
                if isinstance(block, TextBlock):
                    text = block.text
                elif isinstance(block, ToolUseBlock):
                    text = f"[Tool: {block.name}] {str(block.input)[:300]}"
                    entry_type = "tool_use"
                elif isinstance(block, ToolResultBlock):
                    text = str(block.content)[:500] if block.content else ""
                    entry_type = "tool_result"
                if text:
                    entry = OutputEntry(type=entry_type, content=text)
                    agent.output_log.append(entry)
                    await self._emit(agent_id, "agent_output", {
                        "type": entry_type,
                        "text": text,
                        "timestamp": entry.timestamp.isoformat(),
                    })

        # --- ResultMessage ---
        elif isinstance(message, ResultMessage):
            if message.result:
                entry = OutputEntry(type="result", content=message.result)
                agent.output_log.append(entry)
                await self._emit(agent_id, "agent_output", {
                    "type": "result",
                    "text": message.result,
                    "timestamp": entry.timestamp.isoformat(),
                })

        # --- SystemMessage (e.g. init) ---
        elif isinstance(message, SystemMessage):
            logger.debug("Agent %s system message: subtype=%s", agent_id, message.subtype)

    # ------------------------------------------------------------------ #
    #  Pipeline execution                                                 #
    # ------------------------------------------------------------------ #

    async def run_pipeline(
        self,
        requirement: str,
        stages: list[PipelineStage] | None = None,
    ) -> None:
        """Run a multi-stage pipeline. Stages run sequentially; agents within
        a parallel stage run concurrently."""
        if stages is None:
            stages = self._default_pipeline()

        # Notify pipeline start
        await self.ws.broadcast_raw({
            "type": "pipeline_status",
            "data": {
                "status": "running",
                "requirement": requirement[:200],
                "stages": [s.model_dump() for s in stages],
                "current_stage": 0,
            },
        })

        # Create agents for each stage
        stage_agents: list[list[str]] = []
        for stage in stages:
            agent_ids: list[str] = []
            for role_id in stage.agents:
                aid = self.create_agent(role_id).id
                agent_ids.append(aid)
            stage_agents.append(agent_ids)

        # Execute stages
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
                prompt = requirement
            else:
                prompt = (
                    f"Based on the previous agents' work (see their context documents), "
                    f"continue with the following requirement:\n\n{requirement}"
                )

            if stage.parallel:
                tasks = [
                    self._run_agent(aid, prompt, context_from=prior_ids)
                    for aid in agent_ids
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
            else:
                for aid in agent_ids:
                    await self._run_agent(aid, prompt, context_from=prior_ids)

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
