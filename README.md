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
