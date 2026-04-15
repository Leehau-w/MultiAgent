from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AgentStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    ERROR = "error"


class AgentRole(BaseModel):
    id: str
    name: str
    description: str
    system_prompt: str
    tools: list[str] = Field(default_factory=list)
    provider: str = "claude"
    model: str = "sonnet"
    max_turns: int = 30
    effort: str | None = None


class AgentUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0


class AgentState(BaseModel):
    id: str
    role_id: str
    role_name: str
    status: AgentStatus = AgentStatus.IDLE
    session_id: str | None = None
    current_task: str | None = None
    output_log: list[OutputEntry] = Field(default_factory=list)
    usage: AgentUsage = Field(default_factory=AgentUsage)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    context_file: str = ""


class OutputEntry(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.now)
    type: str = "text"  # text | tool_use | tool_result | error
    content: str = ""


class WSEvent(BaseModel):
    type: str  # agent_status | agent_output | agent_usage | context_update | agent_error
    agent_id: str
    data: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    agent_id: str
    content: str
    role: str = "user"
    timestamp: datetime = Field(default_factory=datetime.now)


# --- Request / Response models ---


class CreateAgentRequest(BaseModel):
    role_id: str
    agent_id: str | None = None


class StartAgentRequest(BaseModel):
    prompt: str
    context_from: list[str] | None = None


class SendMessageRequest(BaseModel):
    content: str


class StartPipelineRequest(BaseModel):
    requirement: str
    stages: list[PipelineStage] | None = None


class PipelineStage(BaseModel):
    name: str
    agents: list[str]  # role ids
    parallel: bool = False


# Rebuild forward refs so AgentState can use OutputEntry
AgentState.model_rebuild()
