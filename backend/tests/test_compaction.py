"""Smoke tests for context MD compaction.

The Haiku summarizer path requires a live API key, so these tests
exercise the deterministic fallback summarizer + archiving logic by
stubbing ``_summarize_via_haiku`` to return ``None``. That's the
degraded-mode path the backend takes when Haiku is unreachable, and
the one we most need to be correct (users can't tell it's a fallback
until they read the summary).
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest

from app.compaction import (
    HISTORY_DIRNAME,
    compact_context,
    history_dir,
    list_history,
    read_history,
)
from app.context_manager import ContextManager


@pytest.fixture
def ctx(tmp_path):
    return ContextManager(str(tmp_path))


def _run(coro):
    return asyncio.run(coro)


def test_compact_raises_when_no_context(ctx):
    with pytest.raises(FileNotFoundError):
        _run(compact_context(ctx, "ghost"))


def test_compact_archives_original(ctx):
    ctx.create("w1", "Writer")
    ctx.write("w1", "# big\n\n## one\nhello\n\n## two\nworld\n\n## three\n!\n\n## four\n?\n")

    with patch("app.compaction._summarize_via_haiku", return_value=None) as _:
        result = _run(compact_context(ctx, "w1"))

    # Archive exists under .history/ with the returned filename.
    arch_path = os.path.join(history_dir(ctx), result["archived"])
    assert os.path.isfile(arch_path)
    assert result["method"] == "fallback"
    # Original bytes preserved in archive.
    with open(arch_path, "r", encoding="utf-8") as f:
        assert "## two" in f.read()


def test_fallback_summary_keeps_last_three_sections(ctx):
    ctx.create("w1", "Writer")
    # Four `##` sections — fallback must keep the last three verbatim.
    ctx.write(
        "w1",
        "# head\n\n## one\nA\n\n## two\nB\n\n## three\nC\n\n## four\nD\n",
    )

    with patch("app.compaction._summarize_via_haiku", return_value=None):
        _run(compact_context(ctx, "w1"))

    after = ctx.read("w1")
    assert "## two" in after
    assert "## three" in after
    assert "## four" in after
    # Earlier content replaced by the "Compacted" notice.
    assert "Compacted" in after


def test_compact_uses_haiku_output_when_available(ctx):
    ctx.create("w1", "Writer")
    ctx.write("w1", "# huge content\n\n## tail\nkeep\n")

    async def fake_haiku(_content):
        return "# compacted by haiku\n\n## summary\nit worked\n"

    with patch("app.compaction._summarize_via_haiku", side_effect=fake_haiku):
        result = _run(compact_context(ctx, "w1"))

    assert result["method"] == "haiku"
    assert "compacted by haiku" in ctx.read("w1")


def test_list_history_sorts_newest_first(ctx):
    ctx.create("w1", "Writer")

    # Create three archive files with distinct timestamps.
    hist = history_dir(ctx)
    for ts in ("2026-01-01T00-00-00", "2026-02-01T00-00-00", "2026-03-01T00-00-00"):
        with open(os.path.join(hist, f"w1_{ts}.md"), "w", encoding="utf-8") as f:
            f.write(f"archived at {ts}")

    entries = list_history(ctx, "w1")
    assert [e["timestamp"] for e in entries] == [
        "2026-03-01T00-00-00",
        "2026-02-01T00-00-00",
        "2026-01-01T00-00-00",
    ]
    # Only w1's files — not other agents'.
    with open(os.path.join(hist, "other_2026-01-01T00-00-00.md"), "w") as f:
        f.write("x")
    entries2 = list_history(ctx, "w1")
    assert all(e["filename"].startswith("w1_") for e in entries2)


def test_read_history_round_trip(ctx):
    ctx.create("w1", "Writer")
    hist = history_dir(ctx)
    fname = "w1_2026-04-19T12-00-00.md"
    with open(os.path.join(hist, fname), "w", encoding="utf-8") as f:
        f.write("archived content")

    assert read_history(ctx, "w1", fname) == "archived content"


def test_read_history_rejects_wrong_agent(ctx):
    # Requesting w2's archive via w1's agent_id must fail — don't let
    # the frontend accidentally (or deliberately) read arbitrary files.
    ctx.create("w1", "Writer")
    with pytest.raises(ValueError):
        read_history(ctx, "w1", "w2_2026-01-01T00-00-00.md")


def test_read_history_rejects_path_traversal(ctx):
    ctx.create("w1", "Writer")
    with pytest.raises(ValueError):
        read_history(ctx, "w1", "w1_../../secret.md")


def test_history_dirname_is_dotted(ctx):
    # Sanity: the dir is hidden (leading dot) so it doesn't clutter
    # the normal context listing UI.
    assert HISTORY_DIRNAME.startswith(".")
