export type AgentStatus = 'idle' | 'running' | 'waiting' | 'completed' | 'error'

export type PermissionMode = 'manual' | 'workspace' | 'bypass'

export interface AgentUsage {
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_creation_tokens: number
  cost_usd: number
}

export interface OutputEntry {
  timestamp: string
  type: 'text' | 'tool_use' | 'tool_result' | 'result' | 'error' | 'user' | 'permission'
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
  permission_mode: PermissionMode | null
}

export interface AgentRole {
  id: string
  name: string
  description: string
  system_prompt: string
  tools: string[]
  provider: string
  model: string
  max_turns: number
  effort: string | null
}

export interface PermissionRequest {
  request_id: string
  agent_id: string
  tool_name: string
  tool_input: Record<string, unknown>
  timestamp: string
}

export type ErrorCategory =
  | 'tool_error'
  | 'api_error'
  | 'auth_error'
  | 'config_error'
  | 'sdk_internal'

export interface ErrorInfo {
  id: string
  timestamp: string
  agent_id: string
  project_id: string
  category: ErrorCategory
  tool: string | null
  tool_input: Record<string, unknown> | null
  message: string
  stack: string | null
  recoverable: boolean
  retry_count: number
  final: boolean
}

export type WSEventType =
  | 'agent_status'
  | 'agent_output'
  | 'agent_usage'
  | 'context_update'
  | 'agent_error'
  | 'pipeline_status'
  | 'agent_permission_request'
  | 'agent_permission_resolved'
  | 'permission_mode'
  // v0.3.0 — stage-gate + coordinator dialog
  | 'stage_gate_review_started'
  | 'stage_gate_resolved'
  | 'pipeline_paused'
  | 'pipeline_resumed'
  | 'coordinator_notify_user'
  | 'coordinator_decision'

export type NotificationLevel = 'info' | 'warning' | 'blocker'

export interface CoordinatorNotification {
  id: string
  level: NotificationLevel
  message: string
  action_required: boolean
  timestamp: string
  dismissed: boolean
}

export interface CoordinatorDecision {
  scope: 'user_override' | 'coordinator'
  decision: string
  rationale: string
  timestamp: string
}

export interface WSEvent {
  type: WSEventType
  agent_id: string
  project_id?: string
  data: Record<string, unknown>
}

export interface ProjectMeta {
  id: string
  name: string
  project_dir: string
  created_at: string
}

export interface WorkflowStage {
  name: string
  agents: string[]
  parallel: boolean
  acceptance_criteria?: string | null
}

export interface WorkflowTrigger {
  on: string[]
  start: string[]
  context_from: string[]
  decide: string | null
}

export interface WorkflowCoordinator {
  enabled: boolean
  role_id: string
  allow_spawn: boolean
  max_spawned_agents: number
}

export interface WorkflowBudget {
  max_total_cost_usd: number | null
  max_total_turns: number | null
  max_wall_clock_min: number | null
  max_concurrent_agents: number | null
  max_stage_retries: number | null
}

export interface Workflow {
  version: number
  stages: WorkflowStage[]
  budget: WorkflowBudget | null
  coordinator: WorkflowCoordinator | null
  triggers: WorkflowTrigger[]
}
