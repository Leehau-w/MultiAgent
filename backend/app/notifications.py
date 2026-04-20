"""Persistent notifications emitted by the coordinator for the user.

Track B adds a ``notify_user`` MCP tool the coordinator uses to surface
information, warnings, and blockers to the user outside the chat flow.  The
frontend renders these as toasts; a fresh browser tab calls ``/notifications``
to replay anything still undismissed.

Each entry lives in ``workspace/{slug}/notifications.jsonl`` as a line of
JSON — same convention as :mod:`errors`.  The file is append-only and the
reader tails it with optional ``since`` filtering.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

NOTIFICATIONS_FILENAME = "notifications.jsonl"

Level = Literal["info", "warning", "blocker"]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class NotificationEntry(BaseModel):
    """One record written to ``notifications.jsonl``."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    ts: datetime = Field(default_factory=_now_utc)
    level: Level
    message: str
    action_required: bool = False


def notifications_path(workspace_dir: str) -> str:
    return os.path.join(workspace_dir, NOTIFICATIONS_FILENAME)


def append_notification(
    workspace_dir: str,
    level: Level,
    message: str,
    action_required: bool = False,
) -> NotificationEntry:
    """Persist one notification and return the stored entry.

    Caller is responsible for broadcasting the matching WS event — this
    module only handles durability so a late-connecting browser can replay.
    """
    entry = NotificationEntry(
        level=level,
        message=message,
        action_required=action_required,
    )
    path = notifications_path(workspace_dir)
    try:
        os.makedirs(workspace_dir, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")
    except OSError as exc:
        logger.warning("Failed to append notification to %s: %s", path, exc)
    return entry


def read_notifications(
    workspace_dir: str,
    since: datetime | None = None,
    limit: int = 100,
) -> list[NotificationEntry]:
    """Return recent notifications, newest last.

    * ``since`` — filter to entries with ``ts > since``.  Useful when the
      browser already holds some notifications and only wants the delta.
    * ``limit`` — cap the tail returned (default 100).  Older entries
      remain on disk but are not loaded.
    """
    path = notifications_path(workspace_dir)
    if not os.path.isfile(path):
        return []
    out: list[NotificationEntry] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = NotificationEntry(**json.loads(line))
                except (json.JSONDecodeError, ValueError) as exc:
                    logger.debug("skipping malformed notification: %s", exc)
                    continue
                if since is not None and entry.ts <= since:
                    continue
                out.append(entry)
    except OSError as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return []
    return out[-limit:]


__all__ = [
    "Level",
    "NotificationEntry",
    "NOTIFICATIONS_FILENAME",
    "append_notification",
    "notifications_path",
    "read_notifications",
]
