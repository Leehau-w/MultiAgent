import { useEffect, useMemo, useState } from 'react'
import { useAgentStore } from '../stores/agentStore'
import { apiDelete, apiFetch } from '../utils/api'
import type { ErrorInfo } from '../types'

const CATEGORY_STYLE: Record<ErrorInfo['category'], string> = {
  tool_error: 'bg-amber-900/30 border-amber-700/60 text-amber-200',
  api_error: 'bg-sky-900/30 border-sky-700/60 text-sky-200',
  auth_error: 'bg-rose-900/40 border-rose-700/70 text-rose-200',
  config_error: 'bg-rose-900/40 border-rose-700/70 text-rose-200',
  sdk_internal: 'bg-purple-900/30 border-purple-700/60 text-purple-200',
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString()
  } catch {
    return iso
  }
}

interface Props {
  open: boolean
  onClose: () => void
}

/**
 * Side drawer listing the most recent errors for the active project.
 * Filters to the selected agent when one is chosen, otherwise shows all.
 */
export default function ErrorPanel({ open, onClose }: Props) {
  const { selectedAgentId, errors, setErrors, clearErrors } = useAgentStore()
  const [loading, setLoading] = useState(false)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  // Fetch the persistent tail whenever the panel opens — merges with the
  // live stream already in state.
  useEffect(() => {
    if (!open) return
    setLoading(true)
    const url = selectedAgentId
      ? `/api/errors?agent_id=${encodeURIComponent(selectedAgentId)}&limit=100`
      : '/api/errors?limit=100'
    apiFetch(url)
      .then((r) => r.json())
      .then((d) => {
        if (Array.isArray(d.errors)) setErrors(d.errors as ErrorInfo[])
      })
      .finally(() => setLoading(false))
  }, [open, selectedAgentId, setErrors])

  const filtered = useMemo(() => {
    if (!selectedAgentId) return errors
    return errors.filter((e) => e.agent_id === selectedAgentId)
  }, [errors, selectedAgentId])

  const visible = useMemo(() => [...filtered].reverse(), [filtered])

  function toggle(id: string) {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  async function handleClear() {
    if (!confirm('Clear all errors for the active project?')) return
    await apiDelete('/api/errors')
    clearErrors()
  }

  if (!open) return null

  return (
    <div className="fixed inset-0 z-40 flex">
      <div
        className="flex-1 bg-black/50 backdrop-blur-sm"
        onClick={onClose}
      />
      <aside className="w-[460px] shrink-0 h-full bg-gray-900 border-l border-gray-800 flex flex-col">
        <header className="px-4 py-3 border-b border-gray-800 flex items-center justify-between">
          <div>
            <h2 className="text-sm font-semibold text-gray-100">
              Errors
              {selectedAgentId && (
                <span className="text-gray-400 font-normal ml-1">
                  · {selectedAgentId}
                </span>
              )}
            </h2>
            <p className="text-xs text-gray-500 mt-0.5">
              {visible.length} shown · retries are recoverable, red is final
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={handleClear}
              disabled={visible.length === 0}
              className="text-xs text-gray-400 hover:text-rose-400 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Clear
            </button>
            <button
              onClick={onClose}
              className="text-gray-500 hover:text-gray-200 text-lg leading-none"
            >
              ×
            </button>
          </div>
        </header>

        <div className="flex-1 overflow-auto p-3 space-y-2">
          {loading && visible.length === 0 && (
            <p className="text-xs text-gray-500">Loading…</p>
          )}
          {!loading && visible.length === 0 && (
            <p className="text-xs text-gray-500">No errors recorded.</p>
          )}
          {visible.map((e) => {
            const isOpen = expanded.has(e.id)
            return (
              <div
                key={e.id}
                className={`border rounded-md text-xs ${
                  CATEGORY_STYLE[e.category]
                } ${e.final ? 'ring-1 ring-rose-500/40' : ''}`}
              >
                <button
                  onClick={() => toggle(e.id)}
                  className="w-full text-left px-3 py-2 flex items-start justify-between gap-2"
                >
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-[10px] uppercase tracking-wider opacity-70">
                        {e.category}
                      </span>
                      {!e.final && (
                        <span className="text-[10px] text-gray-400">
                          retry {e.retry_count + 1}
                        </span>
                      )}
                      {e.final && (
                        <span className="text-[10px] text-rose-300">final</span>
                      )}
                    </div>
                    <div className="mt-1 truncate">{e.message}</div>
                  </div>
                  <div className="text-[10px] opacity-60 shrink-0">
                    {formatTime(e.timestamp)}
                  </div>
                </button>
                {isOpen && (
                  <div className="px-3 pb-3 space-y-2 border-t border-white/10">
                    <div className="text-[10px] text-gray-400">
                      agent · {e.agent_id}
                      {e.tool && (
                        <>
                          {'  ·  '}tool · {e.tool}
                        </>
                      )}
                    </div>
                    {e.stack && (
                      <pre className="bg-black/30 rounded p-2 overflow-auto max-h-40 text-[10px] font-mono whitespace-pre-wrap text-gray-300">
                        {e.stack}
                      </pre>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </aside>
    </div>
  )
}
