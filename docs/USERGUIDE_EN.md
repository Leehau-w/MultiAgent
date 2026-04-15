# MultiAgent Studio — User Guide

## What is MultiAgent Studio?

MultiAgent Studio is a visual platform for orchestrating multiple AI agents. Each agent takes on a specific role (Product Manager, Technical Director, Developer, Code Reviewer) and works on a shared project. Agents share context through Markdown documents, enabling a multi-role collaborative workflow.

Supports **Claude**, **OpenAI**, and **Ollama** (local models) — mix and match in a single pipeline.

---

## Quick Start

### Prerequisites

- **Python 3.10+** (backend)
- **Node.js 18+** (frontend)
- At least one LLM provider:
  - **Claude**: `claude login` (Max plan) or `ANTHROPIC_API_KEY`
  - **OpenAI**: `OPENAI_API_KEY`
  - **Ollama**: `ollama serve` running locally

### Option 1: One-click startup

**Windows:**
```bat
start.bat
```

**Linux / macOS:**
```bash
chmod +x start.sh
./start.sh
```

This will:
1. Create a Python virtual environment and install dependencies
2. Install frontend dependencies
3. Start backend on `http://localhost:8000`
4. Start frontend on `http://localhost:5173`
5. Open the browser automatically

### Option 2: Manual startup

**Terminal 1 — Backend:**
```bash
cd backend
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

**Terminal 2 — Frontend:**
```bash
cd frontend
npm install
npm run dev
```

### Option 3: Docker

```bash
docker compose up --build
```

Open **http://localhost:8000** (Docker serves the built frontend directly).

### Port conflicts

If default ports are taken, customize them:
```bat
set BACKEND_PORT=8001
set FRONTEND_PORT=5174
start.bat
```

---

## Interface Overview

```
┌──────────────────────────────────────────────────────────────┐
│  MultiAgent Studio  [📁 Project ▾]     [Roles] [Pipeline]   │
├──────────────────────────────────────────────────────────────┤
│  Pipeline:  ● Analysis ── ● Design ── ○ Impl ── ○ Review    │
├──────────────────────────────────────────────────────────────┤
│  [PM ●] [TD ●] [Dev ●] [Dev ●] [Reviewer ○] [+ Add Agent]  │
├────────────────────────────────┬─────────────────────────────┤
│  Output Stream                 │  Context Viewer             │
│  (real-time agent output)      │  (Markdown document)        │
│                                │                             │
│                                │  Usage: In/Out/Cost         │
├────────────────────────────────┤                             │
│  Chat: [To: Agent ▾] [msg]    │                             │
└────────────────────────────────┴─────────────────────────────┘
```

---

## Core Concepts

### Roles

A **role** defines an agent's capabilities:

| Field | Description |
|-------|-------------|
| `provider` | LLM provider: `claude`, `openai`, or `ollama` |
| `model` | Model name (provider-specific) |
| `tools` | Allowed tools: Read, Write, Edit, Bash, Glob, Grep |
| `system_prompt` | Instructions defining the role's behavior |
| `max_turns` | Max agentic turns (tool-use round trips) |

Default roles:

| Role | Provider | Model | Purpose |
|------|----------|-------|---------|
| **PM** | claude | sonnet | Requirement analysis, task breakdown |
| **TD** | claude | sonnet | Architecture design, tech decisions |
| **Developer** | claude | sonnet | Code implementation |
| **Reviewer** | claude | sonnet | Code review, security audit |

### Agents

An **agent** is a running instance of a role. Multiple agents can share the same role (e.g. two Developers in parallel). Each agent has its own context file, output log, usage tracking, and resumable session.

### Pipeline

A **pipeline** is a sequence of stages executed in order. Each stage has one or more agents. Agents within a parallel stage run concurrently.

Default pipeline:
```
Analysis (PM) → Design (TD) → Implementation (Dev ×2, parallel) → Review (Reviewer)
```

### Context Documents

Every agent has a Markdown file in `workspace/context/`. When an agent completes, its output is saved there. Downstream agents receive prior agents' context as part of their prompt.

---

## How To Use

### Switch project

Click the **project selector** in the header → enter the path to your project → click **Open**. Recent projects are remembered.

### Create an agent

Click **"+ Add Agent"** → select role → optionally set a custom ID → **Create**.

### Start an agent

Click the agent card → click ▶ → enter a prompt → **Ctrl+Enter** or click **Start**.

### Send a follow-up message

Use the **Chat Panel** at the bottom. Select target agent, type message, press **Enter**.

### Run a pipeline

Click **"Start Pipeline"** → enter requirement → customize stages → **Start Pipeline**.

### Edit roles

Click **"Roles"** → edit YAML in the modal → **Save**.

---

## Multi-Provider Configuration

### roles.yaml example

```yaml
roles:
  # Claude agent
  pm:
    provider: "claude"
    model: "sonnet"
    tools: [Read, Glob, Grep]

  # OpenAI agent
  developer-gpt:
    provider: "openai"
    model: "gpt-4o-mini"
    tools: [Read, Write, Edit, Bash, Glob, Grep]

  # Local Ollama agent
  reviewer-local:
    provider: "ollama"
    model: "qwen2.5-coder"
    tools: [Read, Glob, Grep]
```

### Provider differences

| Feature | Claude | OpenAI | Ollama |
|---------|--------|--------|--------|
| Agentic tools | Full (via Claude Code CLI) | Read/Write/Edit/Bash/Glob/Grep | Same as OpenAI |
| Session resume | Yes | No | No |
| Cost tracking | Exact (from SDK) | Estimated | Free |
| WebSearch | Yes | No | No |
| Auth | `claude login` or API key | API key | None (local) |

### Environment variables

| Variable | Provider | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Claude | API key (not needed if using `claude login`) |
| `OPENAI_API_KEY` | OpenAI | Required for OpenAI provider |
| `OLLAMA_HOST` | Ollama | Override Ollama URL (default: `http://localhost:11434`) |

---

## API Reference

Interactive docs at `http://localhost:8000/docs`.

### Project
| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/project` | Get current project path + recent list |
| `PUT` | `/api/project` | Switch project `{path}` |

### Roles
| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/roles` | List all roles |
| `GET` | `/api/config/roles` | Get raw roles.yaml |
| `PUT` | `/api/config/roles` | Update roles.yaml |

### Agents
| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/agents` | List all agents |
| `POST` | `/api/agents` | Create agent |
| `DELETE` | `/api/agents/{id}` | Delete agent |
| `POST` | `/api/agents/{id}/start` | Start agent |
| `POST` | `/api/agents/{id}/message` | Send message |
| `POST` | `/api/agents/{id}/stop` | Stop agent |
| `GET` | `/api/agents/{id}/context` | Get context doc |

### Pipeline
| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/pipeline/start` | Start pipeline |

### WebSocket (`ws://localhost:8000/ws`)
Events: `agent_status`, `agent_output`, `agent_usage`, `agent_error`, `context_update`, `pipeline_status`

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `claude-agent-sdk not installed` | `pip install claude-agent-sdk` |
| `OPENAI_API_KEY not set` | `export OPENAI_API_KEY=sk-...` |
| Ollama connection refused | Run `ollama serve` first |
| Port already in use | Set `BACKEND_PORT` / `FRONTEND_PORT` env vars |
| No output from agent | Check backend terminal for errors |
| Agent stuck in "Running" | Click stop button, check logs for rate limits |
