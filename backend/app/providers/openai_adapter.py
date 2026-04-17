"""OpenAI provider adapter — chat completions with tool calling loop."""

from __future__ import annotations

import json
import logging
import os
from typing import AsyncIterator, Awaitable, Callable

from . import register
from ._permissions import tool_needs_approval
from .base import ProviderAdapter, ProviderMessage
from .tools import execute_tool, get_tool_schemas

logger = logging.getLogger(__name__)

try:
    from openai import AsyncOpenAI

    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False
    logger.warning("openai package not installed — OpenAI provider unavailable")

# Pricing per million tokens (approximate)
_PRICING: dict[str, dict[str, float]] = {
    "gpt-4o": {"input": 2.5, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.6},
    "gpt-4.1": {"input": 2.0, "output": 8.0},
    "gpt-4.1-mini": {"input": 0.4, "output": 1.6},
    "gpt-4.1-nano": {"input": 0.1, "output": 0.4},
    "o3": {"input": 2.0, "output": 8.0},
    "o3-mini": {"input": 1.1, "output": 4.4},
    "o4-mini": {"input": 1.1, "output": 4.4},
}


def _estimate_cost(model: str, usage: dict) -> float:
    p = _PRICING.get(model, _PRICING.get("gpt-4o-mini", {"input": 0.15, "output": 0.6}))
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    return (inp * p["input"] + out * p["output"]) / 1_000_000


class OpenAIAdapter(ProviderAdapter):
    """Adapter for OpenAI-compatible APIs (also used as base for Ollama)."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str = "gpt-4o-mini",
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.default_model = default_model

    def _make_client(self) -> "AsyncOpenAI":
        kwargs: dict = {}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        if self.api_key:
            kwargs["api_key"] = self.api_key
        elif not self.base_url:
            # Standard OpenAI — key from env
            key = os.environ.get("OPENAI_API_KEY", "")
            if not key:
                raise RuntimeError(
                    "OPENAI_API_KEY not set. Export it or pass it as an environment variable."
                )
            kwargs["api_key"] = key
        return AsyncOpenAI(**kwargs)

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
        if not _OPENAI_AVAILABLE:
            yield ProviderMessage(
                type="error",
                content="openai package is not installed. "
                "Install with: pip install openai",
            )
            return

        client = self._make_client()
        effective_model = model or self.default_model
        tool_schemas = get_tool_schemas(tools)

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        total_usage = {"input_tokens": 0, "output_tokens": 0}
        collected_text: list[str] = []
        stopped_early = False

        for turn in range(max_turns):
            # Build request kwargs
            kwargs: dict = {
                "model": effective_model,
                "messages": messages,
            }
            if tool_schemas:
                kwargs["tools"] = tool_schemas

            response = await client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            assistant_msg = choice.message

            # Track usage — orchestrator REPLACEs input_tokens (current window
            # size) and ACCUMULATEs output_tokens (per-turn delta), so we
            # must yield the per-turn values, not our running total.
            if response.usage:
                total_usage["input_tokens"] = response.usage.prompt_tokens
                total_usage["output_tokens"] += response.usage.completion_tokens
                yield ProviderMessage(
                    type="usage",
                    usage={
                        "input_tokens": response.usage.prompt_tokens,
                        "output_tokens": response.usage.completion_tokens,
                    },
                )

            # Emit text content
            if assistant_msg.content:
                collected_text.append(assistant_msg.content)
                yield ProviderMessage(type="text", content=assistant_msg.content)

            # No tool calls → done
            if not assistant_msg.tool_calls:
                stopped_early = True
                break

            # Build the assistant message for conversation history
            tool_call_dicts = []
            for tc in assistant_msg.tool_calls:
                tool_call_dicts.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                })
            messages.append({
                "role": "assistant",
                "content": assistant_msg.content or None,
                "tool_calls": tool_call_dicts,
            })

            # Execute each tool call
            for tc in assistant_msg.tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    fn_args = {}

                yield ProviderMessage(
                    type="tool_use",
                    content=f"[Tool: {fn_name}] {str(fn_args)[:300]}",
                    tool_name=fn_name,
                    tool_input=fn_args,
                )

                # Gate write/execute tools on the same permission channel the
                # Claude adapter uses.  Read/Glob/Grep and read-only Bash are
                # classified as safe and run without prompting.
                if permission_callback and tool_needs_approval(fn_name, fn_args):
                    try:
                        allowed = await permission_callback(fn_name, fn_args)
                    except Exception as exc:
                        logger.error("Permission callback failed: %s", exc, exc_info=True)
                        allowed = False
                    if not allowed:
                        result = f"[Permission denied] User did not approve {fn_name}"
                        yield ProviderMessage(type="tool_result", content=result)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result,
                        })
                        continue

                result = await execute_tool(fn_name, fn_args, cwd)
                yield ProviderMessage(
                    type="tool_result",
                    content=result[:500],
                )

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

            # If the model signaled stop despite tool calls, break
            if choice.finish_reason == "stop":
                stopped_early = True
                break

        if not stopped_early:
            # Loop exhausted max_turns with a tool-calling model still going
            # — surface this so the user doesn't see a "successful" result
            # that's actually a cut-off conversation.
            notice = f"[max_turns={max_turns} reached — truncated]"
            collected_text.append(notice)
            yield ProviderMessage(type="text", content=notice)

        # Final result
        result_text = "\n".join(collected_text) if collected_text else ""
        yield ProviderMessage(
            type="result",
            content=result_text,
            usage=total_usage,
            cost_usd=_estimate_cost(effective_model, total_usage),
        )


register("openai", OpenAIAdapter)
