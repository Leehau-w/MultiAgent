"""Claude provider adapter — wraps claude-agent-sdk."""

from __future__ import annotations

import logging
from typing import AsyncIterator

from . import register
from .base import ProviderAdapter, ProviderMessage

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

    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False
    logger.warning("claude-agent-sdk not installed — Claude provider unavailable")


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
    ) -> AsyncIterator[ProviderMessage]:
        if not _SDK_AVAILABLE:
            yield ProviderMessage(
                type="error",
                content="claude-agent-sdk is not installed. "
                "Install with: pip install claude-agent-sdk",
            )
            return

        options = ClaudeAgentOptions(
            allowed_tools=tools,
            model=model,
            system_prompt=system_prompt,
            cwd=cwd,
            max_turns=max_turns,
            resume=session_id,
        )

        result_text = ""
        final_session_id = session_id
        final_usage: dict = {}
        final_cost: float | None = None

        async for message in query(prompt=prompt, options=options):
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


register("claude", ClaudeAdapter)
