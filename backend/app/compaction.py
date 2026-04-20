"""Context document compaction.

Long-running agents accumulate ``context/<agent_id>.md`` entries until
the file is too big to inject into a downstream agent's prompt. Rather
than auto-truncating (which loses information silently), we expose a
manual "Compact" action: the current file is archived to
``context/.history/<agent_id>_<timestamp>.md`` and then rewritten to a
concise summary produced by Claude Haiku.

The summarizer prompt is deliberately conservative — the last three
``##`` sections are kept verbatim so freshly-produced output isn't
stripped, and the preamble above them is replaced by a short summary.
If the summarizer call fails for any reason we leave the original file
untouched; the archive is still written so the user can retry safely.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import datetime
from typing import Any

from .context_manager import ContextManager

logger = logging.getLogger(__name__)


HISTORY_DIRNAME = ".history"

_SUMMARIZER_SYSTEM = (
    "You compact agent context documents. Keep the last 3 `##` sections "
    "verbatim. Replace earlier content with a concise summary (<= 500 tokens) "
    "that preserves: key decisions, open questions, file references, and "
    "next steps. Output the new markdown content directly with no preamble "
    "and no code fences."
)

_SUMMARIZER_PROMPT_TEMPLATE = (
    "Compact the following context document. Preserve the final three `##` "
    "sections verbatim; summarize everything above them.\n\n"
    "---BEGIN CONTEXT---\n{content}\n---END CONTEXT---"
)


def history_dir(ctx: ContextManager) -> str:
    path = os.path.join(ctx.context_dir, HISTORY_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def _archive(ctx: ContextManager, agent_id: str, content: str) -> str:
    """Copy current context MD into the history dir. Returns filename
    (not full path) so callers can surface it in toasts/logs."""
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    fname = f"{agent_id}_{ts}.md"
    dest = os.path.join(history_dir(ctx), fname)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(content)
    return fname


def _fallback_summary(content: str) -> str:
    """Truncate-and-keep-last-3-sections summarizer for when Haiku isn't
    available. Keeps the file readable without ever silently dropping
    the most recent work."""
    marker = "\n## "
    idx = content.rfind(marker)
    tail = content
    kept_sections = 0
    while idx >= 0 and kept_sections < 3:
        tail = content[idx + 1:]
        idx = content.rfind(marker, 0, idx)
        kept_sections += 1
    head_note = (
        "> **Compacted** — earlier content archived to "
        "`context/.history/`. The sections below are preserved verbatim.\n\n"
    )
    return head_note + tail


async def _summarize_via_haiku(content: str) -> str | None:
    """Run Claude Haiku over *content*. Returns the new MD body, or
    ``None`` if the SDK call fails (import error, auth error, whatever —
    we degrade to the fallback summarizer rather than bubble up)."""
    try:
        from .providers.claude_adapter import ClaudeAdapter
    except Exception as exc:  # noqa: BLE001
        logger.warning("Haiku unavailable: %s", exc)
        return None

    prompt = _SUMMARIZER_PROMPT_TEMPLATE.format(content=content)
    collected: list[str] = []
    try:
        adapter = ClaudeAdapter()
        async for msg in adapter.run(
            prompt=prompt,
            system_prompt=_SUMMARIZER_SYSTEM,
            model="haiku",
            tools=[],  # summarizer gets no tools
            cwd=os.getcwd(),
            max_turns=1,
        ):
            if msg.type == "text" and msg.content:
                collected.append(msg.content)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Haiku summarizer failed: %s", exc)
        return None
    text = "".join(collected).strip()
    return text or None


async def compact_context(ctx: ContextManager, agent_id: str) -> dict[str, Any]:
    """Archive + rewrite ``context/<agent_id>.md`` in place.

    Returns a dict with ``archived`` (filename of the backup),
    ``method`` (``"haiku"`` or ``"fallback"``), ``before_bytes``,
    ``after_bytes``. Raises ``FileNotFoundError`` if no context exists
    for *agent_id*.
    """
    original = ctx.read(agent_id)
    if not original:
        raise FileNotFoundError(agent_id)

    archived = _archive(ctx, agent_id, original)

    summary = await _summarize_via_haiku(original)
    method = "haiku"
    if summary is None:
        summary = _fallback_summary(original)
        method = "fallback"

    ctx.write(agent_id, summary)
    return {
        "archived": archived,
        "method": method,
        "before_bytes": len(original.encode("utf-8")),
        "after_bytes": len(summary.encode("utf-8")),
    }


def list_history(ctx: ContextManager, agent_id: str) -> list[dict[str, Any]]:
    """Return archived versions for *agent_id*, newest first.

    Entries are shaped for direct JSON serialization:
    ``{"filename", "timestamp", "size_bytes"}``.
    """
    root = history_dir(ctx)
    prefix = f"{agent_id}_"
    out: list[dict[str, Any]] = []
    for fname in os.listdir(root):
        if not fname.startswith(prefix) or not fname.endswith(".md"):
            continue
        full = os.path.join(root, fname)
        try:
            st = os.stat(full)
        except OSError:
            continue
        ts_part = fname[len(prefix):-3]  # strip prefix + ".md"
        out.append({
            "filename": fname,
            "timestamp": ts_part,
            "size_bytes": st.st_size,
        })
    out.sort(key=lambda e: e["timestamp"], reverse=True)
    return out


def read_history(ctx: ContextManager, agent_id: str, filename: str) -> str:
    """Read one archived version. Raises ``FileNotFoundError`` if
    absent, ``ValueError`` if *filename* tries to escape the history
    dir or doesn't belong to *agent_id*."""
    # Reject separators or parent-dir tokens outright — an archive
    # filename is always a flat basename.
    if (
        "/" in filename
        or "\\" in filename
        or ".." in filename
        or filename in (".", "")
    ):
        raise ValueError(f"Invalid history filename: {filename!r}")
    if not filename.startswith(f"{agent_id}_") or not filename.endswith(".md"):
        raise ValueError(f"Filename {filename!r} does not belong to {agent_id!r}")
    root = history_dir(ctx)
    path = os.path.join(root, filename)
    # Belt-and-braces: after the basename checks, the resolved path
    # must still be under history_dir.
    full = os.path.realpath(path)
    root_real = os.path.realpath(root)
    try:
        if os.path.commonpath([full, root_real]) != root_real:
            raise ValueError(f"Invalid history filename: {filename!r}")
    except ValueError:
        raise ValueError(f"Invalid history filename: {filename!r}")
    if not os.path.isfile(full):
        raise FileNotFoundError(filename)
    with open(full, "r", encoding="utf-8") as f:
        return f.read()


__all__ = [
    "HISTORY_DIRNAME",
    "compact_context",
    "history_dir",
    "list_history",
    "read_history",
]
