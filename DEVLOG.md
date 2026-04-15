# MultiAgent Studio — Development Log

## Project Overview

MultiAgent Studio is a multi-agent orchestration platform built on top of the Anthropic Claude Agent SDK (`claude-agent-sdk`). It provides a visual dashboard to create, manage, and coordinate multiple Claude agents working on the same project — each with a dedicated role, tool set, and context document.

The core idea: **decompose complex software tasks into roles (PM, TD, Developer, Reviewer), let each agent work within its scope, and share context through Markdown documents** — mimicking how a real engineering team collaborates.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Frontend (React)                   │
│  Vite + React 19 + Zustand + Tailwind CSS v4        │
│                                                      │
│  Dashboard ─── AgentCard[] ─── PipelineProgress      │
│  OutputStream ─── ChatPanel ─── ContextViewer        │
│  PipelineModal ─── RolesEditor ─── Toast             │
└────────────────┬───────────────────┬────────────────┘
                 │ REST /api/*       │ WebSocket /ws
┌────────────────┴───────────────────┴────────────────┐
│                  Backend (FastAPI)                    │
│                                                      │
│  Orchestrator ──── ContextManager ──── WSManager      │
│       │                  │                            │
│       ▼                  ▼                            │
│  claude-agent-sdk   workspace/context/*.md            │
│  (query / resume)   (per-agent Markdown files)        │
└─────────────────────────────────────────────────────┘
```

**Key design decisions:**

1. **Context as Markdown** — Each agent gets a `.md` file in `workspace/context/`. When an agent finishes, its output is written to the file. Downstream agents receive prior agents' context files as part of their prompt. This is simple, inspectable, and version-controllable.

2. **Role configuration as YAML** — Roles live in `backend/config/roles.yaml` and can be edited at runtime through the UI. Each role defines: name, model, tools, system prompt, max turns.

3. **WebSocket for real-time** — All agent status changes, output streams, usage updates, and pipeline progress are pushed to the frontend via WebSocket events. No polling.

4. **Stateless-ish backend** — Agent state lives in memory. Context files provide persistence for the work output. Session IDs from `claude-agent-sdk` allow resuming conversations.

---

## Development Timeline

### Phase 1 — Scaffolding (Initial)

**Backend:**
- Set up FastAPI project structure (`app/main.py`, models, orchestrator)
- Implemented `WSManager` for WebSocket connection management and broadcast
- Implemented `ContextManager` for per-agent Markdown file lifecycle
- Defined data models: `AgentState`, `AgentRole`, `AgentUsage`, `OutputEntry`, `WSEvent`, `PipelineStage`
- Created REST API: CRUD for agents, roles query, pipeline start, context read
- Created `roles.yaml` with 4 default roles: PM, TD, Developer, Reviewer
- Implemented `Orchestrator` with agent lifecycle, claude-agent-sdk integration, and multi-stage pipeline execution

**Frontend:**
- Scaffolded React 19 + Vite + TypeScript + Tailwind v4
- Built Zustand store (`agentStore`) to manage agents, roles, output streams, context cache
- Built `useWebSocket` hook with auto-reconnect
- Built component set: Dashboard, AgentCard, OutputStream, ChatPanel, ContextViewer
- Wired up initial data fetch (roles + agents) and WebSocket event handling

### Phase 2 — Hardening & UX (2025-04-15)

**Backend fixes:**
- Fixed `claude-agent-sdk` integration:
  - Replaced `hasattr()` checks with proper `isinstance()` against `AssistantMessage`, `ResultMessage`, `SystemMessage`
  - Fixed usage tracking — SDK returns `dict`, not object attributes; switched to `dict.get()`
  - Moved `resume` from post-construction assignment to `ClaudeAgentOptions(resume=...)` constructor
  - Leveraged `ResultMessage.total_cost_usd` for accurate cost tracking, with manual estimation as fallback
  - Added `cwd` parameter pointing to workspace directory
  - Added `max_turns` parameter from role config to prevent runaway agents
  - Added `ToolResultBlock` handling in output stream
  - Added graceful degradation: SDK import wrapped in try/except, warns instead of crashing
- Added pipeline progress broadcasting (`pipeline_status` WebSocket events)
- Added static file serving for production: if `frontend/dist/` exists, FastAPI mounts it at `/`
- Added `max_turns` field to `AgentRole` model (default: 30)
- Updated `roles.yaml`: all roles use `sonnet` model, with appropriate `max_turns` per role (15–40)

**Frontend — new components:**
- `PipelineModal` — Full modal for pipeline configuration: requirement input, visual stage editor (add/remove stages, add/remove agents per stage, toggle parallel mode)
- `PipelineProgress` — Horizontal stage progress bar below header, shows current stage, completion status, parallel indicators
- `RolesEditor` — Full modal with monospace YAML editor, fetches current config, saves and refreshes store on save
- `Toast` + `toastStore` — Toast notification system (success/error/info), auto-dismiss after 5 seconds, slide-up animation

**Frontend — improvements:**
- Created unified API layer (`utils/api.ts`): `apiFetch`, `apiPost`, `apiPut`, `apiDelete` — all wrap fetch with automatic error → toast notification
- Migrated all components (AgentCard, Dashboard, ChatPanel, PipelineModal, RolesEditor) to use the new API layer
- Updated `agentStore` to handle `pipeline_status` events
- Updated `WSEvent` type to include `pipeline_status`
- Fixed AgentCard hover button visibility (added `group` class for proper CSS hover)
- Added slide-up CSS animation for toast

**DevOps:**
- `git init` + `.gitignore` (Python, Node, IDE, workspace runtime data, env files)
- `start.sh` — Linux/Mac startup script: auto-creates venv, installs deps, starts backend (uvicorn) + frontend (vite dev), trap for clean shutdown
- `start.bat` — Windows startup script: same flow, opens backend and frontend in separate windows
- `Dockerfile` — Multi-stage build: Node 22 for frontend build, Python 3.13-slim for runtime, serves built frontend as static files
- `docker-compose.yml` — Single-service config, mounts workspace and config volumes, passes `ANTHROPIC_API_KEY` from env

---

## File Structure

```
MultiAgent/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py              # FastAPI app, routes, lifespan
│   │   ├── models.py            # Pydantic models
│   │   ├── orchestrator.py      # Agent lifecycle, SDK integration, pipeline
│   │   ├── context_manager.py   # Per-agent Markdown file management
│   │   └── ws_manager.py        # WebSocket connection pool
│   ├── config/
│   │   └── roles.yaml           # Role definitions
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── AgentCard.tsx     # Individual agent card with actions
│   │   │   ├── ChatPanel.tsx     # Message input to agents
│   │   │   ├── ContextViewer.tsx # Markdown context display
│   │   │   ├── Dashboard.tsx     # Agent cards grid + create form
│   │   │   ├── OutputStream.tsx  # Real-time output stream
│   │   │   ├── PipelineModal.tsx # Pipeline configuration modal
│   │   │   ├── PipelineProgress.tsx # Stage progress bar
│   │   │   ├── RolesEditor.tsx   # YAML role editor modal
│   │   │   └── Toast.tsx         # Toast notification component
│   │   ├── hooks/
│   │   │   └── useWebSocket.ts   # WebSocket hook with auto-reconnect
│   │   ├── stores/
│   │   │   ├── agentStore.ts     # Main Zustand store
│   │   │   └── toastStore.ts     # Toast notification store
│   │   ├── types/
│   │   │   └── index.ts          # TypeScript type definitions
│   │   ├── utils/
│   │   │   └── api.ts            # Unified API fetch with error handling
│   │   ├── App.tsx               # Root component
│   │   ├── main.tsx              # Entry point
│   │   └── index.css             # Global styles + Tailwind
│   ├── package.json
│   ├── vite.config.ts
│   └── tsconfig*.json
├── workspace/
│   └── context/                  # Runtime: per-agent .md files
├── Dockerfile
├── docker-compose.yml
├── start.sh
├── start.bat
└── .gitignore
```

---

## Known Limitations & Future Work

### Current limitations

- **No persistence** — Agent state lives in memory. Server restart loses all agents (context files survive in workspace).
- **No authentication** — CORS is fully open. Suitable for local development only.
- **Single-user** — No concept of sessions or user isolation.
- **No agent-to-agent direct communication** — Agents share context through files, not real-time messages.

### Planned improvements

- [ ] SQLite/file-based persistence for agent state and pipeline history
- [ ] Auth middleware (API key or OAuth) for multi-user deployment
- [ ] Agent-to-agent messaging (agent A can @mention agent B)
- [ ] Pipeline templates library (save/load pipeline configurations)
- [ ] Cost dashboard with per-pipeline and per-agent cost breakdowns
- [ ] Log export (download all agent outputs as a single document)
- [ ] Diff view for context documents (see how context evolves across pipeline stages)
- [ ] Support for custom MCP tools per role (configured in roles.yaml)
