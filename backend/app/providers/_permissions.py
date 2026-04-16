"""Shared tool-permission classification used by all provider adapters.

The orchestrator always passes a ``permission_callback`` to the adapter.  Each
adapter decides which tool calls to short-circuit as auto-allowed (read-only,
no side effects) and which to forward to the UI via the callback.
"""

from __future__ import annotations

import re

# Tools that never trigger a permission prompt — they have no side effects.
READONLY_TOOLS: frozenset[str] = frozenset({"Read", "Glob", "Grep"})

# Bash — read-only command whitelist.
_READONLY_COMMANDS: frozenset[str] = frozenset({
    "ls", "dir", "pwd", "cat", "head", "tail", "wc", "file", "stat",
    "du", "df", "tree", "which", "type", "echo", "printf", "find",
    "grep", "rg", "ag", "fd", "less", "more", "sort", "uniq", "diff",
    "env", "printenv", "whoami", "hostname", "uname", "date", "id",
    "realpath", "dirname", "basename", "sha256sum", "md5sum",
})

_READONLY_GIT_SUBCMDS: frozenset[str] = frozenset({
    "status", "log", "diff", "show", "branch", "tag", "remote",
    "describe", "shortlog", "blame", "ls-files", "ls-tree",
})

_READONLY_PREFIXES: tuple[str, ...] = (
    "node --version", "node -v",
    "python --version", "python -V", "python3 --version",
    "npm list", "npm ls", "npm --version", "npm -v",
    "pip list", "pip show", "pip --version",
    "cargo --version", "go version", "java --version",
    "rustc --version", "dotnet --version",
)


def is_readonly_bash(command: str) -> bool:
    """Return True when *command* has no side effects (no files written, no
    network calls, no state changes).  Conservative: unknown → not read-only.
    """
    # Output redirects (>, >>) are writes even if the command is read-only.
    # Skip 2> (stderr redirect to a file descriptor) — detection is rough
    # but good enough; a leading "2>&1" is safe because it redirects to fd 1.
    if re.search(r"(?<![0-9])>|>>", command):
        return False

    # ALL parts of a piped/chained command must be read-only.
    parts = re.split(r"\s*(?:&&|\|\||;|\|)\s*", command.strip())

    for part in parts:
        part = part.strip()
        if not part:
            continue

        if any(part.startswith(p) for p in _READONLY_PREFIXES):
            continue

        tokens = part.split()
        if not tokens:
            continue
        cmd = tokens[0].rsplit("/", 1)[-1]  # strip a leading path

        if cmd == "git" and len(tokens) >= 2:
            if tokens[1] in _READONLY_GIT_SUBCMDS:
                continue
            return False

        if cmd in _READONLY_COMMANDS:
            continue

        return False

    return True


def tool_needs_approval(tool_name: str, tool_input: dict) -> bool:
    """Return True when a tool call should be gated by the permission panel."""
    if tool_name in READONLY_TOOLS:
        return False
    if tool_name == "Bash":
        return not is_readonly_bash(tool_input.get("command", ""))
    return True
