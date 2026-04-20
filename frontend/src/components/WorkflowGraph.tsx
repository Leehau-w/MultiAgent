import { useMemo, useState } from 'react'
import { useAgentStore } from '../stores/agentStore'
import { apiDelete, apiPost } from '../utils/api'
import type { AgentState, Workflow, WorkflowStage, WorkflowTrigger } from '../types'
import AgentCard from './AgentCard'

interface Props {
  workflow: Workflow
}

interface Slot {
  stageIdx: number
  slotIdx: number
  roleId: string
  agent: AgentState | null
}

function matchAgents(
  workflow: Workflow,
  agents: Record<string, AgentState>,
): { slots: Slot[][]; coordinator: AgentState | null; unmatched: AgentState[] } {
  const pool: AgentState[] = Object.values(agents)
  const claimed = new Set<string>()

  const slots: Slot[][] = workflow.stages.map((stage, stageIdx) =>
    stage.agents.map((roleId, slotIdx) => {
      const agent =
        pool.find((a) => a.role_id === roleId && !claimed.has(a.id)) ?? null
      if (agent) claimed.add(agent.id)
      return { stageIdx, slotIdx, roleId, agent }
    }),
  )

  const coordRoleId = workflow.coordinator?.role_id ?? 'coordinator'
  const coordinator =
    pool.find((a) => a.role_id === coordRoleId && !claimed.has(a.id)) ?? null
  if (coordinator) claimed.add(coordinator.id)

  const unmatched = pool.filter((a) => !claimed.has(a.id))
  return { slots, coordinator, unmatched }
}

// Find a trigger whose target (its ``start`` list) contains *roleId*, so we
// can render the incoming trigger label on top of a stage. Not exact —
// triggers could fan into multiple stages — but close enough for a label.
function incomingTrigger(
  roleId: string,
  triggers: WorkflowTrigger[],
): WorkflowTrigger | null {
  for (const t of triggers) {
    if (t.start.includes(roleId)) return t
  }
  return null
}

function formatTriggerOn(trigger: WorkflowTrigger | null): string | null {
  if (!trigger || trigger.on.length === 0) return null
  if (trigger.on.length === 1) return `on ${trigger.on[0]}`
  const counts: Record<string, number> = {}
  for (const p of trigger.on) counts[p] = (counts[p] || 0) + 1
  const parts = Object.entries(counts).map(([p, c]) =>
    c > 1 ? `${c}× ${p}` : p,
  )
  return `on ${parts.join(' & ')}`
}

function PlaceholderCard({ roleId }: { roleId: string }) {
  const roles = useAgentStore((s) => s.roles)
  const role = roles[roleId]
  const name = role?.name ?? roleId
  return (
    <div
      className="w-[180px] rounded-lg border border-dashed border-gray-700 bg-gray-900/40
        px-3 py-2 flex flex-col gap-1 opacity-80"
      title={`No ${name} yet — will spawn when this stage runs`}
    >
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-gray-400 truncate">{name}</span>
        <span className="w-2 h-2 rounded-full bg-gray-700 shrink-0" />
      </div>
      <div className="text-[10px] text-gray-600 italic">pending</div>
      <div className="h-1.5 bg-gray-800 rounded-full" />
      <div className="text-[10px] text-gray-600">Not started</div>
    </div>
  )
}

function StageColumn({
  stage,
  slots,
  stageIdx,
  isActive,
  isComplete,
  isReviewing,
  isPaused,
  retries,
  maxRetries,
}: {
  stage: WorkflowStage
  slots: Slot[]
  stageIdx: number
  isActive: boolean
  isComplete: boolean
  isReviewing: boolean
  isPaused: boolean
  retries: number
  maxRetries: number | null
}) {
  // Visual state cascade: paused (red) > reviewing (amber) > active (indigo)
  // > complete (green) > pending (gray).
  const headerTone = isPaused
    ? 'text-rose-300'
    : isReviewing
      ? 'text-amber-300'
      : isComplete
        ? 'text-green-400'
        : isActive
          ? 'text-indigo-300'
          : 'text-gray-500'
  const marker = isPaused
    ? '\u25A0'
    : isReviewing
      ? '\u25C9'
      : isComplete
        ? '\u2713'
        : isActive
          ? '\u25CF'
          : '\u25CB'

  // Ring around the slot group for reviewing/paused — makes the stage
  // visually distinct even when the header is collapsed.
  const ringClass = isPaused
    ? 'ring-2 ring-rose-700/70 rounded-lg p-1 -m-1'
    : isReviewing
      ? 'ring-2 ring-amber-600/70 rounded-lg p-1 -m-1 animate-pulse'
      : ''

  const retryLabel = maxRetries != null ? `${retries}/${maxRetries}` : `${retries}`

  return (
    <div className="flex flex-col gap-2 shrink-0">
      <div className={`flex items-center gap-1.5 text-[11px] uppercase tracking-wider ${headerTone}`}>
        <span>{marker}</span>
        <span className="font-semibold">{stage.name}</span>
        {stage.parallel && (
          <span
            className="text-[9px] px-1 py-0.5 rounded bg-yellow-950/50 text-yellow-400 border border-yellow-900/70"
            title="Agents in this stage run in parallel"
          >
            parallel
          </span>
        )}
        {isReviewing && (
          <span
            className="text-[9px] px-1 py-0.5 rounded bg-amber-950/60 text-amber-200 border border-amber-800/70 animate-pulse"
            title="Coordinator is reviewing this stage"
          >
            gate review
          </span>
        )}
        {isPaused && (
          <span
            className="text-[9px] px-1 py-0.5 rounded bg-rose-950/70 text-rose-200 border border-rose-800/70"
            title="Pipeline paused; user action required"
          >
            paused
          </span>
        )}
        {retries > 0 && (
          <span
            className="text-[9px] px-1 py-0.5 rounded bg-amber-950/50 text-amber-300 border border-amber-900/70"
            title={
              maxRetries != null
                ? `${retries} rework cycle(s) used of ${maxRetries} allowed`
                : `${retries} rework cycle(s) so far`
            }
          >
            {retryLabel}
          </span>
        )}
        <span className="text-gray-600 text-[10px]">#{stageIdx + 1}</span>
      </div>
      <div className={ringClass}>
        <div className={stage.parallel ? 'flex gap-2' : 'flex flex-col gap-2'}>
          {slots.map((slot) =>
            slot.agent ? (
              <AgentCard key={`${stageIdx}-${slot.slotIdx}`} agent={slot.agent} />
            ) : (
              <PlaceholderCard
                key={`${stageIdx}-${slot.slotIdx}-ph`}
                roleId={slot.roleId}
              />
            ),
          )}
        </div>
      </div>
    </div>
  )
}

function Connector({ trigger }: { trigger: WorkflowTrigger | null }) {
  const label = formatTriggerOn(trigger)
  const isAndJoin = trigger && trigger.on.length > 1
  return (
    <div className="flex flex-col items-center justify-center pt-7 px-1 shrink-0">
      {label && (
        <div
          className={`text-[9px] mb-0.5 px-1.5 py-0.5 rounded border ${
            isAndJoin
              ? 'text-amber-300 border-amber-900/60 bg-amber-950/30'
              : 'text-gray-500 border-gray-800 bg-gray-900/40'
          }`}
          title={trigger?.on.join(' AND ')}
        >
          {label}
        </div>
      )}
      <div className="text-gray-600 text-xl leading-none">&#8594;</div>
    </div>
  )
}

function CoordinatorNode({
  agent,
  workflow,
}: {
  agent: AgentState | null
  workflow: Workflow
}) {
  const config = workflow.coordinator
  if (!config || !config.enabled) return null

  if (agent) {
    return (
      <div className="flex items-center gap-2">
        <span className="text-[10px] uppercase tracking-wider text-amber-400">
          coordinator
        </span>
        <AgentCard agent={agent} />
        {config.allow_spawn && (
          <span className="text-[9px] text-amber-500/80 italic">can spawn</span>
        )}
      </div>
    )
  }
  return (
    <div className="flex items-center gap-2">
      <span className="text-[10px] uppercase tracking-wider text-amber-400">
        coordinator
      </span>
      <PlaceholderCard roleId={config.role_id} />
      {config.allow_spawn && (
        <span className="text-[9px] text-amber-500/80 italic">can spawn</span>
      )}
    </div>
  )
}

function ClearAllButton() {
  const agents = useAgentStore((s) => s.agents)
  const clearAgents = useAgentStore((s) => s.clearAgents)
  const count = Object.keys(agents).length
  if (count === 0) return null
  const handleClick = async () => {
    if (!confirm(`Delete all ${count} agent${count === 1 ? '' : 's'}?`)) return
    const res = await apiDelete('/api/agents')
    if (res.ok) clearAgents()
  }
  return (
    <button
      onClick={handleClick}
      className="text-[11px] px-2 py-1 text-rose-300/80 border border-rose-900/60
        hover:border-rose-700 hover:text-rose-200 rounded transition-colors"
      title={`Stop + delete all ${count} agents`}
    >
      Clear all
    </button>
  )
}

function AdHocRow({
  agents,
  onAdd,
}: {
  agents: AgentState[]
  onAdd: () => void
}) {
  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span className="text-[10px] uppercase tracking-wider text-gray-500 mr-1">
        Ad-hoc
      </span>
      {agents.map((a) => (
        <AgentCard key={a.id} agent={a} />
      ))}
      <button
        onClick={onAdd}
        className="h-[108px] w-[180px] border border-dashed border-gray-700 rounded-lg
          flex items-center justify-center text-gray-500 hover:text-gray-300
          hover:border-gray-500 transition-colors text-sm"
      >
        + Add Agent
      </button>
      <ClearAllButton />
    </div>
  )
}

function AddAgentBubble({
  onCancel,
  onCreated,
}: {
  onCancel: () => void
  onCreated: (agent: AgentState) => void
}) {
  const roles = useAgentStore((s) => s.roles)
  const [roleId, setRoleId] = useState('')
  const [agentId, setAgentId] = useState('')

  const handleCreate = async () => {
    if (!roleId) return
    const body: Record<string, string> = { role_id: roleId }
    if (agentId.trim()) body.agent_id = agentId.trim()
    const res = await apiPost('/api/agents', body)
    if (res.ok) {
      const agent = (await res.json()) as AgentState
      onCreated(agent)
    }
  }

  return (
    <div className="h-[108px] flex items-center gap-2 px-3 bg-gray-900 rounded-lg border border-gray-700">
      <select
        value={roleId}
        onChange={(e) => setRoleId(e.target.value)}
        className="bg-gray-800 text-sm rounded px-2 py-1 border border-gray-700 text-gray-200"
      >
        <option value="">Select role...</option>
        {Object.entries(roles).map(([id, role]) => (
          <option key={id} value={id}>
            {role.name}
          </option>
        ))}
      </select>
      <input
        placeholder="ID (optional)"
        value={agentId}
        onChange={(e) => setAgentId(e.target.value)}
        className="bg-gray-800 text-sm rounded px-2 py-1 border border-gray-700 w-24 text-gray-200"
      />
      <button
        onClick={handleCreate}
        className="text-sm px-2 py-1 bg-indigo-600 hover:bg-indigo-500 rounded text-white"
      >
        Create
      </button>
      <button
        onClick={onCancel}
        className="text-gray-500 hover:text-gray-300 text-sm"
      >
        Cancel
      </button>
    </div>
  )
}

export default function WorkflowGraph({ workflow }: Props) {
  const agents = useAgentStore((s) => s.agents)
  const pipeline = useAgentStore((s) => s.pipeline)
  const [showCreate, setShowCreate] = useState(false)

  const { slots, coordinator, unmatched } = useMemo(
    () => matchAgents(workflow, agents),
    [workflow, agents],
  )

  const onCreated = (agent: AgentState) => {
    useAgentStore.setState((s) => ({
      agents: { ...s.agents, [agent.id]: agent },
    }))
    setShowCreate(false)
  }

  const hasStages = workflow.stages.length > 0

  return (
    <div className="px-6 py-3 border-b border-gray-800 shrink-0 space-y-3 overflow-x-auto">
      {/* Coordinator row — floats on top so the graph below is not shifted */}
      {workflow.coordinator?.enabled && (
        <div className="flex justify-end">
          <CoordinatorNode agent={coordinator} workflow={workflow} />
        </div>
      )}

      {/* Stage columns */}
      {hasStages && (
        <div className="flex items-start gap-1">
          {workflow.stages.map((stage, i) => {
            const isComplete =
              pipeline.status !== 'idle' && i < pipeline.currentStage
            const isActive =
              pipeline.status === 'running' && i === pipeline.currentStage
            // Stage-gate states drive the amber/red rings.
            const isReviewing = pipeline.stageReviewing === stage.name
            const isPaused =
              pipeline.status === 'paused' && pipeline.stageReviewing === stage.name
            const retries = pipeline.stageRetries[stage.name] ?? 0
            const maxRetries = workflow.budget?.max_stage_retries ?? 3
            const trigger =
              i > 0 ? incomingTrigger(stage.agents[0], workflow.triggers) : null
            return (
              <div key={i} className="flex items-start">
                {i > 0 && <Connector trigger={trigger} />}
                <StageColumn
                  stage={stage}
                  slots={slots[i]}
                  stageIdx={i}
                  isActive={isActive}
                  isComplete={isComplete}
                  isReviewing={isReviewing}
                  isPaused={isPaused}
                  retries={retries}
                  maxRetries={maxRetries}
                />
              </div>
            )
          })}
        </div>
      )}

      {/* Ad-hoc / Add Agent row */}
      {showCreate ? (
        <AddAgentBubble
          onCancel={() => setShowCreate(false)}
          onCreated={onCreated}
        />
      ) : (
        <AdHocRow
          agents={unmatched}
          onAdd={() => setShowCreate(true)}
        />
      )}
    </div>
  )
}
