"""Claude provider adapter — wraps claude-agent-sdk."""

from __future__ import annotations

import asyncio
import ctypes
import logging
import queue
import sys
from collections.abc import AsyncIterable
from dataclasses import replace
from threading import Thread
from typing import Any, AsyncIterator, Awaitable, Callable

from . import register
from ._permissions import tool_needs_approval
from .base import ProviderAdapter, ProviderMessage

logger = logging.getLogger(__name__)

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        PermissionResultAllow,
        PermissionResultDeny,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        query,
    )

    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False
    logger.warning("claude-agent-sdk not installed — Claude provider unavailable")

# ---------------------------------------------------------------------------
# Windows Job Object helpers (Task 8b).
#
# ``taskkill /T /F`` walks the parent-PID chain. The SDK's Bash tool spawns
# background subprocesses with DETACHED_PROCESS/CREATE_NEW_PROCESS_GROUP,
# which re-parents them onto services.exe and breaks that chain — taskkill
# can no longer find them, so ``sleep.exe`` / ``node.exe`` etc. leak as
# orphans even after we kill the main claude.exe.
#
# The correct fix on Windows is a Job Object with KILL_ON_JOB_CLOSE. Every
# descendant of the assigned process is enrolled in the same job regardless
# of detach flags (unless they explicitly request CREATE_BREAKAWAY_FROM_JOB
# AND the job allows breakaway — we allow neither). ``TerminateJobObject``
# then kills the entire family in one kernel call.
# ---------------------------------------------------------------------------
_JOB_OBJECT_AVAILABLE = False
if sys.platform == "win32":
    try:
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9
        PROCESS_TERMINATE = 0x0001
        PROCESS_SET_QUOTA = 0x0100

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_ulong),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_ulong),
                ("Affinity", ctypes.c_void_p),
                ("PriorityClass", ctypes.c_ulong),
                ("SchedulingClass", ctypes.c_ulong),
            ]

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        _kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        _kernel32.OpenProcess.restype = ctypes.c_void_p
        _kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
        ]
        _kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

        _JOB_OBJECT_AVAILABLE = True
    except Exception:  # noqa: BLE001
        logger.info("Win32 Job Object bindings unavailable; detached tree-kill disabled")


def _create_kill_on_close_job() -> int | None:
    """Create a Job Object set to kill all member processes when handle closes.

    Returns a HANDLE (as Python int), or None on failure. Caller owns the
    handle and is responsible for eventually calling CloseHandle.
    """
    if not _JOB_OBJECT_AVAILABLE:
        return None
    try:
        h_job = _kernel32.CreateJobObjectW(None, None)
        if not h_job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            h_job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            err = ctypes.get_last_error()
            _kernel32.CloseHandle(h_job)
            raise ctypes.WinError(err)
        return int(h_job)
    except Exception:
        logger.exception("Failed to create Job Object")
        return None


def _assign_pid_to_job(h_job: int, pid: int) -> bool:
    """Open process by PID and assign to the given job. Returns True on success."""
    if not _JOB_OBJECT_AVAILABLE:
        return False
    h_proc = _kernel32.OpenProcess(
        PROCESS_TERMINATE | PROCESS_SET_QUOTA, False, pid
    )
    if not h_proc:
        logger.warning("OpenProcess(%d) failed: %s", pid, ctypes.get_last_error())
        return False
    try:
        ok = _kernel32.AssignProcessToJobObject(h_job, h_proc)
        if not ok:
            logger.warning(
                "AssignProcessToJobObject(%d) failed: %s",
                pid,
                ctypes.get_last_error(),
            )
            return False
        return True
    finally:
        _kernel32.CloseHandle(h_proc)


def _terminate_job(h_job: int) -> bool:
    """Kill all processes currently in the job. Handle remains open."""
    if not _JOB_OBJECT_AVAILABLE or not h_job:
        return False
    try:
        return bool(_kernel32.TerminateJobObject(h_job, 1))
    except Exception:
        logger.exception("TerminateJobObject failed")
        return False


def _close_job(h_job: int) -> None:
    """Close the job handle. Pair with _create_kill_on_close_job."""
    if not _JOB_OBJECT_AVAILABLE or not h_job:
        return
    try:
        _kernel32.CloseHandle(h_job)
    except Exception:
        logger.exception("CloseHandle(job) failed")


# Optional: a subclass of the SDK's subprocess transport that reports the
# Claude CLI PID back to the orchestrator so it can kill the subprocess tree
# on stop. Without this, asyncio cancellation cannot reach bash/node workers
# spawned under claude.exe. If the SDK's private transport module moves, we
# silently fall back to the default transport.
_PID_TRANSPORT_AVAILABLE = False
try:
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    class _PidCapturingTransport(SubprocessCLITransport):  # type: ignore[misc]
        """SubprocessCLITransport that reports the CLI PID and owns a Job Object.

        On Windows we wrap claude.exe in a Job Object with KILL_ON_JOB_CLOSE
        so the orchestrator can wipe out every descendant — including those
        the CLI detaches via DETACHED_PROCESS — by calling TerminateJobObject.
        """

        def __init__(
            self,
            *args: Any,
            pid_callback: Callable[[int, int | None], None] | None = None,
            **kwargs: Any,
        ) -> None:
            super().__init__(*args, **kwargs)
            self._pid_callback = pid_callback
            self._job_handle: int | None = None

        async def connect(self) -> None:  # type: ignore[override]
            await super().connect()
            proc = getattr(self, "_process", None)
            pid = getattr(proc, "pid", None) if proc is not None else None
            if not isinstance(pid, int):
                return

            # Best-effort: if Job Object creation or assignment fails (e.g. the
            # process already exited, or we're not on Windows), we fall back to
            # the taskkill /T /F path in project.py. The PID callback still
            # fires so that path still has something to target.
            h_job: int | None = _create_kill_on_close_job()
            if h_job is not None:
                if not _assign_pid_to_job(h_job, pid):
                    _close_job(h_job)
                    h_job = None
            self._job_handle = h_job

            cb = self._pid_callback
            if cb is not None:
                try:
                    cb(pid, h_job)
                except Exception:
                    logger.exception("pid_callback raised")

        async def close(self) -> None:  # type: ignore[override]
            try:
                await super().close()
            finally:
                if self._job_handle is not None:
                    _close_job(self._job_handle)
                    self._job_handle = None

    _PID_TRANSPORT_AVAILABLE = True
except Exception:  # noqa: BLE001
    logger.info(
        "PID-capturing transport unavailable; tree-kill on stop will use "
        "child-process enumeration fallback"
    )


# Windows + Python 3.14: the default SelectorEventLoop does not support
# subprocesses, but the SDK spawns the Claude CLI as a child process.
# We detect this once at import time so we can route SDK calls through a
# dedicated thread running a ProactorEventLoop.
_NEEDS_PROACTOR = sys.platform == "win32"

# Default and hard-cap timeouts for the CLI's Bash tool, in milliseconds.
# One developer agent in v0.2.0 testing invoked `pnpm test --reporter=dot`
# (vitest watch mode) without `--run`; the Bash call never returned and hung
# the asyncio task tree for 10+ minutes. We now inject an explicit `timeout`
# on every Bash tool call so the CLI enforces it natively and returns a
# tool_error instead of blocking forever.
BASH_DEFAULT_TIMEOUT_MS = 300_000   # 5 min
BASH_MAX_TIMEOUT_MS = 900_000       # 15 min hard cap


def _normalize_bash_input(tool_input: dict) -> dict:
    """Return a copy of *tool_input* with a sensible ``timeout`` field.

    The Claude Code CLI's Bash tool accepts an optional ``timeout`` in ms.
    We enforce our own policy on top: if unset, default to 5 min; if set,
    clamp to 15 min max. Non-dict inputs pass through unchanged so this
    helper is safe to call defensively.
    """
    if not isinstance(tool_input, dict):
        return tool_input
    timeout = tool_input.get("timeout")
    try:
        timeout_int = int(timeout) if timeout is not None else None
    except (TypeError, ValueError):
        timeout_int = None
    if timeout_int is None or timeout_int <= 0:
        timeout_int = BASH_DEFAULT_TIMEOUT_MS
    if timeout_int > BASH_MAX_TIMEOUT_MS:
        timeout_int = BASH_MAX_TIMEOUT_MS
    if timeout_int == tool_input.get("timeout"):
        return tool_input
    new_input = dict(tool_input)
    new_input["timeout"] = timeout_int
    return new_input


def _allow_with_timeout(tool_name: str, tool_input: dict) -> "PermissionResultAllow":
    """Build a PermissionResultAllow, injecting Bash timeout when applicable.

    Returning ``updated_input`` on the allow result tells the SDK to rewrite
    the tool call before dispatching it to the CLI. This is how we enforce
    our default/cap timeout without the agent having to cooperate.
    """
    if tool_name == "Bash" and isinstance(tool_input, dict):
        normalized = _normalize_bash_input(tool_input)
        if normalized is not tool_input:
            return PermissionResultAllow(updated_input=normalized)
    return PermissionResultAllow()


def _stderr_callback(line: str) -> None:
    """Log CLI stderr output — surfaces permission/connection failures."""
    stripped = line.strip()
    if stripped:
        logger.info("[claude-cli] %s", stripped)


# ------------------------------------------------------------------ #
#  Async-iterable prompt wrapper                                      #
# ------------------------------------------------------------------ #

def _make_prompt_iter(
    s: str, done_event: asyncio.Event
) -> AsyncIterable[dict]:
    """Wrap a plain string prompt into an async iterable of SDK message dicts.

    The SDK requires an ``AsyncIterable[dict]`` prompt when ``can_use_tool``
    is set.  We yield the user message envelope, then BLOCK on *done_event*
    until the adapter signals the conversation has ended.

    Why block? ``query.stream_input`` calls ``transport.end_input()`` (closes
    stdin) as soon as this iterator is exhausted — but the CLI needs stdin
    open to receive ``control_response`` replies for its ``can_use_tool``
    requests.  Closing stdin too early causes the CLI to fail every
    permission request with ``Error: Stream closed``.  The SDK only keeps
    stdin open automatically when ``sdk_mcp_servers`` or ``hooks`` are set;
    ``can_use_tool`` is absent from that list (SDK v0.1.59 bug).
    """

    async def _gen() -> AsyncIterable[dict]:
        yield {
            "type": "user",
            "session_id": "",
            "message": {"role": "user", "content": s},
            "parent_tool_use_id": None,
        }
        try:
            await done_event.wait()
        except asyncio.CancelledError:
            pass

    return _gen()


def _iter_sdk_in_thread(
    prompt: str,
    options: ClaudeAgentOptions,
    main_loop: asyncio.AbstractEventLoop | None = None,
    perm_cb: Callable[[str, dict], Awaitable[bool]] | None = None,
    pid_callback: Callable[[int, int | None], None] | None = None,
) -> queue.Queue:
    """Run the SDK query on a ProactorEventLoop in a background thread.

    Returns a thread-safe queue that receives SDK messages followed by a
    ``None`` sentinel (success) or an ``Exception`` (failure).

    When *perm_cb* is provided the SDK's ``can_use_tool`` hook is wired up,
    bridging the async callback from the worker thread back to *main_loop*
    via ``run_coroutine_threadsafe`` + ``wrap_future``.

    When *pid_callback* is provided (and the PID-capturing transport is
    available) the spawned ``claude.exe`` PID is reported back so the
    orchestrator can tree-kill it on stop / delete.
    """
    msg_queue: queue.Queue = queue.Queue()

    def _target() -> None:
        loop = asyncio.ProactorEventLoop()
        asyncio.set_event_loop(loop)
        try:
            async def _collect() -> None:
                done_event = asyncio.Event()

                # If we have a permission callback, set can_use_tool on options
                if perm_cb and main_loop:
                    async def _can_use_tool(tool_name, tool_input, _ctx):
                        logger.info(
                            "can_use_tool invoked (thread): tool=%s", tool_name
                        )
                        try:
                            # Auto-approve read-only tools without bothering
                            # the user (Read/Glob/Grep/readonly Bash). Bash
                            # calls still get the default timeout injected.
                            if not tool_needs_approval(tool_name, tool_input):
                                return _allow_with_timeout(tool_name, tool_input)
                            # Bridge to main uvicorn loop where the Future lives
                            concurrent_future = asyncio.run_coroutine_threadsafe(
                                perm_cb(tool_name, tool_input), main_loop
                            )
                            # wrap into current (Proactor) loop's future
                            allowed = await asyncio.wrap_future(concurrent_future)
                            logger.info("Permission result: %s -> %s", tool_name, allowed)
                            if allowed:
                                return _allow_with_timeout(tool_name, tool_input)
                            return PermissionResultDeny(message="User denied")
                        except Exception as exc:
                            logger.error("Permission callback error: %s", exc, exc_info=True)
                            return PermissionResultDeny(message=f"Permission error: {exc}")

                    options.can_use_tool = _can_use_tool

                # Always use the keep-stdin-open iterator, even without a
                # permission callback — harmless, and future-proof if we add
                # hooks later.
                sdk_prompt = _make_prompt_iter(prompt, done_event)
                transport = _build_pid_capturing_transport(
                    sdk_prompt, options, pid_callback
                )
                try:
                    async for message in query(
                        prompt=sdk_prompt, options=options, transport=transport
                    ):
                        msg_queue.put(message)
                        # Once a ResultMessage arrives the conversation is
                        # over — release the iterator so stdin can close.
                        if isinstance(message, ResultMessage):
                            done_event.set()
                finally:
                    done_event.set()

            loop.run_until_complete(_collect())
            msg_queue.put(None)
        except Exception as exc:
            msg_queue.put(exc)
        finally:
            loop.close()

    thread = Thread(target=_target, daemon=True)
    thread.start()
    return msg_queue


def _build_pid_capturing_transport(
    sdk_prompt: AsyncIterable[dict],
    options: "ClaudeAgentOptions",
    pid_callback: Callable[[int, int | None], None] | None,
):
    """Return a transport that reports the CLI PID, or None for SDK default.

    Only useful when ``_PID_TRANSPORT_AVAILABLE`` (the SDK's private transport
    module is importable) and a pid_callback was supplied. In all other cases
    we return ``None`` so ``query()`` constructs its own default transport.
    """
    if not _PID_TRANSPORT_AVAILABLE or pid_callback is None:
        return None
    # InternalClient.process_query patches options with
    # permission_prompt_tool_name="stdio" whenever can_use_tool is set, then
    # uses the patched options to build its default transport. When we hand
    # in our own transport, that patch is skipped — so the CLI never gets
    # --permission-prompt-tool stdio, never routes approvals through the
    # stdio control protocol, and our can_use_tool hook never fires.
    transport_options = options
    if options.can_use_tool and not options.permission_prompt_tool_name:
        transport_options = replace(options, permission_prompt_tool_name="stdio")
    try:
        return _PidCapturingTransport(
            prompt=sdk_prompt,
            options=transport_options,
            pid_callback=pid_callback,
        )
    except Exception:
        logger.exception("failed to build PID-capturing transport; using SDK default")
        return None


class ClaudeAdapter(ProviderAdapter):
    async def run(
        self,
        *,
        prompt: str,
        system_prompt: str,
        model: str,
        tools: list[str],
        cwd: str,
        max_turns: int,
        session_id: str | None = None,
        effort: str | None = None,
        permission_callback: Callable[[str, dict], Awaitable[bool]] | None = None,
        mcp_servers: dict[str, Any] | None = None,
        pid_callback: Callable[[int, int | None], None] | None = None,
    ) -> AsyncIterator[ProviderMessage]:
        if not _SDK_AVAILABLE:
            yield ProviderMessage(
                type="error",
                content="claude-agent-sdk is not installed. "
                "Install with: pip install claude-agent-sdk",
            )
            return

        # When permission_callback is set, route EVERY tool call through
        # can_use_tool so the callback decides (including auto-approve for
        # read-only tools).  Do NOT pass --allowedTools as a whitelist —
        # the CLI's allowedTools rules can short-circuit before the stdio
        # permission protocol fires, producing the "silent deny" behavior.
        # Do NOT pass --tools either (conflicts with the control protocol).
        # Do NOT pass --permission-mode — the SDK auto-sets the right mode
        # when can_use_tool is provided.
        opts: dict = dict(
            model=model,
            system_prompt=system_prompt,
            cwd=cwd,
            max_turns=max_turns,
            resume=session_id,
            stderr=_stderr_callback,  # surface CLI errors in our logs
        )
        if not permission_callback:
            opts["allowed_tools"] = tools
        if effort:
            opts["effort"] = effort
        if mcp_servers:
            opts["mcp_servers"] = mcp_servers
        options = ClaudeAgentOptions(**opts)

        result_text = ""
        final_session_id = session_id
        final_usage: dict = {}
        final_cost: float | None = None

        async for message in self._query(prompt, options, permission_callback, pid_callback):
            # --- AssistantMessage ---
            if isinstance(message, AssistantMessage):
                if message.usage:
                    yield ProviderMessage(
                        type="usage",
                        usage={
                            "input_tokens": message.usage.get("input_tokens", 0),
                            "output_tokens": message.usage.get("output_tokens", 0),
                            "cache_read_input_tokens": message.usage.get("cache_read_input_tokens", 0),
                            "cache_creation_input_tokens": message.usage.get("cache_creation_input_tokens", 0),
                        },
                    )
                for block in message.content:
                    if isinstance(block, TextBlock):
                        yield ProviderMessage(type="text", content=block.text)
                    elif isinstance(block, ToolUseBlock):
                        yield ProviderMessage(
                            type="tool_use",
                            content=f"[Tool: {block.name}] {str(block.input)[:300]}",
                            tool_name=block.name,
                            tool_input=block.input,
                        )
                    elif isinstance(block, ToolResultBlock):
                        yield ProviderMessage(
                            type="tool_result",
                            content=str(block.content)[:500] if block.content else "",
                        )

            # --- ResultMessage ---
            elif isinstance(message, ResultMessage):
                final_session_id = message.session_id
                if message.result:
                    result_text = message.result
                if message.total_cost_usd is not None:
                    final_cost = message.total_cost_usd
                if message.usage:
                    final_usage = message.usage

            # --- SystemMessage ---
            elif isinstance(message, SystemMessage):
                logger.debug("Claude system message: subtype=%s", message.subtype)

        # Emit final result
        yield ProviderMessage(
            type="result",
            content=result_text,
            session_id=final_session_id,
            usage=final_usage,
            cost_usd=final_cost,
        )

    # -------------------------------------------------------------- #

    @staticmethod
    async def _query(
        prompt: str,
        options: ClaudeAgentOptions,
        perm_cb: Callable[[str, dict], Awaitable[bool]] | None = None,
        pid_callback: Callable[[int, int | None], None] | None = None,
    ) -> AsyncIterator:
        """Yield SDK messages, using a thread only when the main loop lacks subprocess support."""

        # On Windows the SDK spawns a CLI subprocess which needs a
        # ProactorEventLoop.  Python 3.8+ already defaults to Proactor on
        # Windows, so the main uvicorn loop usually qualifies.  Only fall
        # back to the background-thread approach when it does not.
        _ProactorEventLoop = getattr(asyncio, "ProactorEventLoop", type(None))
        needs_thread = _NEEDS_PROACTOR and not isinstance(
            asyncio.get_running_loop(), _ProactorEventLoop
        )

        if not needs_thread:
            # Direct path: SDK runs on the main event loop.
            # Permission callbacks run in the same loop — no cross-thread
            # bridge needed.
            done_event = asyncio.Event()
            if perm_cb:
                async def _can_use_tool(tool_name, tool_input, _ctx):
                    logger.info(
                        "can_use_tool invoked: tool=%s input_keys=%s",
                        tool_name,
                        list(tool_input.keys()) if isinstance(tool_input, dict) else None,
                    )
                    try:
                        # Auto-approve read-only tools without bothering
                        # the user (Read/Glob/Grep/readonly Bash). Bash
                        # calls still get the default timeout injected.
                        if not tool_needs_approval(tool_name, tool_input):
                            return _allow_with_timeout(tool_name, tool_input)
                        allowed = await perm_cb(tool_name, tool_input)
                        logger.info("Permission result: %s -> %s", tool_name, allowed)
                        if allowed:
                            return _allow_with_timeout(tool_name, tool_input)
                        return PermissionResultDeny(message="User denied")
                    except Exception as exc:
                        logger.error("Permission callback error: %s", exc, exc_info=True)
                        return PermissionResultDeny(message=f"Permission error: {exc}")
                options.can_use_tool = _can_use_tool

            # Always use the keep-stdin-open iterator.  See _make_prompt_iter
            # for why we can't just pass the prompt as a string.
            sdk_prompt = _make_prompt_iter(prompt, done_event)
            transport = _build_pid_capturing_transport(
                sdk_prompt, options, pid_callback
            )
            try:
                async for msg in query(
                    prompt=sdk_prompt, options=options, transport=transport
                ):
                    yield msg
                    # Once a ResultMessage arrives the conversation is
                    # over — release the iterator so stdin can close.
                    if isinstance(msg, ResultMessage):
                        done_event.set()
            finally:
                done_event.set()
            return

        # Fallback: main loop is not Proactor (rare on modern Windows).
        # Run the SDK in a dedicated thread with its own ProactorEventLoop
        # and drain messages through a thread-safe queue.
        logger.info("Using thread-based SDK query (main loop is not ProactorEventLoop)")
        main_loop = asyncio.get_running_loop()
        msg_queue = _iter_sdk_in_thread(
            prompt, options, main_loop, perm_cb, pid_callback
        )
        while True:
            item = await main_loop.run_in_executor(None, msg_queue.get)
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield item


register("claude", ClaudeAdapter)
