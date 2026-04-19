import { create } from 'zustand'
import type { AgentState, AgentRole, AgentStatus, ErrorInfo, OutputEntry, PermissionMode, PermissionRequest, WSEvent } from '../types'

export interface PipelineStage {
  name: string
  agents: string[]
  parallel: boolean
}

export interface PipelineState {
  status: 'idle' | 'running' | 'completed'
  requirement: string
  stages: PipelineStage[]
  currentStage: number
}

interface AgentStore {
  agents: Record<string, AgentState>
  roles: Record<string, AgentRole>
  selectedAgentId: string | null
  contextCache: Record<string, string>
  outputStreams: Record<string, OutputEntry[]>
  errors: ErrorInfo[]
  pipeline: PipelineState
  permissionQueue: PermissionRequest[]
  globalPermissionMode: PermissionMode

  setRoles: (roles: Record<string, AgentRole>) => void
  setAgents: (agents: Record<string, AgentState>) => void
  setErrors: (errors: ErrorInfo[]) => void
  clearErrors: () => void
  selectAgent: (id: string | null) => void
  setGlobalPermissionMode: (mode: PermissionMode) => void
  setAgentPermissionMode: (agentId: string, mode: PermissionMode | null) => void
  handleWSEvent: (event: WSEvent) => void
}

export const useAgentStore = create<AgentStore>((set) => ({
  agents: {},
  roles: {},
  selectedAgentId: null,
  contextCache: {},
  outputStreams: {},
  errors: [],
  pipeline: { status: 'idle', requirement: '', stages: [], currentStage: 0 },
  permissionQueue: [],
  globalPermissionMode: 'manual',

  setRoles: (roles) => set({ roles }),

  setErrors: (errors) => set({ errors }),

  clearErrors: () => set({ errors: [] }),

  setGlobalPermissionMode: (mode) => set({ globalPermissionMode: mode }),

  setAgentPermissionMode: (agentId, mode) =>
    set((state) => {
      const agent = state.agents[agentId]
      if (!agent) return state
      return {
        agents: {
          ...state.agents,
          [agentId]: { ...agent, permission_mode: mode },
        },
      }
    }),

  setAgents: (agents) =>
    set((state) => {
      // Merge streams: seed from output_log only if we don't already have
      // a live stream from the WebSocket. A naive replace would wipe
      // accumulated realtime output on any refresh.
      const streams = { ...state.outputStreams }
      for (const [id, agent] of Object.entries(agents)) {
        if (!streams[id] || streams[id].length === 0) {
          streams[id] = agent.output_log || []
        }
      }
      for (const id of Object.keys(streams)) {
        if (!(id in agents)) delete streams[id]
      }
      return { agents, outputStreams: streams }
    }),

  selectAgent: (id) => set({ selectedAgentId: id }),

  handleWSEvent: (event) =>
    set((state) => {
      const { type, agent_id, data } = event

      // Permission request events — dedupe by request_id so a retransmit
      // from the backend never stacks two cards for the same tool call.
      if (type === 'agent_permission_request') {
        const requestId = data.request_id as string
        if (state.permissionQueue.some((p) => p.request_id === requestId)) {
          return state
        }
        const req: PermissionRequest = {
          request_id: requestId,
          agent_id,
          tool_name: data.tool_name as string,
          tool_input: (data.tool_input as Record<string, unknown>) || {},
          timestamp: new Date().toISOString(),
        }
        return { permissionQueue: [...state.permissionQueue, req] }
      }

      if (type === 'agent_permission_resolved') {
        const requestId = data.request_id as string
        return {
          permissionQueue: state.permissionQueue.filter(
            (p) => p.request_id !== requestId,
          ),
        }
      }

      // Permission mode updates — scope=global flips the default;
      // scope=agent writes the per-agent override (null = inherit).
      if (type === 'permission_mode') {
        const scope = data.scope as 'global' | 'agent'
        const mode = (data.mode as PermissionMode | null) ?? null
        if (scope === 'global') {
          return { globalPermissionMode: (mode || 'manual') as PermissionMode }
        }
        const aid = (data.agent_id as string) || agent_id
        const agent = state.agents[aid]
        if (!agent) return state
        return {
          agents: {
            ...state.agents,
            [aid]: { ...agent, permission_mode: mode },
          },
        }
      }

      // Pipeline-level events (no agent_id)
      if (type === 'pipeline_status') {
        return {
          pipeline: {
            ...state.pipeline,
            status: (data.status as PipelineState['status']) || state.pipeline.status,
            requirement: (data.requirement as string) ?? state.pipeline.requirement,
            stages: (data.stages as PipelineStage[]) ?? state.pipeline.stages,
            currentStage: (data.current_stage as number) ?? state.pipeline.currentStage,
          },
        }
      }

      switch (type) {
        case 'agent_status': {
          const agent = state.agents[agent_id]
          if (!agent) return state
          const nextStatus = (data.status as AgentStatus) || agent.status
          // If the agent left the running state, drop its pending permission
          // cards as a safety net in case a resolved event was missed.
          const clearPermissions =
            nextStatus !== 'running' && nextStatus !== 'waiting'
          return {
            agents: {
              ...state.agents,
              [agent_id]: {
                ...agent,
                status: nextStatus,
                current_task: (data.currentTask as string) ?? agent.current_task,
                session_id: (data.sessionId as string) ?? agent.session_id,
                started_at: (data.startedAt as string) ?? agent.started_at,
                finished_at: (data.finishedAt as string) ?? agent.finished_at,
              },
            },
            permissionQueue: clearPermissions
              ? state.permissionQueue.filter((p) => p.agent_id !== agent_id)
              : state.permissionQueue,
          }
        }

        case 'agent_output': {
          const entry: OutputEntry = {
            timestamp: (data.timestamp as string) || new Date().toISOString(),
            type: (data.type as OutputEntry['type']) || 'text',
            content: (data.text as string) || '',
          }
          const existing = state.outputStreams[agent_id] || []
          return {
            outputStreams: {
              ...state.outputStreams,
              [agent_id]: [...existing, entry],
            },
          }
        }

        case 'agent_usage': {
          const agent = state.agents[agent_id]
          if (!agent) return state
          return {
            agents: {
              ...state.agents,
              [agent_id]: {
                ...agent,
                usage: {
                  input_tokens: (data.input_tokens as number) ?? 0,
                  output_tokens: (data.output_tokens as number) ?? 0,
                  cache_read_tokens: (data.cache_read_tokens as number) ?? 0,
                  cache_creation_tokens: (data.cache_creation_tokens as number) ?? 0,
                  cost_usd: (data.cost_usd as number) ?? 0,
                },
              },
            },
          }
        }

        case 'context_update': {
          return {
            contextCache: {
              ...state.contextCache,
              [agent_id]: (data.content as string) || '',
            },
          }
        }

        case 'agent_error': {
          const agent = state.agents[agent_id]
          if (!agent) return state

          // Two payload shapes:
          //  * v0.2.0 ErrorInfo — { id, category, message, final, retry_count, ... }
          //  * Legacy { error: string } — keep working for older backends
          const isInfo =
            typeof data.id === 'string' && typeof data.category === 'string'

          const message = isInfo
            ? (data.message as string) || 'Unknown error'
            : (data.error as string) || 'Unknown error'
          const isFinal = isInfo ? (data.final as boolean) ?? true : true

          const errEntry: OutputEntry = {
            timestamp: new Date().toISOString(),
            type: 'error',
            content: isInfo
              ? `[${data.category as string}] ${message}${
                  !isFinal ? ` (retry ${(data.retry_count as number) + 1})` : ''
                }`
              : message,
          }
          const stream = state.outputStreams[agent_id] || []

          // Transient retries keep the agent in 'running' — only final
          // errors flip the card to red.
          const nextAgent: AgentState = isFinal
            ? { ...agent, status: 'error' }
            : agent

          const newError: ErrorInfo | null = isInfo
            ? {
                id: data.id as string,
                timestamp: (data.timestamp as string) || new Date().toISOString(),
                agent_id,
                project_id: (data.project_id as string) || '',
                category: data.category as ErrorInfo['category'],
                tool: (data.tool as string | null) ?? null,
                tool_input: (data.tool_input as Record<string, unknown> | null) ?? null,
                message,
                stack: (data.stack as string | null) ?? null,
                recoverable: (data.recoverable as boolean) ?? false,
                retry_count: (data.retry_count as number) ?? 0,
                final: isFinal,
              }
            : null
          // Cap the errors list so a runaway agent can't balloon state.
          const nextErrors = newError
            ? [...state.errors, newError].slice(-200)
            : state.errors

          return {
            agents: {
              ...state.agents,
              [agent_id]: nextAgent,
            },
            outputStreams: {
              ...state.outputStreams,
              [agent_id]: [...stream, errEntry],
            },
            errors: nextErrors,
            permissionQueue: isFinal
              ? state.permissionQueue.filter((p) => p.agent_id !== agent_id)
              : state.permissionQueue,
          }
        }

        default:
          return state
      }
    }),
}))
