import { create } from 'zustand'
import type { AgentState, AgentRole, AgentStatus, OutputEntry, PermissionRequest, WSEvent } from '../types'

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
  pipeline: PipelineState
  permissionQueue: PermissionRequest[]

  setRoles: (roles: Record<string, AgentRole>) => void
  setAgents: (agents: Record<string, AgentState>) => void
  selectAgent: (id: string | null) => void
  handleWSEvent: (event: WSEvent) => void
}

export const useAgentStore = create<AgentStore>((set) => ({
  agents: {},
  roles: {},
  selectedAgentId: null,
  contextCache: {},
  outputStreams: {},
  pipeline: { status: 'idle', requirement: '', stages: [], currentStage: 0 },
  permissionQueue: [],

  setRoles: (roles) => set({ roles }),

  setAgents: (agents) => {
    const streams: Record<string, OutputEntry[]> = {}
    for (const [id, agent] of Object.entries(agents)) {
      streams[id] = agent.output_log || []
    }
    set({ agents, outputStreams: streams })
  },

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
          const errEntry: OutputEntry = {
            timestamp: new Date().toISOString(),
            type: 'error',
            content: (data.error as string) || 'Unknown error',
          }
          const stream = state.outputStreams[agent_id] || []
          return {
            agents: {
              ...state.agents,
              [agent_id]: { ...agent, status: 'error' },
            },
            outputStreams: {
              ...state.outputStreams,
              [agent_id]: [...stream, errEntry],
            },
            permissionQueue: state.permissionQueue.filter(
              (p) => p.agent_id !== agent_id,
            ),
          }
        }

        default:
          return state
      }
    }),
}))
