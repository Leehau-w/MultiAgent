"""Regression tests for the v0.3.1 stuck-agent pipeline fix.

Two independent bugs surfaced together in the first real pipeline run:

1. ``WSManager.broadcast`` held a global lock across every ``ws.send_json``
   await. A single slow or backgrounded browser tab froze every agent's
   emit path, back-pressured the SDK reader, and deadlocked the CLI's
   control protocol — every agent ended up silently "running" forever.
2. Nothing detected that silent state. Agents sat in ``running`` with no
   stream activity for hours while the wall-clock budget ran out.

This module covers:
  * ``WSManager`` fan-out no longer serialises on slow clients.
  * ``Project._scan_for_stuck`` flips inactive agents to ``STUCK`` and
    emits an event the coord can react to.
  * The ``restart_agent`` coord tool force-stops and re-runs a stuck
    agent in one round-trip.

The codebase doesn't use ``pytest-asyncio``, so each async helper runs
via ``asyncio.run`` in a plain ``def`` test.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.coordinator_tools import build_coordinator_tools
from app.models import AgentState, AgentStatus
from app.ws_manager import WSManager


# ------------------------------------------------------------------ #
#  WSManager — non-blocking fan-out                                   #
# ------------------------------------------------------------------ #


class _FakeWS:
    """Stand-in WebSocket whose ``send_json`` can be made slow on demand.

    Exposes an ``asyncio.Event`` the test can clear to make this client
    hang, mimicking a backgrounded browser tab whose TCP buffer has
    filled. Tracks every payload it received so the test can assert the
    fast client still got everything.
    """

    def __init__(self, *, slow: bool = False) -> None:
        # Build the Event lazily — creating it outside a running loop
        # raises DeprecationWarning on 3.10+ and breaks on 3.14.
        self._release: asyncio.Event | None = None
        self._slow = slow
        self.received: list[dict] = []

    def _event(self) -> asyncio.Event:
        if self._release is None:
            self._release = asyncio.Event()
            if not self._slow:
                self._release.set()
        return self._release

    async def send_json(self, payload: dict) -> None:
        await self._event().wait()
        self.received.append(payload)


def test_slow_client_does_not_stall_fast_client():
    """Before the fix, holding ``_lock`` across ``await ws.send_json``
    meant any slow peer stalled every subsequent broadcast. After the
    fix we fan out with ``asyncio.gather`` and a per-send timeout, so
    a fast client's payload lands promptly even while a slow one hangs.
    """

    async def run() -> tuple[_FakeWS, _FakeWS, WSManager]:
        mgr = WSManager()
        slow = _FakeWS(slow=True)
        fast = _FakeWS(slow=False)
        async with mgr._lock:
            mgr._connections.extend([slow, fast])

        # Shorten the per-send timeout so the slow client is evicted quickly.
        with patch("app.ws_manager.WS_SEND_TIMEOUT_SECONDS", 0.1):
            await mgr.broadcast_raw({"hello": "world"})
        return slow, fast, mgr

    slow, fast, mgr = asyncio.run(run())
    assert fast.received == [{"hello": "world"}]
    # The slow client must have been evicted so subsequent broadcasts
    # don't keep waiting on it.
    assert slow not in mgr._connections
    assert fast in mgr._connections


def test_broadcast_survives_exception_on_one_client():
    """A client that raises inside ``send_json`` is dropped, not
    propagated. Other clients still receive the payload."""

    async def run() -> tuple[_FakeWS, MagicMock, WSManager]:
        mgr = WSManager()
        healthy = _FakeWS(slow=False)
        broken = MagicMock()
        broken.send_json = AsyncMock(side_effect=RuntimeError("socket gone"))
        async with mgr._lock:
            mgr._connections.extend([broken, healthy])
        await mgr.broadcast_raw({"ping": 1})
        return healthy, broken, mgr

    healthy, broken, mgr = asyncio.run(run())
    assert healthy.received == [{"ping": 1}]
    assert broken not in mgr._connections


# ------------------------------------------------------------------ #
#  Watchdog — stuck-agent detection                                   #
# ------------------------------------------------------------------ #


def _register_running_agent(project, agent_id: str, *, idle_seconds: int) -> None:
    """Stash an agent in ``RUNNING`` state whose last activity is
    *idle_seconds* ago. ``started_at`` doubles as the fall-back anchor
    in case a test forgets to set last_activity_at."""
    stale = datetime.now() - timedelta(seconds=idle_seconds)
    project.agents[agent_id] = AgentState(
        id=agent_id,
        role_id="writer",
        role_name="Writer",
        status=AgentStatus.RUNNING,
        context_file=f"{agent_id}.md",
        started_at=stale,
        last_activity_at=stale,
    )
    project._role_map[agent_id] = project.roles["writer"]


def test_watchdog_flips_idle_running_to_stuck(project):
    _register_running_agent(
        project, "w1", idle_seconds=project.WATCHDOG_STUCK_SECONDS + 60
    )
    assert project.agents["w1"].status == AgentStatus.RUNNING

    with patch.object(project, "_emit", new=AsyncMock()), \
            patch.object(project, "_emit_status", new=AsyncMock()):
        asyncio.run(project._scan_for_stuck())

    assert project.agents["w1"].status == AgentStatus.STUCK
    kinds = [e.kind for e in project.events._events]
    assert "agent_stuck" in kinds


def test_watchdog_leaves_active_agent_alone(project):
    """An agent that produced output recently must NOT be flagged —
    tool_results that merely take ~30s to arrive are normal."""
    _register_running_agent(project, "w1", idle_seconds=30)

    with patch.object(project, "_emit", new=AsyncMock()), \
            patch.object(project, "_emit_status", new=AsyncMock()):
        asyncio.run(project._scan_for_stuck())

    assert project.agents["w1"].status == AgentStatus.RUNNING
    assert all(e.kind != "agent_stuck" for e in project.events._events)


def test_stuck_agent_recovers_on_next_message(project):
    """If an agent produces output after being flagged (rare but
    possible — the tool_result finally arrives), the status must drop
    back to RUNNING so the UI isn't stuck on a false red flag."""
    from app.providers.base import ProviderMessage

    _register_running_agent(
        project, "w1", idle_seconds=project.WATCHDOG_STUCK_SECONDS + 60
    )

    async def exercise() -> None:
        with patch.object(project, "_emit", new=AsyncMock()), \
                patch.object(project, "_emit_status", new=AsyncMock()):
            await project._scan_for_stuck()
        assert project.agents["w1"].status == AgentStatus.STUCK

        with patch.object(project, "_emit", new=AsyncMock()), \
                patch.object(project, "_emit_status", new=AsyncMock()):
            await project._handle_provider_message(
                "w1", "sonnet",
                ProviderMessage(type="text", content="finally responding"),
            )

    asyncio.run(exercise())
    assert project.agents["w1"].status == AgentStatus.RUNNING
    assert project.agents["w1"].last_activity_at is not None


# ------------------------------------------------------------------ #
#  restart_agent coord tool                                           #
# ------------------------------------------------------------------ #


def _tool_handlers(project):
    return {t.name: t.handler for t in build_coordinator_tools(project)}


def test_restart_agent_tool_force_stops_and_restarts(project):
    _register_running_agent(
        project, "w1", idle_seconds=project.WATCHDOG_STUCK_SECONDS + 60
    )
    project.agents["w1"].status = AgentStatus.STUCK
    handlers = _tool_handlers(project)

    with patch.object(project, "stop_agent") as stop, \
            patch.object(project, "start_agent") as start:
        result = asyncio.run(handlers["restart_agent"]({
            "agent_id": "w1",
            "prompt": "reread Stage1 and summarise in 5 bullets",
        }))

    assert "isError" not in result, result
    stop.assert_called_once_with("w1")
    start.assert_called_once()
    assert start.call_args.args[0] == "w1"


def test_restart_agent_tool_rejects_missing_args(project):
    _register_running_agent(project, "w1", idle_seconds=10)
    handlers = _tool_handlers(project)

    missing_prompt = asyncio.run(handlers["restart_agent"]({"agent_id": "w1"}))
    assert missing_prompt.get("isError") is True

    missing_id = asyncio.run(handlers["restart_agent"]({"prompt": "go"}))
    assert missing_id.get("isError") is True


def test_restart_agent_tool_rejects_unknown_agent(project):
    handlers = _tool_handlers(project)
    result = asyncio.run(
        handlers["restart_agent"]({"agent_id": "ghost", "prompt": "try"})
    )
    assert result.get("isError") is True
    assert "No such agent" in result["content"][0]["text"]
