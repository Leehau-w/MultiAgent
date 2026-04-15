export type AgentStatus = 'idle' | 'running' | 'waiting' | 'completed' | 'error'

export interface AgentUsage {
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_creation_tokens: number
  cost_usd: number
}

export interface OutputEntry {
  timestamp: string
  type: 'text' | 'tool_use' | 'tool_result' | 'result' | 'error' | 'user'
  content: string
}

export interface AgentState {
  id: string
  role_id: string
  role_name: string
  status: AgentStatus
  session_id: string | null
  current_task: string | null
  output_log: OutputEntry[]
  usage: AgentUsage
  started_at: string | null
  finished_at: string | null
  context_file: string
}

export interface AgentRole {
  id: string
  name: string
  description: string
  system_prompt: string
  tools: string[]
  model: string
  effort: string | null
}

export interface WSEvent {
  type: 'agent_status' | 'agent_output' | 'agent_usage' | 'context_update' | 'agent_error' | 'pipeline_status'
  agent_id: string
  data: Record<string, unknown>
}
