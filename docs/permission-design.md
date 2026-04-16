# Permission Approval System — Fine-grained Design

## Problem

Current implementation passes all configured tools via `allowed_tools` (CLI `--allowedTools`),
which pre-approves them. The SDK's `can_use_tool` callback is never triggered, so the
frontend PermissionPanel never shows anything.

## Goal

Agent reads/searches freely without interruption; any write/execute operation requires
user approval via the frontend panel.

## Architecture

```
Agent calls tool
       |
       v
  CLI subprocess
       |
  tool in allowedTools? ──yes──> execute directly (no callback)
       |no
       v
  send permission request via stdio control protocol
       |
       v
  SDK Query receives "can_use_tool" control message
       |
       v
  call can_use_tool(tool_name, tool_input, ctx)
       |
       v
  [claude_adapter] bridge to main loop ──> [orchestrator] emit WS event
       |                                          |
       v                                          v
  await result <─────────────────────── frontend Allow/Deny
       |
       v
  Allow → execute tool
  Deny  → skip tool, return denial message to agent
```

## Tool Classification

### Auto-allow (put in `allowed_tools`, bypass `can_use_tool`)

| Tool   | Reason                    |
|--------|---------------------------|
| Read   | Read file, no side effect |
| Glob   | Find files, no side effect|
| Grep   | Search content, no side effect |

### Require approval (NOT in `allowed_tools`, handled by `can_use_tool`)

| Tool   | Reason                           |
|--------|----------------------------------|
| Write  | Creates/overwrites files         |
| Edit   | Modifies existing files          |
| Bash   | Depends on command (see below)   |

### Bash — command-level classification

Bash is the most complex case. Inspect `tool_input["command"]` to determine risk level.

**Read-only whitelist** (auto-allow in `can_use_tool`, return `PermissionResultAllow`):

```
ls, dir, pwd, cat, head, tail, wc, file, stat, du, df, tree, which, type, echo,
find, grep, rg, ag, fd,
git status, git log, git diff, git show, git branch, git tag, git remote -v,
node --version, python --version, npm list, pip list, pip show,
env, printenv, whoami, hostname, uname, date
```

Detection logic: extract the first token of the command (before `|`, `&&`, `;`),
strip leading `env`/`sudo`/path prefixes, match against whitelist.

**Write/execute commands** (require approval — return pending, wait for UI):

Everything not in the read-only whitelist, including but not limited to:
```
rm, mv, cp, mkdir, touch, chmod, chown,
git add, git commit, git push, git checkout, git reset, git rebase, git merge,
npm install, npm run, npx, yarn, pnpm,
pip install, pip uninstall,
curl, wget, ssh, scp,
docker, kubectl,
python, node (running scripts),
make, cargo, go build
```

## Code Changes

### 1. `backend/app/providers/claude_adapter.py`

**File**: `claude_adapter.py` lines 137-147

Current:
```python
opts: dict = dict(
    allowed_tools=tools,
    model=model,
    ...
)
```

Change: split `tools` into two lists based on a classification function.

```python
READONLY_TOOLS = {"Read", "Glob", "Grep"}

auto_allow = [t for t in tools if t in READONLY_TOOLS]
needs_approval = [t for t in tools if t not in READONLY_TOOLS]

opts: dict = dict(
    tools=tools,                # all tools available (--tools)
    allowed_tools=auto_allow,   # only read-only tools pre-approved (--allowedTools)
    model=model,
    ...
)
```

This makes Write, Edit, Bash go through `can_use_tool`.

### 2. `backend/app/providers/claude_adapter.py` — Bash smart filter

Add a helper function that the `can_use_tool` bridge calls to decide whether
a Bash command is read-only:

```python
import shlex

_READONLY_COMMANDS = {
    "ls", "dir", "pwd", "cat", "head", "tail", "wc", "file", "stat",
    "du", "df", "tree", "which", "type", "echo", "printf", "find",
    "grep", "rg", "ag", "fd", "less", "more", "sort", "uniq", "diff",
    "env", "printenv", "whoami", "hostname", "uname", "date", "id",
}

_READONLY_GIT = {
    "status", "log", "diff", "show", "branch", "tag", "remote",
    "describe", "shortlog", "blame", "ls-files", "ls-tree",
}

_READONLY_PREFIXES = {
    "node --version", "python --version", "python3 --version",
    "npm list", "npm ls", "pip list", "pip show", "pip --version",
    "cargo --version", "go version", "java --version", "rustc --version",
}

def _is_readonly_bash(command: str) -> bool:
    """Check if a bash command is read-only (no side effects)."""
    # Split on shell operators to get individual commands
    # Handle &&, ||, ;, | — ALL parts must be read-only
    import re
    parts = re.split(r'\s*(?:&&|\|\||;|\|)\s*', command.strip())

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Check prefix matches first (multi-word commands)
        if any(part.startswith(p) for p in _READONLY_PREFIXES):
            continue

        # Extract first token
        tokens = part.split()
        if not tokens:
            continue
        cmd = tokens[0].rsplit("/", 1)[-1]  # strip path prefix

        # git subcommand check
        if cmd == "git" and len(tokens) >= 2:
            if tokens[1] in _READONLY_GIT:
                continue
            return False

        if cmd in _READONLY_COMMANDS:
            continue

        # Not in whitelist → needs approval
        return False

    return True
```

### 3. `backend/app/providers/claude_adapter.py` — Update `can_use_tool` logic

In both the Windows thread path and non-Windows path, the `_can_use_tool` callback
should auto-allow read-only Bash commands instead of always forwarding to the UI:

```python
async def _can_use_tool(tool_name, tool_input, _ctx):
    # Auto-allow read-only Bash commands
    if tool_name == "Bash" and _is_readonly_bash(tool_input.get("command", "")):
        return PermissionResultAllow()

    # Everything else → ask the user
    allowed = await perm_cb(tool_name, tool_input)
    if allowed:
        return PermissionResultAllow()
    return PermissionResultDeny(reason="User denied")
```

### 4. No frontend changes needed

The existing PermissionPanel, store, and WebSocket handling are already correct.
Once the backend starts actually triggering `can_use_tool` for Write/Edit/Bash-write,
the events will flow through and the panel will render.

## Summary of changes

| File | Change |
|------|--------|
| `claude_adapter.py` | Split tools into `tools` + `allowed_tools` (read-only only) |
| `claude_adapter.py` | Add `_is_readonly_bash()` helper |
| `claude_adapter.py` | Update `_can_use_tool` to auto-allow read-only Bash |
| Frontend | No changes needed |
| `roles.yaml` | No changes needed |

## Edge cases

- **Piped commands**: `cat file | grep foo` — both parts checked, both read-only → allow
- **Chained commands**: `ls && rm file` — `rm` not in whitelist → require approval
- **Subshells / backticks**: Not parsed deeply; if the outer command is unknown → require approval (safe default)
- **Redirects**: `echo x > file` — `echo` is in whitelist but redirect writes a file. Consider stripping redirects or flagging `>`, `>>` as write operations
- **Timeout**: 5-minute timeout on permission wait; auto-deny on timeout
