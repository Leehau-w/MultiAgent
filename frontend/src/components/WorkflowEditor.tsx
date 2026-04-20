import { useEffect, useState } from 'react'
import { apiFetch, apiPut } from '../utils/api'
import { useAgentStore } from '../stores/agentStore'
import { useToastStore } from '../stores/toastStore'

interface Props {
  open: boolean
  onClose: () => void
}

const EXAMPLE_WORKFLOW = `version: 1

stages:
  - name: analysis
    agents: [pm]
  - name: design
    agents: [td]
  - name: implementation
    agents: [developer, developer]
    parallel: true
  - name: review
    agents: [reviewer]

budget:
  max_total_cost_usd: 10.0
  max_total_turns: 200
  max_wall_clock_min: 60
  max_concurrent_agents: 5

coordinator:
  enabled: false
  role_id: coordinator
  allow_spawn: false

triggers:
  - on: pm.completed
    start: td
    context_from: [pm]
  - on: td.completed
    start: [developer, developer]
    context_from: [pm, td]
  - on: [developer.completed, developer.completed]
    start: reviewer
    context_from: [pm, td, developer]
`

export default function WorkflowEditor({ open, onClose }: Props) {
  const [content, setContent] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [exists, setExists] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setLoading(true)
    setError(null)
    apiFetch('/api/workflow/raw')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d) {
          setExists(!!d.exists)
          setContent(d.content || '')
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [open])

  const handleSave = async () => {
    setSaving(true)
    setError(null)
    try {
      const res = await apiPut('/api/workflow/raw', { content })
      if (res.ok) {
        useToastStore.getState().add('success', 'Workflow saved')
        setExists(true)
        useAgentStore.getState().refreshWorkflow()
        onClose()
      } else {
        const body = await res.json().catch(() => ({}))
        setError(body.detail || `Save failed (${res.status})`)
      }
    } catch (e) {
      setError(String(e))
    } finally {
      setSaving(false)
    }
  }

  const handleLoadExample = () => {
    setContent(EXAMPLE_WORKFLOW)
  }

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={onClose}
    >
      <div
        className="bg-gray-900 border border-gray-700 rounded-xl w-[780px] max-w-[95vw] max-h-[90vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-5 py-4 border-b border-gray-800 flex items-center justify-between">
          <div>
            <h2 className="text-white font-semibold text-base">Workflow Configuration</h2>
            <p className="text-gray-500 text-xs mt-0.5">
              Edit <span className="font-mono">workflow.yaml</span> — stages, budget,
              triggers, coordinator
              {exists ? (
                <span className="ml-2 px-1.5 py-0.5 bg-indigo-900/40 text-indigo-300 rounded">
                  loaded
                </span>
              ) : (
                <span className="ml-2 px-1.5 py-0.5 bg-gray-800 text-gray-400 rounded">
                  not yet saved
                </span>
              )}
            </p>
          </div>
          <button
            onClick={handleLoadExample}
            className="px-2 py-1 text-xs text-gray-400 hover:text-gray-200 border border-gray-700 hover:border-gray-500 rounded"
            title="Replace current content with a starter template"
          >
            Load example
          </button>
        </div>

        <div className="flex-1 min-h-0 p-4 flex flex-col gap-2">
          {loading ? (
            <div className="text-gray-500 text-sm p-4">Loading...</div>
          ) : (
            <>
              <textarea
                value={content}
                onChange={(e) => setContent(e.target.value)}
                spellCheck={false}
                placeholder="# No workflow.yaml yet — click 'Load example' or paste your own"
                className="flex-1 min-h-[420px] bg-gray-950 border border-gray-700 rounded-lg px-4 py-3 text-sm text-gray-300 font-mono resize-none focus:outline-none focus:border-indigo-500 leading-relaxed"
              />
              {error && (
                <div className="text-xs text-rose-300 bg-rose-950/30 border border-rose-800/60 rounded px-3 py-2 whitespace-pre-wrap">
                  {error}
                </div>
              )}
            </>
          )}
        </div>

        <div className="px-5 py-3 border-t border-gray-800 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="px-3 py-1.5 text-sm text-gray-400 hover:text-gray-200"
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={saving || loading || !content.trim()}
            className="px-4 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:bg-gray-700 disabled:text-gray-500 text-white text-sm rounded-md transition-colors"
          >
            {saving ? 'Saving...' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}
