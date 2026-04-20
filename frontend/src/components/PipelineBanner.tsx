import { useState } from 'react'
import { useAgentStore } from '../stores/agentStore'
import { apiPost } from '../utils/api'

export default function PipelineBanner() {
  const pipeline = useAgentStore((s) => s.pipeline)
  const [busy, setBusy] = useState<'retry' | 'force' | null>(null)

  // Banner only surfaces for the two terminal-but-recoverable states.
  // `failed` is an ABORT (unrecoverable); `error` is a backend crash — both
  // render via the existing ErrorPanel/stage pills, not here.
  const paused = pipeline.status === 'paused'
  const failed = pipeline.status === 'failed'

  if (!paused && !failed) return null

  const resume = async (action: 'retry' | 'force_advance') => {
    setBusy(action === 'retry' ? 'retry' : 'force')
    try {
      await apiPost('/api/pipeline/resume', { action })
    } finally {
      setBusy(null)
    }
  }

  if (failed) {
    return (
      <div className="px-6 py-2 border-b border-rose-800 bg-rose-950/50 flex items-center gap-3 shrink-0">
        <span className="text-[11px] uppercase tracking-wider text-rose-300 font-semibold">
          Pipeline aborted
        </span>
        <span className="text-sm text-rose-100 truncate flex-1">
          {pipeline.failureReason || 'ABORT recorded by coordinator'}
        </span>
      </div>
    )
  }

  return (
    <div className="px-6 py-2 border-b border-amber-800 bg-amber-950/50 flex items-center gap-3 shrink-0">
      <span className="text-[11px] uppercase tracking-wider text-amber-300 font-semibold shrink-0">
        Pipeline paused
      </span>
      <span className="text-sm text-amber-100 truncate flex-1 min-w-0">
        {pipeline.pauseReason || 'Awaiting user input'}
      </span>
      <button
        onClick={() => resume('retry')}
        disabled={busy !== null}
        className="text-[11px] px-2.5 py-1 rounded-md border border-amber-600/70 text-amber-100 hover:bg-amber-900/60 disabled:opacity-50"
        title="Re-run the gate review with the coordinator"
      >
        {busy === 'retry' ? 'Retrying…' : 'Retry gate review'}
      </button>
      <button
        onClick={() => resume('force_advance')}
        disabled={busy !== null}
        className="text-[11px] px-2.5 py-1 rounded-md border border-rose-600/70 text-rose-100 hover:bg-rose-900/60 disabled:opacity-50"
        title="Skip the gate and advance — logged as user_override"
      >
        {busy === 'force' ? 'Advancing…' : 'Force advance'}
      </button>
    </div>
  )
}
