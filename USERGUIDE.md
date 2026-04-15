# MultiAgent Studio — User Guide

## What is MultiAgent Studio?

MultiAgent Studio is a visual platform for orchestrating multiple Claude AI agents. Each agent takes on a specific role (Product Manager, Technical Director, Developer, Code Reviewer) and works on a shared project. Agents share context through Markdown documents, enabling a multi-role collaborative workflow to tackle complex software engineering tasks.

---

## Quick Start

### Prerequisites

- **Python 3.10+** (backend)
- **Node.js 18+** (frontend)
- **Anthropic API key** — set `ANTHROPIC_API_KEY` in your environment
- **Claude Code CLI** — bundled with `claude-agent-sdk`, or install separately

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
1. Create a Python virtual environment and install backend dependencies
2. Install frontend Node dependencies
3. Start the backend on `http://localhost:8000`
4. Start the frontend on `http://localhost:5173`

Open **http://localhost:5173** in your browser.

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
# Set your API key
export ANTHROPIC_API_KEY=sk-ant-...

# Build and run
docker compose up --build
```

Open **http://localhost:8000** (Docker serves the built frontend directly).

---

## Interface Overview

```
┌──────────────────────────────────────────────────────────┐
│  Header              [Roles]  [Start Pipeline]           │
├──────────────────────────────────────────────────────────┤
│  Pipeline Progress   [Analysis] ── [Design] ── [Impl]   │
├──────────────────────────────────────────────────────────┤
│  Agent Cards   [PM ●] [TD ●] [Dev ●] [+ Add Agent]     │
├────────────────────────────────┬─────────────────────────┤
│  Output Stream                 │  Context Viewer         │
│  (real-time agent output)      │  (Markdown document)    │
│                                │                         │
│                                │  Usage Stats            │
│                                │  Input / Output / Cost  │
├────────────────────────────────┤                         │
│  Chat Panel                    │                         │
│  [To: Agent ▼] [message...   ] │                         │
└────────────────────────────────┴─────────────────────────┘
```

---

## Core Concepts

### Roles

A **role** defines what an agent can do. Each role has:

| Field | Description |
|-------|-------------|
| `name` | Display name (e.g. "Product Manager") |
| `model` | Claude model to use (`sonnet`, `opus`, `haiku`) |
| `tools` | List of allowed tools (Read, Write, Edit, Bash, Glob, Grep, WebSearch) |
| `system_prompt` | Instructions that define the role's behavior |
| `max_turns` | Maximum number of agentic turns (tool-use round trips) |

The system ships with 4 default roles:

| Role | Model | Tools | Purpose |
|------|-------|-------|---------|
| **PM** (Product Manager) | sonnet | Read, Glob, Grep, WebSearch | Requirement analysis, task breakdown |
| **TD** (Technical Director) | sonnet | Read, Glob, Grep, Bash | Architecture design, tech decisions |
| **Developer** | sonnet | Read, Write, Edit, Bash, Glob, Grep | Code implementation |
| **Reviewer** | sonnet | Read, Glob, Grep | Code review, security audit |

### Agents

An **agent** is a running instance of a role. You can create multiple agents from the same role (e.g. two Developers working in parallel). Each agent has:

- A unique ID
- Its own context Markdown file
- Independent output log and usage tracking
- A session that can be resumed

### Pipeline

A **pipeline** is a sequence of stages. Each stage contains one or more agents. Stages run sequentially; agents within a parallel stage run concurrently. The default pipeline:

```
Analysis (PM) → Design (TD) → Implementation (Dev x2, parallel) → Review (Reviewer)
```

### Context Documents

Every agent has a Markdown file in `workspace/context/`. When an agent completes a task, its output is saved to this file. Downstream agents receive prior agents' context files as part of their prompt, creating an information chain.

---

## How To Use

### Create an agent manually

1. Click **"+ Add Agent"** in the agent cards row
2. Select a role from the dropdown
3. Optionally provide a custom ID
4. Click **Create**

### Start an agent

1. Click an agent card to select it
2. Click the green **play button** (▶) on the card
3. Enter a task prompt in the modal (e.g. "Analyze the requirements for a TODO app")
4. Press **Ctrl+Enter** or click **Start**

The agent will begin working. You'll see:
- The status indicator turns green and pulses
- Real-time output appears in the **Output Stream**
- Token count and cost update live on the card
- When finished, the context document updates in the **Context Viewer**

### Send a message to a running agent

Use the **Chat Panel** at the bottom:
1. Select the target agent from the dropdown
2. Type your message
3. Press **Enter** or click **Send**

If the agent is running, the message is queued. If idle with a prior session, it resumes the conversation.

### Run a pipeline

1. Click **"Start Pipeline"** in the header
2. Enter your project requirement in the text area
3. Review/customize the stages:
   - Rename stages by editing the name field
   - Toggle **Parallel** to run agents within a stage concurrently
   - Click **"+ agent"** to add more agents to a stage
   - Click **"+ Add Stage"** to append a new stage
   - Remove stages or agents with the **×** button
4. Click **Start Pipeline**

The **Pipeline Progress** bar appears below the header, showing which stage is currently running.

### View context documents

1. Click an agent card to select it
2. The **Context Viewer** panel on the right shows that agent's Markdown document
3. Click **"View All"** to see all agents' context documents side by side
4. The context updates in real-time as agents complete their work

### Edit roles

1. Click **"Roles"** in the header
2. The YAML editor opens with the current `roles.yaml` content
3. Edit the configuration (add roles, change models, modify prompts, adjust tools)
4. Click **Save** — roles reload immediately

Example: adding a new QA role:

```yaml
  qa:
    name: "QA Engineer"
    description: "Test planning and quality assurance"
    model: "sonnet"
    max_turns: 20
    tools:
      - Read
      - Glob
      - Grep
      - Bash
    system_prompt: |
      You are a senior QA engineer. Write test plans,
      identify edge cases, and verify acceptance criteria.
```

### Stop an agent

Click the yellow **stop button** (■) on a running agent's card.

### Delete an agent

Click the red **× button** on the agent card. This removes the agent and its context file.

---

## Configuration Reference

### roles.yaml

Located at `backend/config/roles.yaml`. Full schema:

```yaml
roles:
  <role_id>:                    # Unique identifier (used in API and pipeline)
    name: "Display Name"
    description: "What this role does"
    model: "sonnet"             # sonnet | opus | haiku
    max_turns: 30               # Max agentic turns before stopping
    tools:                      # Tools this agent can use
      - Read
      - Write
      - Edit
      - Bash
      - Glob
      - Grep
      - WebSearch
    system_prompt: |
      Multi-line system prompt that defines
      the agent's behavior and output format.
```

### Available tools

| Tool | Description |
|------|-------------|
| `Read` | Read files from the filesystem |
| `Write` | Create new files |
| `Edit` | Modify existing files |
| `Bash` | Execute shell commands |
| `Glob` | Find files by pattern |
| `Grep` | Search file contents |
| `WebSearch` | Search the web |

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Your Anthropic API key |

---

## API Reference

The backend exposes a REST API at `http://localhost:8000`. Interactive docs are available at `/docs` (Swagger UI).

### Roles

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/roles` | List all roles |
| `GET` | `/api/config/roles` | Get raw roles.yaml content |
| `PUT` | `/api/config/roles` | Update roles.yaml content |

### Agents

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/agents` | List all agents |
| `POST` | `/api/agents` | Create agent `{role_id, agent_id?}` |
| `DELETE` | `/api/agents/{id}` | Delete agent |
| `POST` | `/api/agents/{id}/start` | Start agent `{prompt, context_from?}` |
| `POST` | `/api/agents/{id}/message` | Send message `{content}` |
| `POST` | `/api/agents/{id}/stop` | Stop agent |
| `GET` | `/api/agents/{id}/context` | Get context document |

### Pipeline

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/pipeline/start` | Start pipeline `{requirement, stages?}` |

### WebSocket

Connect to `ws://localhost:8000/ws` to receive real-time events:

| Event Type | Description |
|-----------|-------------|
| `agent_status` | Agent state changes (idle/running/completed/error) |
| `agent_output` | Text, tool use, or result output from an agent |
| `agent_usage` | Token count and cost updates |
| `agent_error` | Agent error messages |
| `context_update` | Context document changes |
| `pipeline_status` | Pipeline stage progress |

---

## Typical Workflows

### Workflow 1: Full pipeline for a new feature

1. Click **Start Pipeline**
2. Enter: "Build a REST API for a todo list app with user authentication, CRUD operations, and SQLite storage"
3. Keep default stages → click **Start Pipeline**
4. Watch as:
   - PM analyzes requirements and creates a task breakdown
   - TD designs architecture and selects tech stack
   - Two Developers implement code in parallel
   - Reviewer checks code quality and security
5. Check the **Context Viewer** for each agent's output

### Workflow 2: Single-agent task

1. Click **+ Add Agent** → select "Senior Developer" → **Create**
2. Click the agent card → press ▶
3. Enter: "Read the existing codebase and add input validation to all API endpoints"
4. Monitor progress in the output stream

### Workflow 3: Iterative collaboration

1. Create a PM agent and a Developer agent
2. Start PM with a high-level requirement
3. After PM completes, start Developer with: "Implement the tasks from the PM's analysis"
   - Check "context_from" includes the PM agent ID (auto-populated)
4. Send follow-up messages to refine the implementation

---

## Troubleshooting

### "claude-agent-sdk not installed"

```bash
pip install claude-agent-sdk
```

The SDK bundles the Claude Code CLI. Make sure `ANTHROPIC_API_KEY` is set in your environment.

### Frontend shows "No output yet"

- Check that the backend is running on port 8000
- Check the browser console for WebSocket connection errors
- Verify the Vite proxy config in `frontend/vite.config.ts` points to `localhost:8000`

### Agent stuck in "Running"

An agent may appear stuck if:
- It's waiting for tool approval (check backend logs)
- It hit a rate limit (wait and retry)
- The `max_turns` limit was reached

Click the **stop button** to cancel it.

### Pipeline doesn't start

- Ensure all role IDs in the stage config exist in `roles.yaml`
- Check backend logs (`uvicorn` terminal) for error details

### Toast shows "Network error"

The backend is not reachable. Verify it's running and the port is correct.
