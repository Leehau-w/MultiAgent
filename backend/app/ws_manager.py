from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

from .models import WSEvent

logger = logging.getLogger(__name__)


# Per-connection send timeout. A backgrounded browser tab can leave its
# TCP receive buffer full; ``ws.send_json`` then blocks until the tab
# wakes up (minutes). Holding any lock across that await cascaded into
# every agent in the project appearing "stuck" — see post-ship-hotfix.md.
WS_SEND_TIMEOUT_SECONDS = 5.0


class WSManager:
    """Manages WebSocket connections and broadcasts events to all clients."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.append(ws)
        logger.info("WebSocket client connected (%d total)", len(self._connections))

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            if ws in self._connections:
                self._connections.remove(ws)
        logger.info("WebSocket client disconnected (%d total)", len(self._connections))

    async def broadcast(self, event: WSEvent) -> None:
        payload = event.model_dump(mode="json")
        await self._broadcast_payload(payload)

    async def broadcast_raw(self, data: dict[str, Any]) -> None:
        await self._broadcast_payload(data)

    async def _broadcast_payload(self, payload: dict[str, Any]) -> None:
        # Snapshot the connection list under the lock, release it, then
        # fan out concurrently. Holding ``_lock`` across ``ws.send_json``
        # awaits in a serial loop was the root cause of the cross-agent
        # stall cascade: one slow/backgrounded browser tab froze every
        # agent's ``_emit`` (which in turn back-pressured the SDK reader
        # into a deadlock with the CLI's control-protocol). ``gather``
        # with ``return_exceptions`` keeps fast clients responsive even
        # when one peer is dead or dragging.
        async with self._lock:
            conns = list(self._connections)
        if not conns:
            return

        send_results = await asyncio.gather(
            *(self._send_one(ws, payload) for ws in conns),
            return_exceptions=False,  # _send_one already catches + returns bool
        )
        stale = [ws for ws, ok in zip(conns, send_results) if not ok]
        if stale:
            async with self._lock:
                for ws in stale:
                    if ws in self._connections:
                        self._connections.remove(ws)

    async def _send_one(self, ws: WebSocket, payload: dict[str, Any]) -> bool:
        """Send *payload* on *ws* with a bounded timeout. Returns ``False``
        on any failure — timeout, socket closed, or serialization error —
        so the caller can evict the connection without the broadcast
        fan-out seeing an exception."""
        try:
            await asyncio.wait_for(
                ws.send_json(payload), timeout=WS_SEND_TIMEOUT_SECONDS
            )
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "WS send exceeded %.1fs — evicting slow client",
                WS_SEND_TIMEOUT_SECONDS,
            )
            return False
        except Exception:
            return False

    @property
    def client_count(self) -> int:
        return len(self._connections)

    # ------------------------------------------------------------------ #
    #  Stage-gate helpers (v0.3.0)                                        #
    # ------------------------------------------------------------------ #

    async def stage_gate_review_started(
        self, project_id: str, stage_name: str
    ) -> None:
        await self.broadcast_raw({
            "type": "stage_gate_review_started",
            "data": {"project_id": project_id, "stage_name": stage_name},
        })

    async def stage_gate_resolved(
        self,
        project_id: str,
        stage_name: str,
        verdict: str,
        summary: str = "",
    ) -> None:
        await self.broadcast_raw({
            "type": "stage_gate_resolved",
            "data": {
                "project_id": project_id,
                "stage_name": stage_name,
                "verdict": verdict,
                "summary": summary,
            },
        })
