# MultiAgent Studio — 用户指南

## 简介

MultiAgent Studio 是一个可视化的多智能体协作平台。你可以创建不同角色的 AI Agent（产品经理、技术总监、开发工程师、代码审查员），通过流水线协调它们完成复杂的软件工程任务。Agent 之间通过 Markdown 文档共享上下文。

支持 **Claude**、**OpenAI** 和 **Ollama**（本地模型）——可以在同一个流水线里混合使用不同供应商。

---

## 快速开始

### 环境要求

- **Python 3.10+**
- **Node.js 18+**
- 至少配置一个 LLM 供应商：
  - **Claude**: 运行 `claude login`（Max 订阅）或设置 `ANTHROPIC_API_KEY`
  - **OpenAI**: 设置 `OPENAI_API_KEY`
  - **Ollama**: 本地运行 `ollama serve`

### 方式一：一键启动

**Windows：**
```bat
start.bat
```

**Linux / macOS：**
```bash
chmod +x start.sh
./start.sh
```

脚本会自动：
1. 创建 Python 虚拟环境并安装后端依赖
2. 安装前端依赖
3. 启动后端 `http://localhost:8000`
4. 启动前端 `http://localhost:5173`
5. 自动打开浏览器

### 方式二：手动启动

**终端 1 — 后端：**
```bash
cd backend
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

**终端 2 — 前端：**
```bash
cd frontend
npm install
npm run dev
```

### 方式三：Docker

```bash
docker compose up --build
```

访问 **http://localhost:8000**。

### 端口冲突

如果默认端口被占用：
```bat
set BACKEND_PORT=8001
set FRONTEND_PORT=5174
start.bat
```

---

## 界面概览

```
┌──────────────────────────────────────────────────────────────┐
│  MultiAgent Studio  [📁 项目 ▾]           [Roles] [Pipeline]│
├──────────────────────────────────────────────────────────────┤
│  流水线: ● 分析 ── ● 设计 ── ○ 实现 ── ○ 审查              │
├──────────────────────────────────────────────────────────────┤
│  [PM ●] [TD ●] [Dev ●] [Dev ●] [Reviewer ○] [+ 添加Agent]  │
├─────────────────────────────┬────────────────────────────────┤
│  输出流                      │  上下文查看器                  │
│  (实时 Agent 输出)           │  (Markdown 文档)              │
│                              │                               │
│                              │  用量：输入 / 输出 / 费用      │
├─────────────────────────────┤                                │
│  聊天: [发给: Agent ▾] [消息] │                               │
└─────────────────────────────┴────────────────────────────────┘
```

---

## 核心概念

### 角色（Role）

角色定义了 Agent 的能力：

| 字段 | 说明 |
|------|------|
| `provider` | LLM 供应商：`claude`、`openai` 或 `ollama` |
| `model` | 模型名称（根据供应商不同而不同） |
| `tools` | 允许的工具：Read、Write、Edit、Bash、Glob、Grep |
| `system_prompt` | 定义角色行为的系统提示词 |
| `max_turns` | 最大工具调用轮次 |

默认角色：

| 角色 | 供应商 | 模型 | 职责 |
|------|--------|------|------|
| **PM**（产品经理） | claude | sonnet | 需求分析、任务拆解 |
| **TD**（技术总监） | claude | sonnet | 架构设计、技术决策 |
| **Developer**（开发） | claude | sonnet | 代码实现 |
| **Reviewer**（审查） | claude | sonnet | 代码审查、安全检查 |

### 智能体（Agent）

Agent 是角色的运行实例。同一角色可以创建多个 Agent（比如两个 Developer 并行工作）。每个 Agent 有独立的上下文文件、输出日志、用量统计和可恢复的会话。

### 流水线（Pipeline）

流水线是按顺序执行的多个阶段。每个阶段包含一个或多个 Agent。标记为并行的阶段内，Agent 同时执行。

默认流水线：
```
分析 (PM) → 设计 (TD) → 实现 (Dev ×2, 并行) → 审查 (Reviewer)
```

### 上下文文档

每个 Agent 在 `workspace/context/` 下有一个 Markdown 文件。任务完成后，输出保存到该文件。下游 Agent 会收到上游 Agent 的上下文文件作为 prompt 的一部分。

---

## 操作指南

### 切换项目

点击 Header 里的**项目选择器** → 输入目标项目路径 → **Open**。最近打开的项目会自动记住。

### 创建 Agent

点 **"+ Add Agent"** → 选角色 → 可选填自定义 ID → **Create**。

### 启动 Agent

点 Agent 卡片选中 → 点绿色 ▶ 按钮 → 输入 prompt → **Ctrl+Enter** 启动。

### 发送追问消息

底部**聊天面板** → 选择目标 Agent → 输入消息 → **Enter** 发送。

### 运行流水线

点 **"Start Pipeline"** → 输入需求描述 → 自定义阶段 → **Start Pipeline**。

### 编辑角色

点 **"Roles"** → 在 YAML 编辑器中修改 → **Save**。

---

## 多供应商配置

### roles.yaml 示例

```yaml
roles:
  # Claude 智能体
  pm:
    provider: "claude"
    model: "sonnet"
    tools: [Read, Glob, Grep]

  # OpenAI 智能体
  developer-gpt:
    provider: "openai"
    model: "gpt-4o-mini"
    tools: [Read, Write, Edit, Bash, Glob, Grep]

  # 本地 Ollama 智能体
  reviewer-local:
    provider: "ollama"
    model: "qwen2.5-coder"
    tools: [Read, Glob, Grep]
```

### 供应商能力对比

| 功能 | Claude | OpenAI | Ollama |
|------|--------|--------|--------|
| 工具调用 | 完整（通过 Claude Code CLI） | Read/Write/Edit/Bash/Glob/Grep | 同 OpenAI |
| 会话恢复 | 支持 | 不支持 | 不支持 |
| 费用追踪 | 精确（SDK 报告） | 估算 | 免费 |
| 网页搜索 | 支持 | 不支持 | 不支持 |
| 认证方式 | `claude login` 或 API key | API key | 无需认证（本地） |

### 环境变量

| 变量 | 供应商 | 说明 |
|------|--------|------|
| `ANTHROPIC_API_KEY` | Claude | API 密钥（使用 `claude login` 时不需要） |
| `OPENAI_API_KEY` | OpenAI | OpenAI API 密钥 |
| `OLLAMA_HOST` | Ollama | Ollama 地址（默认 `http://localhost:11434`） |

---

## API 参考

交互式文档：`http://localhost:8000/docs`

### 项目
| 方法 | 端点 | 说明 |
|------|------|------|
| `GET` | `/api/project` | 获取当前项目路径 + 最近列表 |
| `PUT` | `/api/project` | 切换项目 `{path}` |

### 角色
| 方法 | 端点 | 说明 |
|------|------|------|
| `GET` | `/api/roles` | 列出所有角色 |
| `GET` | `/api/config/roles` | 获取 roles.yaml 原始内容 |
| `PUT` | `/api/config/roles` | 更新 roles.yaml |

### 智能体
| 方法 | 端点 | 说明 |
|------|------|------|
| `GET` | `/api/agents` | 列出所有 Agent |
| `POST` | `/api/agents` | 创建 Agent |
| `DELETE` | `/api/agents/{id}` | 删除 Agent |
| `POST` | `/api/agents/{id}/start` | 启动 Agent |
| `POST` | `/api/agents/{id}/message` | 发送消息 |
| `POST` | `/api/agents/{id}/stop` | 停止 Agent |
| `GET` | `/api/agents/{id}/context` | 获取上下文文档 |

### 流水线
| 方法 | 端点 | 说明 |
|------|------|------|
| `POST` | `/api/pipeline/start` | 启动流水线 |

### WebSocket (`ws://localhost:8000/ws`)
事件类型：`agent_status`、`agent_output`、`agent_usage`、`agent_error`、`context_update`、`pipeline_status`

---

## 常见问题

| 问题 | 解决方案 |
|------|---------|
| `claude-agent-sdk not installed` | 运行 `pip install claude-agent-sdk` |
| `OPENAI_API_KEY not set` | 设置环境变量 `export OPENAI_API_KEY=sk-...` |
| Ollama 连接失败 | 先运行 `ollama serve` |
| 端口被占用 | 设置 `BACKEND_PORT` / `FRONTEND_PORT` 环境变量 |
| Agent 无输出 | 检查后端终端的错误日志 |
| Agent 卡在 "Running" | 点停止按钮，检查是否触发了速率限制 |
