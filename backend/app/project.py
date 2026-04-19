from __future__ import annotations

import asyncio
import logging
import os
import re
import traceback
import uuid
from datetime import datetime
from typing import Any

from .context_manager import ContextManager
from .errors import ErrorInfo, ErrorLog, classify_error, retry_delay
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
from .persistence import AgentStore, StreamStore
from .providers import ProviderMessage, get_adapter
from .providers.base import ProviderAdapter
from .ws_manager import WSManager

logger = logging.getLogger(__name__)

_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DIRECTIVE_RE = re.compile(r"^>>>\s+(START|SPAWN|DONE)\b\s*(.*?)\s*$", re.MULTILINE)

COORDINATOR_ROLE_ID = "coordinator"

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
        self.errors = ErrorLog(self.workspace_dir)
        self.agent_store = AgentStore(self.workspace_dir)
        self.streams = StreamStore(self.workspace_dir)

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
        self._save()
        logger.info(
            "[%s] created agent %s (role=%s, provider=%s)",
            self.id, agent_id, role_id, role.provider,
        )
        return state

    def delete_agent(self, agent_id: str) -> None:
        self.stop_agent(agent_id)
        self.ctx.delete(agent_id)
        self.streams.delete(agent_id)
        self.agents.pop(agent_id, None)
        self._role_map.pop(agent_id, None)
        self._message_queues.pop(agent_id, None)
        self._save()

    # ------------------------------------------------------------------ #
    #  Persistence                                                        #
    # ------------------------------------------------------------------ #

    def _save(self) -> None:
        """Snapshot agent metadata to disk. Cheap — atomic write."""
        self.agent_store.save(self.agents)

    def _log_entry(self, agent_id: str, entry: OutputEntry) -> None:
        """Append to both in-memory output_log and the on-disk stream."""
        agent = self.agents.get(agent_id)
        if agent is None:
            return
        agent.output_log.append(entry)
        self.streams.append(agent_id, entry)

    def rehydrate(self) -> None:
        """Load agents + recent output tail from disk on startup.

        Running tasks cannot survive a restart, so every agent is restored
        with status=IDLE. Session ids are preserved, so users can resume
        conversations. Missing roles (a role was removed while the backend
        was down) skip the entry but log a warning.
        """
        for entry in self.agent_store.load():
            try:
                state = AgentState(**entry)
            except Exception as e:
                logger.warning("[%s] skipping malformed agent entry: %s", self.id, e)
                continue
            role = self.roles.get(state.role_id)
            if role is None:
                logger.warning(
                    "[%s] agent %s references missing role %s; skipping",
                    self.id, state.id, state.role_id,
                )
                continue
            if state.status not in (AgentStatus.IDLE, AgentStatus.COMPLETED, AgentStatus.ERROR):
                state.status = AgentStatus.IDLE
            # Reload last 500 stream entries (drops whatever was in output_log)
            tail = self.streams.tail(state.id, limit=500)
            if tail:
                state.output_log = tail
            self.agents[state.id] = state
            self._role_map[state.id] = role
            self._message_queues[state.id] = asyncio.Queue()
        logger.info("[%s] rehydrated %d agents", self.id, len(self.agents))

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
        self._log_entry(agent_id, entry)
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

        entry = OutputEntry(type="permission", content=f"Requesting permission: {tool_name}")
        self._log_entry(agent_id, entry)
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
    #  Coordinator                                                        #
    # ------------------------------------------------------------------ #

    def _find_coordinator(self) -> str | None:
        """Return the id of the project's coordinator agent, if one exists
        and hasn't already been marked ``DONE`` / errored.

        If multiple coordinators are registered we return the most recently
        created one (dict-insertion order).
        """
        last: str | None = None
        for aid, agent in self.agents.items():
            if agent.role_id != COORDINATOR_ROLE_ID:
                continue
            if agent.status == AgentStatus.ERROR:
                continue
            last = aid
        return last

    async def _notify_coordinator(self, source_agent_id: str, summary: str) -> None:
        """Enqueue an ``[AGENT_DONE]`` message to the coordinator, if any.

        Never notifies the coordinator about its own completion — that would
        loop forever.
        """
        coord_id = self._find_coordinator()
        if not coord_id or coord_id == source_agent_id:
            return
        msg = (
            f"[AGENT_DONE] {source_agent_id} finished: {summary[:200]}\n"
            f"Context written to workspace/{self.id}/context/{source_agent_id}.md"
        )
        await self.send_message(coord_id, msg)

    async def _process_coordinator_directives(
        self, coord_id: str, result_text: str
    ) -> None:
        """Parse ``>>>`` directives from the coordinator's last turn and act on them.

        Supported:
          >>> START <agent_id> <prompt>
          >>> SPAWN <role_id> <agent_id> <prompt>
          >>> DONE
        """
        if not result_text:
            return
        for cmd, raw in _DIRECTIVE_RE.findall(result_text):
            try:
                if cmd == "DONE":
                    agent = self.agents.get(coord_id)
                    if agent:
                        agent.status = AgentStatus.COMPLETED
                        agent.current_task = None
                    logger.info("[%s] coordinator %s marked DONE", self.id, coord_id)
                elif cmd == "START":
                    parts = raw.split(None, 1)
                    if len(parts) < 2:
                        logger.warning("[%s] START directive malformed: %r", self.id, raw)
                        continue
                    target, prompt = parts[0], parts[1]
                    if target not in self.agents:
                        logger.warning("[%s] START references unknown agent %s", self.id, target)
                        continue
                    await self.send_message(target, prompt)
                    logger.info("[%s] coordinator dispatched START -> %s", self.id, target)
                elif cmd == "SPAWN":
                    parts = raw.split(None, 2)
                    if len(parts) < 3:
                        logger.warning("[%s] SPAWN directive malformed: %r", self.id, raw)
                        continue
                    role_id, new_aid, prompt = parts[0], parts[1], parts[2]
                    self.create_agent(role_id, new_aid)
                    self.start_agent(new_aid, prompt)
                    logger.info(
                        "[%s] coordinator spawned %s (role=%s)",
                        self.id, new_aid, role_id,
                    )
            except Exception as e:
                logger.warning(
                    "[%s] coordinator directive %s failed: %s", self.id, cmd, e,
                )

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
                self._log_entry(agent_id, user_entry)
                await self._emit(agent_id, "agent_output", {
                    "type": "user",
                    "text": current_prompt[:2000],
                    "timestamp": user_entry.timestamp.isoformat(),
                })
                self._save()

                full_prompt = current_prompt
                if current_context_from:
                    ctx_section = self.ctx.build_context_prompt(current_context_from)
                    if ctx_section:
                        full_prompt = f"{ctx_section}\n\n---\n\nYour task:\n{current_prompt}"

                result_text = await self._run_sdk_with_retry(
                    agent_id=agent_id,
                    role=role,
                    adapter=adapter,
                    full_prompt=full_prompt,
                    perm_cb=_perm_cb,
                )

                if result_text:
                    self.ctx.set_result(agent_id, role.name, current_prompt[:200], result_text)
                    await self._emit(agent_id, "context_update", {"content": self.ctx.read(agent_id)})

                agent.status = AgentStatus.COMPLETED
                agent.finished_at = datetime.now()

                # Coordinator-specific post-turn handling: parse directives,
                # then notify *other* agents via the directive dispatcher.
                # Non-coordinators notify the coordinator about their
                # completion so it can route next steps.
                if role.id == COORDINATOR_ROLE_ID:
                    await self._process_coordinator_directives(agent_id, result_text)
                else:
                    summary = (result_text or current_prompt)[:200]
                    await self._notify_coordinator(agent_id, summary)

                queue = self._message_queues.get(agent_id)
                if queue is None or queue.empty():
                    break
                current_prompt = queue.get_nowait()
                current_context_from = None

        except asyncio.CancelledError:
            agent.status = AgentStatus.IDLE
            logger.info("[%s] agent %s cancelled", self.id, agent_id)
            raise
        except Exception as exc:
            agent.status = AgentStatus.ERROR
            category, _ = classify_error(exc)
            info = ErrorInfo(
                agent_id=agent_id,
                project_id=self.id,
                category=category,
                message=str(exc) or type(exc).__name__,
                stack=traceback.format_exc(),
                recoverable=False,
                final=True,
            )
            self.errors.append(info)
            self._log_entry(agent_id, OutputEntry(type="error", content=info.message))
            await self._emit(agent_id, "agent_error", info.model_dump(mode="json"))
            logger.exception("[%s] agent %s failed (%s)", self.id, agent_id, category)
        finally:
            self._tasks.pop(agent_id, None)
            self._cleanup_pending_permissions(agent_id, reason="agent_ended")
            self.ctx.update_status(agent_id, agent.status.value)
            await self._emit_status(agent_id)
            self._save()

    async def _run_sdk_with_retry(
        self,
        *,
        agent_id: str,
        role: AgentRole,
        adapter: ProviderAdapter,
        full_prompt: str,
        perm_cb: Any,
    ) -> str:
        """Run one adapter.run() pass with category-aware retries.

        Returns the final ``result`` text from the SDK. If a non-recoverable
        error occurs or retries are exhausted, re-raises so the outer handler
        in :meth:`_run_agent` can log a final ErrorInfo and mark the agent
        errored.

        Each transient error is logged with ``final=False`` and broadcast as
        ``agent_error`` so the UI can show the retry trail in real time.
        """
        agent = self.agents[agent_id]
        attempt = 0

        while True:
            result_text = ""
            # Reset usage for the fresh pass — the previous partial tokens
            # would otherwise double-count if the SDK restarts mid-call.
            if attempt > 0:
                agent.usage = AgentUsage()

            try:
                async for msg in adapter.run(
                    prompt=full_prompt,
                    system_prompt=role.system_prompt,
                    model=role.model,
                    tools=role.tools,
                    cwd=str(self.project_dir),
                    max_turns=role.max_turns,
                    session_id=agent.session_id,
                    effort=role.effort,
                    permission_callback=perm_cb,
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
                            agent.usage.input_tokens = msg.usage.get(
                                "input_tokens", agent.usage.input_tokens
                            )
                            agent.usage.output_tokens = msg.usage.get(
                                "output_tokens", agent.usage.output_tokens
                            )
                            agent.usage.cache_read_tokens = msg.usage.get(
                                "cache_read_input_tokens", 0
                            )
                            agent.usage.cache_creation_tokens = msg.usage.get(
                                "cache_creation_input_tokens", 0
                            )
                        await self._emit(
                            agent_id, "agent_usage", agent.usage.model_dump()
                        )
                return result_text

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                category, recoverable = classify_error(exc)
                attempt += 1
                delay = retry_delay(category, attempt) if recoverable else None
                if delay is None:
                    # Either unrecoverable or retries exhausted — let outer
                    # handler log the final record and halt the agent.
                    raise

                info = ErrorInfo(
                    agent_id=agent_id,
                    project_id=self.id,
                    category=category,
                    message=str(exc) or type(exc).__name__,
                    stack=traceback.format_exc(),
                    recoverable=True,
                    retry_count=attempt - 1,
                    final=False,
                )
                self.errors.append(info)
                self._log_entry(
                    agent_id,
                    OutputEntry(
                        type="error",
                        content=f"[{category}] {info.message} — retry {attempt} in {delay:.0f}s",
                    ),
                )
                await self._emit(agent_id, "agent_error", info.model_dump(mode="json"))
                logger.warning(
                    "[%s] agent %s %s (attempt %d): %s — retrying in %.1fs",
                    self.id, agent_id, category, attempt, exc, delay,
                )
                await asyncio.sleep(delay)

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
            self._log_entry(agent_id, entry)
            await self._emit(agent_id, "agent_error", {"error": msg.content})
            return

        if msg.type in ("text", "tool_use", "tool_result") and msg.content:
            entry = OutputEntry(type=msg.type, content=msg.content)
            self._log_entry(agent_id, entry)
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
