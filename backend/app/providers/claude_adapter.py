"""Claude provider adapter — wraps claude-agent-sdk."""

from __future__ import annotations

import asyncio
import logging
import queue
import sys
from collections.abc import AsyncIterable
from threading import Thread
from typing import AsyncIterator, Awaitable, Callable

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

# Windows + Python 3.14: the default SelectorEventLoop does not support
# subprocesses, but the SDK spawns the Claude CLI as a child process.
# We detect this once at import time so we can route SDK calls through a
# dedicated thread running a ProactorEventLoop.
_NEEDS_PROACTOR = sys.platform == "win32"


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
) -> queue.Queue:
    """Run the SDK query on a ProactorEventLoop in a background thread.

    Returns a thread-safe queue that receives SDK messages followed by a
    ``None`` sentinel (success) or an ``Exception`` (failure).

    When *perm_cb* is provided the SDK's ``can_use_tool`` hook is wired up,
    bridging the async callback from the worker thread back to *main_loop*
    via ``run_coroutine_threadsafe`` + ``wrap_future``.
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
                            # the user (Read/Glob/Grep/readonly Bash).
                            if not tool_needs_approval(tool_name, tool_input):
                                return PermissionResultAllow()
                            # Bridge to main uvicorn loop where the Future lives
                            concurrent_future = asyncio.run_coroutine_threadsafe(
                                perm_cb(tool_name, tool_input), main_loop
                            )
                            # wrap into current (Proactor) loop's future
                            allowed = await asyncio.wrap_future(concurrent_future)
                            logger.info("Permission result: %s -> %s", tool_name, allowed)
                            if allowed:
                                return PermissionResultAllow()
                            return PermissionResultDeny(message="User denied")
                        except Exception as exc:
                            logger.error("Permission callback error: %s", exc, exc_info=True)
                            return PermissionResultDeny(message=f"Permission error: {exc}")

                    options.can_use_tool = _can_use_tool

                # Always use the keep-stdin-open iterator, even without a
                # permission callback — harmless, and future-proof if we add
                # hooks later.
                sdk_prompt = _make_prompt_iter(prompt, done_event)
                try:
                    async for message in query(prompt=sdk_prompt, options=options):
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
        options = ClaudeAgentOptions(**opts)

        result_text = ""
        final_session_id = session_id
        final_usage: dict = {}
        final_cost: float | None = None

        async for message in self._query(prompt, options, permission_callback):
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
                        # the user (Read/Glob/Grep/readonly Bash).
                        if not tool_needs_approval(tool_name, tool_input):
                            return PermissionResultAllow()
                        allowed = await perm_cb(tool_name, tool_input)
                        logger.info("Permission result: %s -> %s", tool_name, allowed)
                        if allowed:
                            return PermissionResultAllow()
                        return PermissionResultDeny(message="User denied")
                    except Exception as exc:
                        logger.error("Permission callback error: %s", exc, exc_info=True)
                        return PermissionResultDeny(message=f"Permission error: {exc}")
                options.can_use_tool = _can_use_tool

            # Always use the keep-stdin-open iterator.  See _make_prompt_iter
            # for why we can't just pass the prompt as a string.
            sdk_prompt = _make_prompt_iter(prompt, done_event)
            try:
                async for msg in query(prompt=sdk_prompt, options=options):
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
        msg_queue = _iter_sdk_in_thread(prompt, options, main_loop, perm_cb)
        while True:
            item = await main_loop.run_in_executor(None, msg_queue.get)
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield item


register("claude", ClaudeAdapter)
