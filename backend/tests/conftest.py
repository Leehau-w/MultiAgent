"""Pytest fixtures shared by the smoke tests.

The tests here don't talk to a real LLM — they stub the provider adapter
with :class:`FakeAdapter` so each pass yields a scripted result. That keeps
the smoke tests fast, deterministic and offline.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

# Make ``app`` importable when tests are invoked from the backend root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import AgentRole, ProjectMeta
from app.project import Project
from app.providers.base import ProviderAdapter, ProviderMessage
from app.ws_manager import WSManager


class FakeAdapter(ProviderAdapter):
    """Scripted adapter — pops a result off ``self.scripted`` per ``run()`` call.

    The script is a list of ``(result_text, tool_calls)`` tuples where
    ``tool_calls`` is a list of ``(tool_name, tool_input)`` pairs that the
    adapter should emit before the final result. ``tool_calls`` does NOT
    execute anything itself — for MCP tests we invoke the tool callback
    directly, since SDK plumbing isn't exercised here.
    """

    def __init__(self) -> None:
        self.scripted: list[tuple[str, list]] = []
        self.runs: list[dict] = []

    async def run(  # type: ignore[override]
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
        permission_callback=None,
        mcp_servers=None,
    ) -> AsyncIterator[ProviderMessage]:
        self.runs.append({
            "prompt": prompt,
            "system_prompt": system_prompt,
            "model": model,
            "tools": list(tools),
            "cwd": cwd,
            "session_id": session_id,
            "mcp_servers": mcp_servers,
        })
        if self.scripted:
            result_text, _tool_calls = self.scripted.pop(0)
        else:
            result_text = "ok"
        yield ProviderMessage(type="text", content=result_text)
        yield ProviderMessage(
            type="result",
            content=result_text,
            session_id=session_id or "session-fake",
            usage={"input_tokens": 10, "output_tokens": 5},
            cost_usd=0.0,
        )


@pytest.fixture
def roles() -> dict[str, AgentRole]:
    return {
        "coordinator": AgentRole(
            id="coordinator",
            name="Coordinator",
            description="routes work",
            provider="claude",
            model="opus",
            tools=["Read", "Write"],
            system_prompt="You coordinate.",
        ),
        "writer": AgentRole(
            id="writer",
            name="Writer",
            description="writes things",
            provider="claude",
            model="sonnet",
            tools=["Write"],
            system_prompt="You write.",
        ),
    }


@pytest.fixture
def workspace(tmp_path):
    return str(tmp_path / "ws")


@pytest.fixture
def project(roles, workspace):
    os.makedirs(workspace, exist_ok=True)
    meta = ProjectMeta(id="proj-smoke", name="Smoke", project_dir=os.getcwd())
    return Project(meta, WSManager(), roles, workspace)
