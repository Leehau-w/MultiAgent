"""Shared tool implementations for non-Claude providers (OpenAI, Ollama, etc.).

Each tool function receives parsed arguments and a working directory, and
returns a plain-text result string.  The TOOL_SCHEMAS dict provides the
OpenAI-compatible function-calling JSON schema for each tool.
"""

from __future__ import annotations

import asyncio
import glob as _glob
import os
import re


# ------------------------------------------------------------------ #
#  Tool schemas (OpenAI function-calling format)                      #
# ------------------------------------------------------------------ #

TOOL_SCHEMAS: dict[str, dict] = {
    "Read": {
        "type": "function",
        "function": {
            "name": "Read",
            "description": "Read a file and return its contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file.",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    "Write": {
        "type": "function",
        "function": {
            "name": "Write",
            "description": "Create or overwrite a file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The full content to write.",
                    },
                },
                "required": ["file_path", "content"],
            },
        },
    },
    "Edit": {
        "type": "function",
        "function": {
            "name": "Edit",
            "description": "Replace an exact substring in a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "The exact text to find.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "The replacement text.",
                    },
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        },
    },
    "Bash": {
        "type": "function",
        "function": {
            "name": "Bash",
            "description": "Execute a shell command and return stdout+stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    "Glob": {
        "type": "function",
        "function": {
            "name": "Glob",
            "description": "Find files matching a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern, e.g. '**/*.py'.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search in (defaults to cwd).",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    "Grep": {
        "type": "function",
        "function": {
            "name": "Grep",
            "description": "Search file contents for a regex pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "File or directory to search in.",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Only search files matching this glob.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
}


def get_tool_schemas(tool_names: list[str]) -> list[dict]:
    """Return the OpenAI function-calling schemas for the requested tools."""
    return [TOOL_SCHEMAS[t] for t in tool_names if t in TOOL_SCHEMAS]


# ------------------------------------------------------------------ #
#  Tool execution                                                     #
# ------------------------------------------------------------------ #

def _resolve(path: str, cwd: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(cwd, path))


async def execute_tool(name: str, args: dict, cwd: str) -> str:
    """Run a tool and return its output as a string."""
    try:
        if name == "Read":
            return _tool_read(args, cwd)
        elif name == "Write":
            return _tool_write(args, cwd)
        elif name == "Edit":
            return _tool_edit(args, cwd)
        elif name == "Bash":
            return await _tool_bash(args, cwd)
        elif name == "Glob":
            return _tool_glob(args, cwd)
        elif name == "Grep":
            return _tool_grep(args, cwd)
        else:
            return f"[Unknown tool: {name}]"
    except Exception as e:
        return f"[Tool error] {e}"


def _tool_read(args: dict, cwd: str) -> str:
    path = _resolve(args["file_path"], cwd)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    if len(content) > 100_000:
        content = content[:100_000] + "\n... (truncated)"
    return content


def _tool_write(args: dict, cwd: str) -> str:
    path = _resolve(args["file_path"], cwd)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(args["content"])
    return f"File written: {path}"


def _tool_edit(args: dict, cwd: str) -> str:
    path = _resolve(args["file_path"], cwd)
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    old = args["old_string"]
    new = args["new_string"]
    if old not in content:
        return f"[Edit failed] old_string not found in {path}"
    content = content.replace(old, new, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"File edited: {path}"


async def _tool_bash(args: dict, cwd: str) -> str:
    cmd = args["command"]
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        # Kill the child — otherwise it keeps running past the return and
        # leaks pipes/descriptors.
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:
            pass
        return "[Bash timeout] Command exceeded 120s and was killed."
    output = stdout.decode("utf-8", errors="replace")
    if len(output) > 50_000:
        output = output[:50_000] + "\n... (truncated)"
    exit_info = f"[exit code: {proc.returncode}]" if proc.returncode else ""
    return f"{output}{exit_info}".strip()


def _tool_glob(args: dict, cwd: str) -> str:
    base = _resolve(args.get("path", ""), cwd) if args.get("path") else cwd
    pattern = args["pattern"]
    matches = sorted(_glob.glob(os.path.join(base, pattern), recursive=True))
    if not matches:
        return "No files found."
    if len(matches) > 200:
        matches = matches[:200]
        matches.append("... (truncated)")
    return "\n".join(matches)


def _tool_grep(args: dict, cwd: str) -> str:
    pattern = args["pattern"]
    base = _resolve(args.get("path", ""), cwd) if args.get("path") else cwd
    file_glob = args.get("glob", "**/*")

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"[Regex error] {e}"

    results: list[str] = []
    if os.path.isfile(base):
        files = [base]
    else:
        files = _glob.glob(os.path.join(base, file_glob), recursive=True)

    for fpath in files[:500]:
        if not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if regex.search(line):
                        results.append(f"{fpath}:{i}: {line.rstrip()}")
                        if len(results) >= 200:
                            results.append("... (truncated)")
                            return "\n".join(results)
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(results) if results else "No matches found."
