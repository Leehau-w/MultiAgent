# MultiAgent Studio

A visual multi-agent orchestration platform. Create AI agent teams with different roles (PM, Architect, Developer, Reviewer), coordinate them through pipelines, and watch them collaborate in real time.

**Supports multiple LLM providers** — Claude, OpenAI, and Ollama (local models) can work together in the same pipeline.

[English Guide](./docs/USERGUIDE_EN.md) | [中文指南](./docs/USERGUIDE_CN.md)

---

## Quick Start

### Prerequisites

- Python 3.10+
- Node.js 18+
- At least one LLM provider configured (see below)

### One-click startup

**Windows:**
```bat
start.bat
```

**Linux / macOS:**
```bash
./start.sh
```

Opens **http://localhost:5173** automatically.

### Docker

```bash
docker compose up --build
```

Opens **http://localhost:8000**.

---

## Provider Setup

| Provider | Setup | Use Case |
|----------|-------|----------|
| **Claude** | `claude login` (Max plan) or set `ANTHROPIC_API_KEY` | Best agentic capabilities |
| **OpenAI** | Set `OPENAI_API_KEY` | GPT-4o / GPT-4o-mini |
| **Ollama** | Run `ollama serve` locally | Free, private, offline |

Mix providers in the same pipeline — e.g. Claude PM + OpenAI Developer + Ollama Reviewer.

Configure per role in `backend/config/roles.yaml`:

```yaml
pm:
  provider: "claude"
  model: "sonnet"

developer:
  provider: "openai"
  model: "gpt-4o-mini"

reviewer:
  provider: "ollama"
  model: "qwen2.5-coder"
```

---

## Features

- **Role-based agents** — PM, TD, Developer, Reviewer with customizable system prompts and tools
- **Visual pipeline editor** — configure stages, parallel execution, agent assignment
- **Real-time output stream** — watch agents think and act as it happens
- **Context sharing** — agents pass Markdown documents to downstream agents
- **Multi-provider** — Claude, OpenAI, Ollama in one pipeline
- **Project switcher** — point agents at any project directory from the UI
- **Roles editor** — edit role config (YAML) live in the browser
- **Usage tracking** — token counts and cost per agent
- **Session resume** — continue conversations with agents

### New in v0.3.1

- **Stuck-agent watchdog** — flags any `RUNNING` agent with no stream activity past 8 min as `stuck`; recovers automatically on next provider message. Visible as a pulsing rose dot on the agent card.
- **Coordinator restart tool** — `restart_agent(agent_id, prompt)` tree-kills a wedged CLI subprocess and re-runs the worker with a fresh prompt (session_id preserved).
- **WebSocket fan-out fix** — broadcasts no longer serialize behind a global lock; slow or backgrounded browser tabs can't wedge the SDK control channel anymore.
- **Budget sticky-flag hotfix** — raising a cap in `workflow.yaml` now clears a stale `BudgetExceeded` trip on the next start.

### New in v0.3.0

- **Stage-gate pipeline** — stages run to completion, then the coordinator reviews against `acceptance_criteria` before advancing; pause/retry/force-advance controls in the pipeline banner.
- **User ↔ coordinator dialog** — message the coord directly from the chat panel; coord decisions and user overrides are logged with rationale.
- **Pipeline-level status UI** — pipeline banner, workflow graph with current-stage highlight, error panel, and desktop-style notifications (info / warning / blocker).
- **Permission panel** — cross-agent permission request queue with per-request approve/deny; global mode (`manual` / `workspace` / `bypass`) with per-agent override.

### New in v0.2.0

- **Declarative workflows** — `workflow.yaml` per project with stages, budget, triggers, coordinator config; UI editor under the **Workflow** button. See [workflow guide](./docs/workflow-guide.md).
- **Budget enforcement** — cap cost / turns / wall-clock / concurrency per project; visible in a header bar that trips amber at 70%, red at 90%.
- **Event-driven triggers** — one agent's completion can fan out in parallel, AND-join into a reviewer, or hand off to a coordinator.
- **Stateless coordinator** — a `coordinator` role with 4-block externalized memory (`facts` / `hypothesis` / `open_questions` / `decisions`) at `coordinator_state.yaml`, re-invoked fresh per event.
- **Error resilience** — errors classified into tool/api/auth/sdk; transient errors retry with backoff, fatal errors halt the agent; full error log at `workspace/{project}/errors.jsonl`.
- **Multi-project isolation** — open multiple projects in separate tabs; each has its own agents, budget, permission mode, context.
- **Persistence** — agent metadata and a 500-entry rolling output window survive backend restarts.
- **Context compaction** — manual Compact button archives the current MD to `.history/` and replaces it with a Haiku-summarised version.

---

## Architecture

```
Frontend (React 19 + Zustand + Tailwind v4)
    ↕ REST + WebSocket
Backend (FastAPI)
    ↕
Provider Adapters
    ├── Claude  → claude-agent-sdk
    ├── OpenAI  → openai SDK + tool execution
    └── Ollama  → OpenAI-compatible local API
```

---

## License

MIT
