import { useEffect, useMemo, useState } from 'react'
import { useAgentStore } from '../stores/agentStore'
import { apiFetch, apiPost } from '../utils/api'

interface BudgetSnapshot {
  caps: {
    max_total_cost_usd?: number
    max_total_turns?: number
    max_wall_clock_min?: number
    max_concurrent_agents?: number
  }
  usage: {
    cost_usd: number
    turns: number
    wall_clock_min: number
    concurrent: number
    concurrent_peak: number
  }
  exceeded: boolean
  exceeded_reason: string | null
  exceeded_detail: string | null
}

interface Segment {
  key: string
  label: string
  used: number
  cap: number | undefined
  format: (n: number) => string
}

function pct(used: number, cap: number | undefined) {
  if (!cap || cap <= 0) return 0
  return Math.min((used / cap) * 100, 100)
}

function barColor(percent: number) {
  if (percent >= 90) return 'bg-red-500'
  if (percent >= 70) return 'bg-amber-500'
  return 'bg-indigo-500'
}

export default function BudgetBar() {
  const { agents } = useAgentStore()
  const [snap, setSnap] = useState<BudgetSnapshot | null>(null)

  // Aggregate trigger — whenever total spend/turns across agents ticks
  // up, re-pull the authoritative budget snapshot from the backend. This
  // keeps the bar in sync without re-implementing the caps calculation
  // in the frontend.
  const usageKey = useMemo(() => {
    const agentList = Object.values(agents)
    const totalCost = agentList.reduce((sum, a) => sum + (a.usage?.cost_usd || 0), 0)
    const totalTokens = agentList.reduce(
      (sum, a) => sum + (a.usage?.input_tokens || 0) + (a.usage?.output_tokens || 0),
      0,
    )
    const statuses = agentList.map((a) => `${a.id}:${a.status}`).sort().join(',')
    return `${totalCost}|${totalTokens}|${statuses}`
  }, [agents])

  useEffect(() => {
    let cancelled = false
    apiFetch('/api/budget')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (!cancelled) setSnap(d) })
      .catch(() => { if (!cancelled) setSnap(null) })
    return () => { cancelled = true }
  }, [usageKey])

  if (!snap) return null

  const caps = snap.caps
  const hasAnyCap =
    caps.max_total_cost_usd !== undefined ||
    caps.max_total_turns !== undefined ||
    caps.max_wall_clock_min !== undefined ||
    caps.max_concurrent_agents !== undefined

  if (!hasAnyCap) return null

  const segments: Segment[] = [
    {
      key: 'cost',
      label: 'Cost',
      used: snap.usage.cost_usd,
      cap: caps.max_total_cost_usd,
      format: (n) => `$${n.toFixed(3)}`,
    },
    {
      key: 'turns',
      label: 'Turns',
      used: snap.usage.turns,
      cap: caps.max_total_turns,
      format: (n) => String(Math.round(n)),
    },
    {
      key: 'time',
      label: 'Time',
      used: snap.usage.wall_clock_min,
      cap: caps.max_wall_clock_min,
      format: (n) => `${n.toFixed(1)}m`,
    },
    {
      key: 'agents',
      label: 'Agents',
      used: snap.usage.concurrent,
      cap: caps.max_concurrent_agents,
      format: (n) => String(Math.round(n)),
    },
  ].filter((s) => s.cap !== undefined)

  return (
    <div
      className={`flex items-center gap-3 px-6 py-1.5 border-b text-[11px] ${
        snap.exceeded
          ? 'border-red-800/60 bg-red-950/30'
          : 'border-gray-800 bg-gray-900/40'
      }`}
    >
      <span
        className={`uppercase tracking-wide shrink-0 ${
          snap.exceeded ? 'text-red-300' : 'text-gray-500'
        }`}
      >
        {snap.exceeded ? `Budget blocked (${snap.exceeded_reason})` : 'Budget'}
      </span>
      <div className="flex-1 flex items-center gap-4">
        {segments.map((s) => {
          const p = pct(s.used, s.cap)
          return (
            <div key={s.key} className="flex items-center gap-1.5 min-w-[120px]">
              <span className="text-gray-500 w-12 shrink-0">{s.label}</span>
              <div className="flex-1 h-1.5 bg-gray-800 rounded-full overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all duration-500 ${barColor(p)}`}
                  style={{ width: `${p}%` }}
                />
              </div>
              <span className="text-gray-400 font-mono whitespace-nowrap shrink-0">
                {s.format(s.used)} / {s.format(s.cap!)}
              </span>
            </div>
          )
        })}
      </div>
      {snap.exceeded && (
        <button
          onClick={async () => {
            await apiPost('/api/budget/reset', {})
            const res = await apiFetch('/api/budget')
            if (res.ok) setSnap(await res.json())
          }}
          className="text-[11px] px-2 py-0.5 border border-red-700/60 text-red-300 hover:text-red-200 rounded shrink-0"
          title={snap.exceeded_detail || 'Clear budget counters and unblock'}
        >
          Reset
        </button>
      )}
    </div>
  )
}
