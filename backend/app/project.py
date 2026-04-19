from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Any

from .context_manager import ContextManager
from .models import (
    AgentRole,
    AgentState,
    AgentStatus,
    AgentUsage,
    OutputEntry,
    PermissionMode,
    ProjectMeta,
    WSEvent,
)
from .providers import ProviderMessage, get_adapter
from .ws_manager import WSManager

logger = logging.getLogger(__name__)

_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

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


class Project:
    """One isolated project: its own agents, tasks, queues, permission mode,
    context directory. All long-running runtime state that used to live on
    ``Orchestrator`` now lives here, so multiple projects can run side by side
    without cross-talk.
    """

    def __init__(
        self,
        meta: ProjectMeta,
        ws: WSManager,
        roles: dict[str, AgentRole],
        workspace_root: str,
    ) -> None:
        self.meta = meta
        self.ws = ws
        self.roles = roles  # shared dict; readonly from here
        self.workspace_dir = os.path.join(workspace_root, meta.id)
        os.makedirs(self.workspace_dir, exist_ok=True)
        self.ctx = ContextManager(self.workspace_dir)

        self.agents: dict[str, AgentState] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._message_queues: dict[str, asyncio.Queue[str]] = {}
        self._role_map: dict[str, AgentRole] = {}
        self._pending_permissions: dict[str, asyncio.Future[bool]] = {}

        # Global default permission mode for this project. Runtime-only.
        self.permission_mode: PermissionMode = "manual"

    # ------------------------------------------------------------------ #
    #  Convenience accessors                                              #
    # ------------------------------------------------------------------ #

    @property
    def id(self) -> str:
        return self.meta.id

    @property
    def project_dir(self) -> str:
        return self.meta.project_dir

    # ------------------------------------------------------------------ #
    #  Agent CRUD                                                         #
    # ------------------------------------------------------------------ #

    def create_agent(self, role_id: str, agent_id: str | None = None) -> AgentState:
        role = self.roles.get(role_id)
        if role is None:
            raise ValueError(f"Unknown role: {role_id}")
        if agent_id is None:
            agent_id = f"{role_id}-{uuid.uuid4().hex[:6]}"
        if not _AGENT_ID_RE.match(agent_id):
            raise ValueError(
                f"Invalid agent_id {agent_id!r}: must match [A-Za-z0-9._-]{{1,64}}"
            )
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
        logger.info(
            "[%s] created agent %s (role=%s, provider=%s)",
            self.id, agent_id, role_id, role.provider,
        )
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
    #  Permission handling                                                #
    # ------------------------------------------------------------------ #

    def set_permission_mode(self, mode: PermissionMode) -> None:
        self.permission_mode = mode
        logger.info("[%s] default permission mode -> %s", self.id, mode)

    def set_agent_permission_mode(
        self, agent_id: str, mode: PermissionMode | None
    ) -> None:
        agent = self.get_agent(agent_id)
        agent.permission_mode = mode
        logger.info(
            "[%s] agent %s permission mode -> %s",
            self.id, agent_id, mode or "(inherit)",
        )

    def _effective_mode(self, agent_id: str) -> PermissionMode:
        agent = self.agents.get(agent_id)
        if agent and agent.permission_mode:
            return agent.permission_mode
        return self.permission_mode

    def _path_in_workspace(self, path: str) -> bool:
        """Return True if *path* resolves inside this project's ``project_dir``."""
        if not path:
            return False
        try:
            if not os.path.isabs(path):
                path = os.path.join(self.project_dir, path)
            full = os.path.realpath(path)
            root = os.path.realpath(self.project_dir)
            return os.path.commonpath([full, root]) == root
        except (ValueError, OSError):
            return False

    async def _auto_approve(
        self, agent_id: str, tool_name: str, tool_input: dict, label: str
    ) -> None:
        preview = ""
        if tool_name in ("Write", "Edit"):
            preview = str(tool_input.get("file_path", ""))
        elif tool_name == "Bash":
            preview = str(tool_input.get("command", ""))[:120]
        msg = f"[{label}] {tool_name} {preview}".strip()
        agent = self.agents.get(agent_id)
        if agent is None:
            return
        entry = OutputEntry(type="permission", content=msg)
        agent.output_log.append(entry)
        await self._emit(agent_id, "agent_output", {
            "type": "permission",
            "text": msg,
            "timestamp": entry.timestamp.isoformat(),
        })

    async def request_permission(
        self,
        agent_id: str,
        request_id: str,
        tool_name: str,
        tool_input: dict,
    ) -> bool:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending_permissions[request_id] = future

        await self._emit(agent_id, "agent_permission_request", {
            "request_id": request_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
        })

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
            logger.warning("[%s] permission request %s timed out", self.id, request_id)
            return False
        except asyncio.CancelledError:
            resolution = "cancelled"
            raise
        finally:
            self._pending_permissions.pop(request_id, None)
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
            return
        loop.create_task(self._emit(agent_id, "agent_permission_resolved", {
            "request_id": request_id,
            "allow": allowed,
            "resolution": resolution,
        }))

    def _cleanup_pending_permissions(self, agent_id: str, reason: str = "agent_stopped") -> None:
        prefix = f"{agent_id}-"
        for request_id in list(self._pending_permissions.keys()):
            if not request_id.startswith(prefix):
                continue
            future = self._pending_permissions.pop(request_id, None)
            if future and not future.done():
                future.set_result(False)
            self._broadcast_resolution(agent_id, request_id, False, reason)

    async def resolve_permission(self, request_id: str, allow: bool) -> None:
        future = self._pending_permissions.get(request_id)
        if future and not future.done():
            future.set_result(allow)

    def has_pending_permission(self, request_id: str) -> bool:
        return request_id in self._pending_permissions

    # ------------------------------------------------------------------ #
    #  Messaging                                                          #
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
        adapter = get_adapter(role.provider)

        async def _perm_cb(tool_name: str, tool_input: dict) -> bool:
            mode = self._effective_mode(agent_id)
            if mode == "bypass":
                await self._auto_approve(agent_id, tool_name, tool_input, "bypass")
                return True
            if mode == "workspace" and tool_name in ("Write", "Edit"):
                path = tool_input.get("file_path", "") if isinstance(tool_input, dict) else ""
                if self._path_in_workspace(str(path)):
                    await self._auto_approve(
                        agent_id, tool_name, tool_input, "workspace-auto"
                    )
                    return True
            rid = f"{agent_id}-{uuid.uuid4().hex[:8]}"
            return await self.request_permission(agent_id, rid, tool_name, tool_input)

        current_prompt = prompt
        current_context_from = context_from

        try:
            while True:
                agent.status = AgentStatus.RUNNING
                agent.started_at = datetime.now()
                agent.current_task = current_prompt[:200]
                agent.usage = AgentUsage()
                await self._emit_status(agent_id)
                self.ctx.update_status(agent_id, "running", current_prompt[:200])

                user_entry = OutputEntry(type="user", content=current_prompt[:2000])
                agent.output_log.append(user_entry)
                await self._emit(agent_id, "agent_output", {
                    "type": "user",
                    "text": current_prompt[:2000],
                    "timestamp": user_entry.timestamp.isoformat(),
                })

                full_prompt = current_prompt
                if current_context_from:
                    ctx_section = self.ctx.build_context_prompt(current_context_from)
                    if ctx_section:
                        full_prompt = f"{ctx_section}\n\n---\n\nYour task:\n{current_prompt}"

                result_text = ""
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

                if result_text:
                    self.ctx.set_result(agent_id, role.name, current_prompt[:200], result_text)
                    await self._emit(agent_id, "context_update", {"content": self.ctx.read(agent_id)})

                agent.status = AgentStatus.COMPLETED
                agent.finished_at = datetime.now()

                queue = self._message_queues.get(agent_id)
                if queue is None or queue.empty():
                    break
                current_prompt = queue.get_nowait()
                current_context_from = None

        except asyncio.CancelledError:
            agent.status = AgentStatus.IDLE
            logger.info("[%s] agent %s cancelled", self.id, agent_id)
            raise
        except Exception as e:
            agent.status = AgentStatus.ERROR
            agent.output_log.append(OutputEntry(type="error", content=str(e)))
            await self._emit(agent_id, "agent_error", {"error": str(e)})
            logger.exception("[%s] agent %s failed", self.id, agent_id)
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
            agent.usage.input_tokens = msg.usage.get("input_tokens", agent.usage.input_tokens)
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

        if msg.type in ("text", "tool_use", "tool_result") and msg.content:
            entry = OutputEntry(type=msg.type, content=msg.content)
            agent.output_log.append(entry)
            await self._emit(agent_id, "agent_output", {
                "type": msg.type,
                "text": msg.content,
                "timestamp": entry.timestamp.isoformat(),
            })

    # ------------------------------------------------------------------ #
    #  Event helpers                                                      #
    # ------------------------------------------------------------------ #

    async def _emit(self, agent_id: str, event_type: str, data: dict[str, Any]) -> None:
        await self.ws.broadcast(
            WSEvent(type=event_type, agent_id=agent_id, project_id=self.id, data=data)
        )

    async def _emit_status(self, agent_id: str) -> None:
        agent = self.agents[agent_id]
        await self._emit(agent_id, "agent_status", {
            "status": agent.status.value,
            "currentTask": agent.current_task,
            "sessionId": agent.session_id,
            "startedAt": agent.started_at.isoformat() if agent.started_at else None,
            "finishedAt": agent.finished_at.isoformat() if agent.finished_at else None,
        })

    async def broadcast_raw(self, data: dict[str, Any]) -> None:
        """Broadcast a raw event scoped to this project."""
        payload = dict(data)
        payload.setdefault("project_id", self.id)
        await self.ws.broadcast_raw(payload)
