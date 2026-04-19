"""Two-tier on-disk persistence for a :class:`Project`.

Nothing here touches remote state — it's a local journal so restarts don't
lose agent cards or their recent output.

L1 (:class:`AgentStore`) writes ``workspace/{slug}/agents.json`` on every
significant state transition. On load, we reconstruct :class:`AgentState`
objects with status forced to IDLE (the async tasks are long gone).

L2 (:class:`StreamStore`) appends one JSONL line per output event to
``workspace/{slug}/streams/{agent_id}.jsonl``. The file is tailed on read
and periodically trimmed to the last ``_ROLL_LIMIT`` entries.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from .models import AgentState, OutputEntry

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  L1: agents.json                                                            #
# --------------------------------------------------------------------------- #


class AgentStore:
    """Persists agent metadata so a backend restart preserves the card set.

    Running tasks cannot survive a restart, but their session_id and output
    log tail can — which means the user can resume a conversation without
    starting over.
    """

    def __init__(self, workspace_dir: str) -> None:
        self.path = os.path.join(workspace_dir, "agents.json")

    def save(self, agents: dict[str, AgentState]) -> None:
        data = {
            "agents": [a.model_dump(mode="json") for a in agents.values()],
        }
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("Failed to save %s: %s", self.path, e)

    def load(self) -> list[dict[str, Any]]:
        if not os.path.isfile(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("agents", [])
            return entries if isinstance(entries, list) else []
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to read %s: %s", self.path, e)
            return []


# --------------------------------------------------------------------------- #
#  L2: streams/{agent_id}.jsonl                                               #
# --------------------------------------------------------------------------- #

_ROLL_LIMIT = 500  # keep last 500 entries on disk per agent


class StreamStore:
    """Per-agent rolling JSONL stream of :class:`OutputEntry` events."""

    def __init__(self, workspace_dir: str) -> None:
        self.dir = os.path.join(workspace_dir, "streams")

    def _path(self, agent_id: str) -> str:
        return os.path.join(self.dir, f"{agent_id}.jsonl")

    def append(self, agent_id: str, entry: OutputEntry) -> None:
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self._path(agent_id), "a", encoding="utf-8") as f:
                f.write(entry.model_dump_json() + "\n")
        except OSError as e:
            logger.warning("Failed to append stream for %s: %s", agent_id, e)

    def tail(self, agent_id: str, limit: int = _ROLL_LIMIT) -> list[OutputEntry]:
        path = self._path(agent_id)
        if not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError as e:
            logger.warning("Failed to read stream for %s: %s", agent_id, e)
            return []
        out: list[OutputEntry] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(OutputEntry(**json.loads(line)))
            except (json.JSONDecodeError, ValueError):
                continue
        return out

    def trim(self, agent_id: str, limit: int = _ROLL_LIMIT) -> None:
        """Rewrite the file keeping only the last *limit* entries.

        Called occasionally from the hot path so a single long-running agent
        does not let its stream grow unboundedly.
        """
        path = self._path(agent_id)
        if not os.path.isfile(path):
            return
        entries = self.tail(agent_id, limit=limit)
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for e in entries:
                    f.write(e.model_dump_json() + "\n")
            os.replace(tmp, path)
        except OSError as e:
            logger.warning("Failed to trim stream for %s: %s", agent_id, e)

    def delete(self, agent_id: str) -> None:
        path = self._path(agent_id)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError as e:
                logger.warning("Failed to delete stream for %s: %s", agent_id, e)

    def count(self, agent_id: str) -> int:
        """Approximate line count — cheap enough to call from the hot path."""
        path = self._path(agent_id)
        if not os.path.isfile(path):
            return 0
        try:
            with open(path, "rb") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0
