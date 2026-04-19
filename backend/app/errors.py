"""Error identification, logging, and retry policy for agent runs.

The SDK loop in :class:`Project._run_agent` used to die on the first exception,
which is how we lost agents mid-task on things like ``Command failed with exit
code 1``. This module centralises:

* A structured :class:`ErrorInfo` record that we persist AND broadcast.
* :func:`classify_error` — a best-effort classifier over exception class names
  and messages (SDK doesn't expose stable error types to us).
* :class:`ErrorLog` — append-only JSONL log per project, capped at ~500 lines
  in memory for the UI panel.
* :class:`RetryPolicy` — per-category retry budget + backoff schedule.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


ErrorCategory = Literal[
    "tool_error",      # a tool returned an error result (recoverable; model handles)
    "api_error",       # network / rate-limit / 5xx (retry with backoff)
    "auth_error",      # 401 / 403 / bad API key (halt)
    "config_error",    # CLI not found, missing env, bad role (halt)
    "sdk_internal",    # subprocess crash, parse error (retry once)
]


class ErrorInfo(BaseModel):
    """Structured error record broadcast on ``agent_error`` and persisted."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: datetime = Field(default_factory=datetime.now)
    agent_id: str
    project_id: str
    category: ErrorCategory
    tool: str | None = None
    tool_input: dict[str, Any] | None = None
    message: str
    stack: str | None = None
    recoverable: bool
    retry_count: int = 0
    final: bool = False  # True once we've given up and halted the agent


# --------------------------------------------------------------------------- #
#  Classification                                                             #
# --------------------------------------------------------------------------- #

_AUTH_HINTS = ("unauthorized", "forbidden", "api key", "authentication", "api_key")
_AUTH_CODES = (" 401", " 403", ":401", ":403")
_CONFIG_HINTS = (
    "clinotfound", "cli not found", "executable not found",
    "no such file", "command not found",
)
_API_HINTS = (
    "rate limit", "rate_limit", "ratelimit", "overloaded", "timeout",
    "timed out", "connection", "network", "502", "503", "504", "429",
)
_TOOL_CRASH_HINTS = ("command failed", "exit code", "non-zero exit")


def classify_error(exc: BaseException) -> tuple[ErrorCategory, bool]:
    """Classify an exception into (category, recoverable).

    We intentionally avoid importing ``claude_agent_sdk`` exception types here —
    the SDK's exception surface is not stable and we'd rather degrade
    gracefully across SDK versions than pin ourselves to specific classes.
    """
    name = type(exc).__name__.lower()
    msg = str(exc).lower()

    # Auth — never retry
    if any(h in msg for h in _AUTH_HINTS) or any(c in msg for c in _AUTH_CODES):
        return ("auth_error", False)

    # Config / missing CLI — never retry
    if any(h in msg for h in _CONFIG_HINTS) or "clinotfound" in name:
        return ("config_error", False)

    # API / network — backoff retry
    if any(h in msg for h in _API_HINTS):
        return ("api_error", True)

    # SDK subprocess / connection crash — one retry. Check the class name
    # before the tool-crash message hints, since ``ProcessError("Command
    # failed with exit code 1")`` is an SDK subprocess crash (the whole run
    # died), not an individual tool's error result.
    if "processerror" in name or "cliconnection" in name or "clierror" in name:
        return ("sdk_internal", True)

    # Tool subprocess crash hints in the message (generic fallback) — the SDK
    # normally routes these back as tool_result; we retry once at the loop
    # level just in case.
    if any(h in msg for h in _TOOL_CRASH_HINTS):
        return ("tool_error", True)

    # Unknown — treat as sdk_internal, one retry
    return ("sdk_internal", True)


# --------------------------------------------------------------------------- #
#  Retry policy                                                               #
# --------------------------------------------------------------------------- #

# Seconds to sleep before each retry. Length of tuple = max attempts.
_BACKOFF: dict[ErrorCategory, tuple[float, ...]] = {
    "api_error":    (1.0, 4.0, 16.0),
    "sdk_internal": (2.0,),
    "tool_error":   (),   # handled by model loop, no outer retry
    "auth_error":   (),
    "config_error": (),
}


def retry_delay(category: ErrorCategory, attempt: int) -> float | None:
    """Return seconds to sleep before *attempt* (1-indexed), or None to halt."""
    schedule = _BACKOFF.get(category, ())
    if attempt < 1 or attempt > len(schedule):
        return None
    return schedule[attempt - 1]


def max_retries(category: ErrorCategory) -> int:
    return len(_BACKOFF.get(category, ()))


# --------------------------------------------------------------------------- #
#  Persistence                                                                #
# --------------------------------------------------------------------------- #

_MAX_IN_MEMORY = 500


class ErrorLog:
    """Append-only JSONL log for one project's errors.

    Lives at ``workspace/{slug}/errors.jsonl``. We keep the last
    ``_MAX_IN_MEMORY`` entries in RAM so the UI panel can query without hitting
    disk. Older entries remain on disk but aren't loaded.
    """

    def __init__(self, workspace_dir: str) -> None:
        self.path = os.path.join(workspace_dir, "errors.jsonl")
        self._recent: list[ErrorInfo] = []
        self._load_tail()

    def _load_tail(self) -> None:
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError as e:
            logger.warning("Failed to read %s: %s", self.path, e)
            return
        for line in lines[-_MAX_IN_MEMORY:]:
            line = line.strip()
            if not line:
                continue
            try:
                self._recent.append(ErrorInfo(**json.loads(line)))
            except (json.JSONDecodeError, ValueError) as e:
                logger.debug("Skipping malformed error entry: %s", e)

    def append(self, info: ErrorInfo) -> None:
        self._recent.append(info)
        if len(self._recent) > _MAX_IN_MEMORY:
            # Trim oldest from memory; disk keeps the full history
            del self._recent[: len(self._recent) - _MAX_IN_MEMORY]
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(info.model_dump_json() + "\n")
        except OSError as e:
            logger.warning("Failed to append to %s: %s", self.path, e)

    def list(self, agent_id: str | None = None, limit: int = 100) -> list[ErrorInfo]:
        items = self._recent
        if agent_id:
            items = [e for e in items if e.agent_id == agent_id]
        return items[-limit:]

    def clear(self) -> None:
        self._recent.clear()
        try:
            if os.path.isfile(self.path):
                os.remove(self.path)
        except OSError as e:
            logger.warning("Failed to clear %s: %s", self.path, e)
