from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from .budget import BudgetExceeded, BudgetTracker
from .context_manager import ContextManager
from .coordinator_tools import build_coordinator_mcp_server
from .errors import ErrorInfo, ErrorLog, classify_error, retry_delay
from .events import Event, EventQueue
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

# psutil is optional. When present, we use it for cross-platform process-tree
# enumeration. On Windows we always have the `taskkill /T /F` fallback which
# handles the actual tree-kill well enough; psutil just gives us a cleaner
# leaf-first shutdown elsewhere.
try:
    import psutil  # type: ignore[import-not-found]

    _HAS_PSUTIL = True
except ImportError:
    psutil = None  # type: ignore[assignment]
    _HAS_PSUTIL = False

_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DIRECTIVE_RE = re.compile(r"^>>>\s+(START|SPAWN|DONE)\b\s*(.*?)\s*$", re.MULTILINE)

DEFAULT_COORDINATOR_ROLE_ID = "coordinator"

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


# --------------------------------------------------------------------------- #
#  Pipeline runtime state (v0.3.0 stage-gate protocol)                         #
# --------------------------------------------------------------------------- #


GateAction = Literal["APPROVE", "RETRY", "ABORT"]


@dataclass
class GateVerdict:
    """Coordinator's decision on a STAGE_COMPLETE review.

    ``action`` drives the orchestrator's next move:
      * ``APPROVE`` — advance to the next stage.
      * ``RETRY``   — re-prompt every agent listed in ``agents`` with
        ``instruction`` appended to the original stage prompt, then re-emit
        STAGE_COMPLETE for another review.
      * ``ABORT``   — unrecoverable failure; pipeline terminates with
        status=failed and ``summary`` surfaces as the reason.

    Phase 1 will add ``SPAWN`` (via ``spawn_and_rework``).
    """

    action: GateAction
    summary: str = ""
    agents: list[str] = field(default_factory=list)
    instruction: str = ""
    # Agent ids that ``spawn_and_rework`` freshly created for this retry.
    # The orchestrator adds these to the stage roster so subsequent
    # STAGE_COMPLETE messages report them too.
    spawned_agents: list[str] = field(default_factory=list)


@dataclass
class PipelineState:
    """Runtime state for an active stage-gate pipeline on one project.

    ``gate_verdict_ready`` is an :class:`asyncio.Event` the orchestrator
    awaits after emitting STAGE_COMPLETE; it is set by the coordinator's
    MCP tools (``approve_stage`` / ``request_rework``). ``gate_verdict``
    carries the tool's payload across that handoff.

    ``pause_reason`` is set when the coordinator crashes mid-review or
    the user explicitly pauses the pipeline; the orchestrator then
    switches to awaiting ``resume_ready`` instead. ``resume_action``
    carries the user's choice (retry the coord invocation, or
    force-advance past the gate) across that handoff.

    Created lazily via :meth:`Project.reset_pipeline` on each
    ``run_pipeline`` invocation so that a second pipeline run does not
    inherit a stale verdict or retry counter.
    """

    coordinator_agent_id: str | None = None
    current_stage_name: str | None = None
    gate_verdict: GateVerdict | None = None
    gate_verdict_ready: asyncio.Event = field(default_factory=asyncio.Event)
    stage_retries: dict[str, int] = field(default_factory=dict)
    # Pause / resume channel — used when the coord errors mid-review or
    # the user hits the pause button. ``pause_reason`` is set alongside
    # the fire of ``gate_verdict_ready`` so the orchestrator wakes, sees
    # a pause state (rather than a verdict), and switches to waiting for
    # ``resume_ready``.
    pause_reason: str | None = None
    resume_action: Literal["retry", "force_advance"] | None = None
    resume_ready: asyncio.Event = field(default_factory=asyncio.Event)


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
        # (pid, job_handle) pairs for each agent's live claude.exe subprocess.
        # Populated via the SDK adapter's ``pid_callback``; drained on
        # tree-kill so orphan bash/pnpm/node workers die with their parent.
        # ``job_handle`` is a Windows Job Object handle (KILL_ON_JOB_CLOSE)
        # owned by the SDK transport — we *terminate* it on stop but never
        # close it (transport closes it in its own ``close()``).
        self._sdk_pids: dict[str, list[tuple[int, int | None]]] = {}

        # Global default permission mode for this project. Runtime-only.
        self.permission_mode: PermissionMode = "manual"

        # Budget — caps loaded live from workflow.yaml; usage is
        # accumulated across the project's lifetime (runtime-only for now).
        self.budget = BudgetTracker(self)

        # Workflow event log. Triggers and coordinator both read from here.
        self.events = EventQueue()

        # Stage-gate pipeline state (reset at each ``run_pipeline`` entry).
        # Living on ``Project`` rather than inside ``run_pipeline`` lets the
        # coordinator's MCP tools (running in a different coroutine task)
        # signal verdicts back to the waiting orchestrator.
        self.pipeline: PipelineState = PipelineState()

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
        # Tree-kill any claude.exe subprocesses first so we don't leave
        # orphan workers chewing CPU after the agent record is gone.
        self._kill_agent_process_tree(agent_id)
        task = self._tasks.pop(agent_id, None)
        if task and not task.done():
            task.cancel()
        self._cleanup_pending_permissions(agent_id, reason="deleted")
        self.ctx.delete(agent_id)
        self.streams.delete(agent_id)
        self.agents.pop(agent_id, None)
        self._role_map.pop(agent_id, None)
        self._message_queues.pop(agent_id, None)
        self._sdk_pids.pop(agent_id, None)
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
        # Gate on workflow.yaml budget. BudgetExceeded bubbles up to the
        # caller (API layer) so the user sees why the start was refused.
        self.budget.check_can_start()
        if self.budget.usage.started_at is None:
            self.budget.start()
        # Invalidate any prior completion so AND-join triggers don't
        # re-fire off a stale ``completed`` flag.
        self.events.clear_completed(agent_id)
        task = asyncio.create_task(self._run_agent(agent_id, prompt, context_from))
        self._tasks[agent_id] = task

    def stop_agent(self, agent_id: str) -> None:
        """Terminate a running agent, tree-killing its subprocess tree first.

        Order matters: killing ``claude.exe`` and its descendants first
        unblocks the Python awaits naturally with ``BrokenPipeError``. If
        we cancel the asyncio task first, cancellation cannot propagate
        across the CLI subprocess boundary and agents hang.

        Scheduled :meth:`_finalize_agent` runs via the running loop so the
        WS broadcast still fires even when no ``_run_agent`` is executing
        (e.g. an agent that was started via ``/message`` and got stuck on
        its first tool call — the exception handler never ran, so without
        an explicit finalize the card would stay ``running`` forever).
        """
        # 1. Tree-kill the CLI subprocess. After this, the asyncio task
        #    awaiting SDK output sees BrokenPipeError / ClosedResourceError
        #    almost immediately.
        self._kill_agent_process_tree(agent_id)

        # 2. Cancel the asyncio task — any awaits that survived the pipe
        #    closure get a CancelledError.
        task = self._tasks.pop(agent_id, None)
        if task and not task.done():
            task.cancel()

        # 3. Release pending permission futures so any caller awaiting
        #    them doesn't deadlock.
        self._cleanup_pending_permissions(agent_id, reason="stopped")

        # 4. Finalize agent status. Schedule the async finalize on the
        #    running loop so `stop_agent` stays sync-callable (FastAPI
        #    route handlers already call us without an await).
        agent = self.agents.get(agent_id)
        if agent is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(self._finalize_agent(agent_id, "idle", "Stopped by user"))
        else:
            # No running loop (startup / shutdown) — at least keep the
            # in-memory state consistent so a subsequent rehydrate is sane.
            agent.status = AgentStatus.IDLE
            agent.finished_at = datetime.now()
            self._save()

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
    #  Subprocess hygiene                                                 #
    # ------------------------------------------------------------------ #

    def _register_sdk_pid(
        self, agent_id: str, pid: int, job_handle: int | None = None,
    ) -> None:
        """Callback handed to the Claude adapter so we learn the CLI PID.

        The adapter invokes this from a worker thread once the SDK spawns
        ``claude.exe``. On Windows the adapter also hands us a Job Object
        handle (KILL_ON_JOB_CLOSE) owning that process — we keep it so
        :meth:`_kill_agent_process_tree` can terminate every descendant,
        including ones detached from the parent-PID chain (Task 8b).
        """
        if pid <= 0:
            return
        entries = self._sdk_pids.setdefault(agent_id, [])
        if any(existing_pid == pid for existing_pid, _ in entries):
            return
        entries.append((pid, job_handle))
        logger.info(
            "[%s] agent %s SDK pid registered: %d%s",
            self.id,
            agent_id,
            pid,
            f" (job=0x{job_handle:x})" if job_handle else "",
        )

    def _kill_agent_process_tree(self, agent_id: str) -> None:
        """Kill every surviving descendant of the agent's ``claude.exe`` PIDs.

        Called from :meth:`stop_agent`, :meth:`delete_agent`, and the
        subprocess-death handler.

        asyncio's task cancellation cannot cross the Python → claude.exe
        → bash → pnpm → node-worker boundary. Strategy:

        1. **Job Object terminate** (preferred, Windows-only). The SDK's
           Bash tool detaches background subprocesses (DETACHED_PROCESS /
           CREATE_NEW_PROCESS_GROUP), which re-parents them onto
           services.exe and hides them from ``taskkill /T``. Putting
           claude.exe in a Job with ``KILL_ON_JOB_CLOSE`` and calling
           ``TerminateJobObject`` kills every descendant regardless of
           detach flags.

        2. **PID tree-kill** (fallback). psutil or ``taskkill /T /F``
           for platforms without Job Objects, or when Job creation
           failed for any reason.
        """
        entries = self._sdk_pids.pop(agent_id, [])
        if not entries:
            return
        for pid, job_handle in entries:
            if job_handle:
                try:
                    from app.providers.claude_adapter import _terminate_job
                    if _terminate_job(job_handle):
                        logger.info(
                            "[%s] terminated job 0x%x for agent %s pid %d",
                            self.id, job_handle, agent_id, pid,
                        )
                        continue
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[%s] TerminateJobObject(0x%x) failed; falling back to PID kill",
                        self.id, job_handle,
                    )
            self._kill_pid_tree(pid)

    def _kill_pid_tree(self, pid: int) -> None:
        """Best-effort recursive kill of *pid* and all its descendants."""
        if pid <= 0:
            return
        # Preferred path: psutil — gives us cross-platform, leaf-first kill.
        if _HAS_PSUTIL:
            try:
                parent = psutil.Process(pid)
                descendants = parent.children(recursive=True)
                # Leaves first so killing a parent doesn't orphan grandchildren.
                for child in reversed(descendants):
                    try:
                        child.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                try:
                    parent.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                return
            except psutil.NoSuchProcess:
                return
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[%s] psutil tree-kill failed for pid %d; falling back",
                    self.id, pid,
                )
        # Windows fallback: taskkill /T /F handles the whole tree in one
        # shot and is universally available.
        if sys.platform == "win32":
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(pid)],
                    capture_output=True,
                    timeout=10,
                )
                return
            except Exception:  # noqa: BLE001
                logger.exception("[%s] taskkill failed for pid %d", self.id, pid)
        # POSIX fallback: process group kill via os.killpg is not reliable
        # because the SDK does not set a new pgid; the best we can do
        # without psutil is kill the parent and hope children noticed.
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        except Exception:  # noqa: BLE001
            logger.exception("[%s] os.kill failed for pid %d", self.id, pid)

    # ------------------------------------------------------------------ #
    #  Agent finalization                                                 #
    # ------------------------------------------------------------------ #

    async def _finalize_agent(
        self,
        agent_id: str,
        status: Literal["completed", "error", "idle"],
        reason: str = "",
    ) -> None:
        """Single exit point for an agent's SDK session.

        Every code path that terminates an agent's run — user stop, budget
        cap, SDK exception, subprocess death, coordinator mark_done —
        should converge here so we do not leave stale status fields,
        skipped WS broadcasts, or unflushed persistence.

        - Sets ``status`` and ``finished_at`` (``started_at`` stays intact
          so the UI can still show duration).
        - Appends a terminal output entry when *reason* is non-empty.
        - Broadcasts ``agent_status`` so the frontend flips the card.
        - Persists the updated agent map to disk.
        """
        agent = self.agents.get(agent_id)
        if agent is None:
            logger.warning(
                "[%s] _finalize_agent called for unknown agent %s", self.id, agent_id,
            )
            return
        try:
            new_status = AgentStatus(status)
        except ValueError:
            logger.warning(
                "[%s] _finalize_agent: invalid status %r for %s", self.id, status, agent_id,
            )
            return
        agent.status = new_status
        agent.finished_at = datetime.now()
        agent.current_task = None

        # Coord crashed mid-review — un-wedge the gate loop so it can
        # transition to paused state.  Same idea as an ABORT verdict,
        # but surfaced as a pause so the user has a chance to retry
        # the review rather than nuking the whole pipeline.
        if (
            new_status == AgentStatus.ERROR
            and self.pipeline.coordinator_agent_id == agent_id
            and self.pipeline.current_stage_name is not None
            and self.pipeline.gate_verdict is None
            and self.pipeline.pause_reason is None
        ):
            self.pipeline.pause_reason = (
                f"Coordinator errored during review: {reason[:200]}"
                if reason
                else "Coordinator errored during review"
            )
            self.pipeline.gate_verdict_ready.set()

        entry_type = "error" if new_status == AgentStatus.ERROR else "text"
        if reason:
            self._log_entry(agent_id, OutputEntry(type=entry_type, content=reason))
            await self._emit(agent_id, "agent_output", {
                "type": entry_type,
                "text": reason,
                "timestamp": datetime.now().isoformat(),
            })
        try:
            self.ctx.update_status(agent_id, new_status.value)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] ctx.update_status failed for %s", self.id, agent_id)
        await self._emit_status(agent_id)
        self._save()
        logger.info(
            "[%s] agent %s finalized -> %s (%s)",
            self.id, agent_id, new_status.value, reason[:80] if reason else "",
        )

    # ------------------------------------------------------------------ #
    #  Coordinator                                                        #
    # ------------------------------------------------------------------ #

    def reset_pipeline(self) -> PipelineState:
        """Replace :attr:`pipeline` with a fresh :class:`PipelineState`.

        Called at the top of each :meth:`orchestrator.run_pipeline` invocation
        so a second run does not inherit a stale ``gate_verdict`` or
        ``stage_retries`` counter from the previous run.
        """
        self.pipeline = PipelineState()
        return self.pipeline

    def _coord_role_id(self) -> str:
        """Resolve the coordinator role_id from ``workflow.yaml``.

        Falls back to :data:`DEFAULT_COORDINATOR_ROLE_ID` when the workflow
        file is absent, malformed, or has no coordinator block.  Read fresh
        on every call — this is a cold path and users may edit the workflow
        between invocations; we'd rather take a tiny I/O hit than serve
        stale state.
        """
        from .workflow import load_workflow
        wf = load_workflow(self.workspace_dir)
        if wf is not None and wf.coordinator is not None:
            return wf.coordinator.role_id
        return DEFAULT_COORDINATOR_ROLE_ID

    def _find_coordinator(self, role_id: str) -> str | None:
        """Return the id of the project's coordinator agent, if one exists
        and hasn't already been marked ``DONE`` / errored.

        ``role_id`` is the configured coordinator role (usually
        ``workflow.coordinator.role_id`` — see :meth:`_coord_role_id`).
        If multiple coordinators are registered we return the most recently
        created one (dict-insertion order).
        """
        last: str | None = None
        for aid, agent in self.agents.items():
            if agent.role_id != role_id:
                continue
            if agent.status == AgentStatus.ERROR:
                continue
            last = aid
        return last

    @property
    def coordinator_state_path(self) -> str:
        """Absolute path to the coordinator's running scratchpad."""
        return os.path.join(self.workspace_dir, "coordinator_state.md")

    def _context_file_path(self, agent_id: str) -> str:
        return os.path.join(self.workspace_dir, "context", f"{agent_id}.md")

    def _coordinator_runtime_header(self, *, mcp_available: bool) -> str:
        """Prepended to the coordinator's prompt each turn. Supplies the
        absolute paths the coordinator needs (its cwd is the user's project
        dir, so relative ``workspace/...`` paths don't resolve), a
        point-in-time roster of other agents, and the action protocol
        appropriate for the active provider."""
        roster: list[str] = []
        coord_role_id = self._coord_role_id()
        for aid, agent in self.agents.items():
            if agent.role_id == coord_role_id:
                continue
            ctx_path = self._context_file_path(aid)
            roster.append(
                f"  - {aid} ({agent.role_id}) — {agent.status.value} — context: {ctx_path}"
            )
        roster_block = "\n".join(roster) if roster else "  (no worker agents yet)"
        if mcp_available:
            protocol = (
                "## Action protocol\n"
                "Use these tools to route work — they execute immediately and\n"
                "report back success or the exact failure reason:\n"
                "  - `start_agent(agent_id, prompt)` — re-task an existing agent\n"
                "  - `spawn_agent(role_id, agent_id, prompt)` — create + run a new agent\n"
                "  - `mark_done()` — declare you have no further routing to do\n"
                "Do not emit `>>>` text directives — they are the fallback for\n"
                "non-Claude providers and will be ignored when tools succeed."
            )
        else:
            protocol = (
                "## Action protocol\n"
                "Emit directives as standalone lines at the end of your turn:\n"
                "  >>> START <agent_id> <prompt>\n"
                "  >>> SPAWN <role_id> <agent_id> <prompt>\n"
                "  >>> DONE"
            )
        return (
            "## Runtime paths (refreshed each turn)\n"
            "Use these **absolute** paths with your Read/Write tools — relative\n"
            "`workspace/...` paths will not resolve from your cwd.\n\n"
            f"- Your scratchpad: {self.coordinator_state_path}\n"
            f"- Context dir:    {os.path.join(self.workspace_dir, 'context')}\n\n"
            f"## Worker roster\n{roster_block}\n\n"
            f"{protocol}"
        )

    async def _notify_coordinator(self, source_agent_id: str, summary: str) -> None:
        """Enqueue an ``[AGENT_DONE]`` message to the coordinator, if any.

        Never notifies the coordinator about its own completion — that would
        loop forever.  Suppressed while a stage-gate pipeline is active
        (``self.pipeline.coordinator_agent_id`` set) — the orchestrator
        emits stage-level ``[STAGE_COMPLETE]`` messages instead, so the
        coordinator isn't spammed with per-worker AGENT_DONE during gated
        runs.
        """
        if self.pipeline.coordinator_agent_id is not None:
            return
        coord_id = self._find_coordinator(self._coord_role_id())
        if not coord_id or coord_id == source_agent_id:
            return
        ctx_path = self._context_file_path(source_agent_id)
        msg = (
            f"[AGENT_DONE] {source_agent_id} finished: {summary[:200]}\n"
            f"Context written to {ctx_path}"
        )
        await self.send_message(coord_id, msg)

    async def _send_pipeline_started(
        self,
        coord_id: str,
        requirement: str,
        stage_names: list[str],
    ) -> None:
        """Send the ``[PIPELINE_STARTED]`` inbox message and log the event.

        Fired once per :meth:`orchestrator.run_pipeline` invocation, right
        after the coordinator is auto-spawned.  The coordinator's system
        prompt instructs it to acknowledge with ``update_state`` and then
        wait for the first ``[STAGE_COMPLETE]``.
        """
        self.events.push(Event(
            kind="pipeline_started",
            detail={
                "requirement": requirement[:500],
                "stages": list(stage_names),
            },
        ))
        stages_rendered = "\n  ".join(f"- {n}" for n in stage_names) or "(none)"
        msg = (
            "[PIPELINE_STARTED]\n"
            f"Requirement: {requirement}\n"
            f"Stages:\n  {stages_rendered}\n"
            f"Workspace: {self.workspace_dir}\n\n"
            "You are the coordinator for this stage-gate pipeline. "
            "Acknowledge by calling update_state with your initial plan / "
            "hypothesis, then wait for [STAGE_COMPLETE] messages before "
            "making any gate decisions."
        )
        await self.send_message(coord_id, msg)

    async def _send_stage_retry_exhausted(
        self,
        coord_id: str,
        stage_name: str,
        retries_so_far: int,
        max_retries: int,
    ) -> None:
        """Tell the coordinator it has burned the retry budget on a stage.

        Fired exactly once per (stage, exhaustion event): after the coord
        returned a ``RETRY`` verdict that would push ``stage_retries[stage]``
        past ``max_retries``.  The orchestrator does NOT re-run any agents;
        it waits for a fresh verdict that must be ``APPROVE`` (treated as a
        user-override path when summary is set, otherwise an explicit coord
        decision to accept the current outputs) or a ``mark_done`` with the
        ``ABORT:`` prefix (Track A Task 4).
        """
        self.events.push(Event(
            kind="stage_completed",
            detail={
                "stage_name": stage_name,
                "retries_so_far": retries_so_far,
                "max_retries": max_retries,
                "exhausted": True,
            },
        ))
        msg = (
            f"[STAGE_RETRY_EXHAUSTED] stage={stage_name} "
            f"retries={retries_so_far}/{max_retries}\n"
            "You have spent the retry budget on this stage without reaching a "
            "passing state.  Another request_rework will be refused.  Pick one:\n"
            "  - approve_stage(stage_name, summary) — accept what you have and "
            "document the gap in the summary.\n"
            "  - mark_done with reason starting \"ABORT: ...\" — fail the pipeline "
            "when the gap is unrecoverable.\n"
            "  - notify_user(level=\"blocker\", action_required=true) — ask the "
            "human to override and record the decision."
        )
        await self.send_message(coord_id, msg)

    async def _send_stage_complete(
        self,
        coord_id: str,
        stage_name: str,
        agent_ids: list[str],
        *,
        acceptance_criteria: str | None = None,
        retries_so_far: int = 0,
        max_retries: int = 3,
    ) -> None:
        """Send the ``[STAGE_COMPLETE]`` inbox message and log the event.

        Called by the orchestrator after every worker in ``stage_name`` has
        reached a terminal status.  The coordinator is expected to read each
        listed ``context_paths`` file and respond with exactly one of
        ``approve_stage`` or ``request_rework``.
        """
        context_paths = {aid: self._context_file_path(aid) for aid in agent_ids}
        self.events.push(Event(
            kind="stage_completed",
            detail={
                "stage_name": stage_name,
                "agent_ids": list(agent_ids),
                "context_paths": context_paths,
                "acceptance_criteria": acceptance_criteria,
                "retries_so_far": retries_so_far,
                "max_retries": max_retries,
            },
        ))
        ac_line = (
            acceptance_criteria.strip()
            if acceptance_criteria and acceptance_criteria.strip()
            else "(none set — use your judgement)"
        )
        rows = "\n".join(
            f"  - {aid} — context: {context_paths[aid]}" for aid in agent_ids
        )
        msg = (
            f"[STAGE_COMPLETE] stage={stage_name} "
            f"retries={retries_so_far}/{max_retries}\n"
            f"Agents:\n{rows}\n"
            f"Acceptance criteria: {ac_line}\n\n"
            "Read each agent's context file with your Read tool, compare the "
            "output against the acceptance criteria, then call EXACTLY one:\n"
            f"  - approve_stage(stage_name=\"{stage_name}\", summary=\"...\")\n"
            f"  - request_rework(stage_name=\"{stage_name}\", agents=[...], "
            "instruction=\"...\")\n"
            "Do not emit any other dispatch tools during a stage review."
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

    async def send_user_message(self, agent_id: str, content: str) -> None:
        """Deliver a message from the user.  Wraps it as ``[USER_MESSAGE] <text>``
        when the target is the coordinator so its system-prompt routing logic
        handles it as a dialog event (not as a new agent-run instruction).
        Non-coord agents receive the raw text — workers don't have inbox
        conventions.
        """
        agent = self.get_agent(agent_id)
        if agent.role_id == self._coord_role_id():
            content = f"[USER_MESSAGE] {content}"
        await self.send_message(agent_id, content)

    # ------------------------------------------------------------------ #
    #  Workflow triggers                                                   #
    # ------------------------------------------------------------------ #

    async def _on_agent_completed(self, agent_id: str, summary: str) -> None:
        """Record the event and dispatch any matching workflow triggers.

        The coordinator is notified separately via ``_notify_coordinator``
        so legacy deployments that don't use triggers still get the
        existing routing behaviour. When triggers *are* configured, the
        matcher decides whether to start workers directly or hand off to
        the coordinator via a ``decide: coordinator`` rule.
        """
        from .workflow import load_workflow, match_triggers

        event = Event(
            kind="agent_completed",
            agent=agent_id,
            detail={"summary": summary},
        )
        self.events.push(event)

        wf = load_workflow(self.workspace_dir)
        if wf is None or not wf.triggers:
            return
        actions = match_triggers(wf, event, self.events.completed_agents())
        if not actions:
            return
        # First-match-wins keeps the common case (one clear rule per
        # event) predictable. Secondary matches are logged for debugging.
        primary = actions[0]
        if len(actions) > 1:
            logger.debug(
                "[%s] multiple triggers matched %s.completed — using #%d",
                self.id, agent_id, primary.trigger_index,
            )
        if primary.decide:
            coord_role_id = self._coord_role_id()
            if primary.decide == coord_role_id:
                coord_id = self._find_coordinator(coord_role_id)
                if coord_id:
                    msg = (
                        f"[TRIGGER] {agent_id}.completed routed to you. "
                        f"Decide the next step.\nSummary: {summary[:200]}"
                    )
                    await self.send_message(coord_id, msg)
            return
        for target in primary.start_agents:
            try:
                if target in self.agents:
                    self.start_agent(
                        target, summary, context_from=primary.context_from or None,
                    )
                else:
                    # Target names a role id — spawn a fresh agent.
                    state = self.create_agent(target)
                    self.start_agent(
                        state.id, summary,
                        context_from=primary.context_from or None,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[%s] trigger dispatch to %s failed: %s",
                    self.id, target, exc,
                )

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
        is_coordinator = role.id == self._coord_role_id()

        try:
            while True:
                agent.status = AgentStatus.RUNNING
                agent.started_at = datetime.now()
                agent.finished_at = None   # clear stale mark from prior turn
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
                mcp_servers: dict[str, Any] | None = None
                if is_coordinator:
                    coord_server = (
                        build_coordinator_mcp_server(self)
                        if role.provider == "claude"
                        else None
                    )
                    if coord_server is not None:
                        mcp_servers = {"coord": coord_server}
                    header = self._coordinator_runtime_header(
                        mcp_available=mcp_servers is not None,
                    )
                    full_prompt = f"{header}\n\n---\n\n{full_prompt}"

                result_text = await self._run_sdk_with_retry(
                    agent_id=agent_id,
                    role=role,
                    adapter=adapter,
                    full_prompt=full_prompt,
                    perm_cb=_perm_cb,
                    mcp_servers=mcp_servers,
                )

                if result_text:
                    self.ctx.set_result(agent_id, role.name, current_prompt[:200], result_text)
                    await self._emit(agent_id, "context_update", {"content": self.ctx.read(agent_id)})

                # Success — route through _finalize_agent so we get the
                # same ``agent_status`` broadcast + persistence that every
                # other exit path produces.
                await self._finalize_agent(agent_id, "completed")

                # Coordinator-specific post-turn handling. When MCP tools
                # are available (Claude), the coordinator has already
                # dispatched via tool calls — running the directive parser
                # would only risk double-firing on narration that happens
                # to start with ``>>>``. Keep the parser for fallback
                # providers that have no MCP path.
                # Non-coordinators notify the coordinator about their
                # completion so it can route next steps.
                if is_coordinator:
                    if mcp_servers is None:
                        await self._process_coordinator_directives(agent_id, result_text)
                else:
                    summary = (result_text or current_prompt)[:200]
                    await self._notify_coordinator(agent_id, summary)
                    await self._on_agent_completed(agent_id, summary)

                queue = self._message_queues.get(agent_id)
                if queue is None or queue.empty():
                    break
                current_prompt = queue.get_nowait()
                current_context_from = None

        except asyncio.CancelledError:
            logger.info("[%s] agent %s cancelled", self.id, agent_id)
            # stop_agent has already scheduled _finalize_agent(idle) and
            # tree-killed subprocesses; but if we got here for any other
            # reason (parent pipeline cancelled us, for instance) we still
            # need the card to reflect a terminal state. Mark idle only if
            # no one else has already finalized.
            if agent.finished_at is None:
                await self._finalize_agent(
                    agent_id, "idle", reason="Cancelled",
                )
            raise
        except Exception as exc:
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
            await self._emit(agent_id, "agent_error", info.model_dump(mode="json"))
            self.events.push(Event(
                kind="agent_error",
                agent=agent_id,
                detail={
                    "category": category,
                    "message": info.message,
                    "error_id": info.id,
                },
            ))
            logger.exception("[%s] agent %s failed (%s)", self.id, agent_id, category)
            await self._finalize_agent(
                agent_id, "error",
                reason=f"[{category}] {info.message}",
            )
        finally:
            self._tasks.pop(agent_id, None)
            self._cleanup_pending_permissions(agent_id, reason="agent_ended")
            # Drop the PID set — by the time we're here claude.exe has
            # exited on its own or been killed by stop_agent.
            self._sdk_pids.pop(agent_id, None)
            # Post-condition: if the SDK session exits without any caller
            # having set finished_at, that's a bug in the exit-path audit.
            # Finalize as error so the agent card doesn't stay idle/running.
            if agent.finished_at is None:
                logger.warning(
                    "[%s] agent %s SDK session ended without finalize — forcing error",
                    self.id, agent_id,
                )
                await self._finalize_agent(
                    agent_id, "error",
                    reason="Agent session ended without explicit finalize",
                )

    async def _run_sdk_with_retry(
        self,
        *,
        agent_id: str,
        role: AgentRole,
        adapter: ProviderAdapter,
        full_prompt: str,
        perm_cb: Any,
        mcp_servers: dict[str, Any] | None = None,
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

            def _pid_cb(pid: int, job_handle: int | None = None) -> None:
                self._register_sdk_pid(agent_id, pid, job_handle)

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
                    mcp_servers=mcp_servers,
                    pid_callback=_pid_cb,
                ):
                    await self._handle_provider_message(agent_id, role.model, msg)

                    if msg.type == "result":
                        if msg.content:
                            result_text = msg.content
                        if msg.session_id:
                            agent.session_id = msg.session_id
                        prev_cost = agent.usage.cost_usd
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
                        delta = max(agent.usage.cost_usd - prev_cost, 0.0)
                        tripped = self.budget.record_turn(delta, turn_delta=1)
                        if tripped:
                            await self.broadcast_raw({
                                "type": "budget_exceeded",
                                "data": {
                                    "reason": self.budget.exceeded_reason,
                                    "detail": self.budget.exceeded_detail,
                                    "snapshot": self.budget.snapshot(),
                                },
                            })
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
