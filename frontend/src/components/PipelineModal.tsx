import { useEffect, useState } from 'react'
import { useAgentStore } from '../stores/agentStore'
import { useToastStore } from '../stores/toastStore'
import { apiDelete, apiFetch, apiPost, apiPut } from '../utils/api'

interface Stage {
  name: string
  agents: string[]
  parallel: boolean
}

const DEFAULT_STAGES: Stage[] = [
  { name: 'Analysis', agents: ['pm'], parallel: false },
  { name: 'Design', agents: ['td'], parallel: false },
  { name: 'Implementation', agents: ['developer', 'developer'], parallel: true },
  { name: 'Review', agents: ['reviewer'], parallel: false },
]

interface Props {
  open: boolean
  onClose: () => void
}

export default function PipelineModal({ open, onClose }: Props) {
  const { roles } = useAgentStore()
  const [requirement, setRequirement] = useState('')
  const [stages, setStages] = useState<Stage[]>(DEFAULT_STAGES)
  const [submitting, setSubmitting] = useState(false)
  const [savingWorkflow, setSavingWorkflow] = useState(false)
  const [source, setSource] = useState<'default' | 'workflow'>('default')

  // On open, load the project's workflow.yaml (if any) so the editor
  // starts from the user's last-saved pipeline rather than the
  // hardcoded default.
  useEffect(() => {
    if (!open) return
    let cancelled = false
    ;(async () => {
      try {
        const res = await apiFetch('/api/workflow')
        if (cancelled) return
        if (!res.ok) {
          setStages(DEFAULT_STAGES)
          setSource('default')
          return
        }
        const data = await res.json()
        if (data?.exists && Array.isArray(data.workflow?.stages)) {
          setStages(
            data.workflow.stages.map((s: Stage) => ({
              name: s.name,
              agents: [...s.agents],
              parallel: Boolean(s.parallel),
            })),
          )
          setSource('workflow')
        } else {
          setStages(DEFAULT_STAGES)
          setSource('default')
        }
      } catch {
        setStages(DEFAULT_STAGES)
        setSource('default')
      }
    })()
    return () => {
      cancelled = true
    }
  }, [open])

  if (!open) return null

  const roleIds = Object.keys(roles)

  const updateStage = (i: number, patch: Partial<Stage>) => {
    setStages((prev) => prev.map((s, idx) => (idx === i ? { ...s, ...patch } : s)))
  }

  const addAgentToStage = (i: number) => {
    setStages((prev) =>
      prev.map((s, idx) =>
        idx === i ? { ...s, agents: [...s.agents, roleIds[0] || 'developer'] } : s,
      ),
    )
  }

  const removeAgentFromStage = (si: number, ai: number) => {
    setStages((prev) =>
      prev.map((s, idx) =>
        idx === si ? { ...s, agents: s.agents.filter((_, j) => j !== ai) } : s,
      ),
    )
  }

  const setAgentRole = (si: number, ai: number, role: string) => {
    setStages((prev) =>
      prev.map((s, idx) =>
        idx === si
          ? { ...s, agents: s.agents.map((a, j) => (j === ai ? role : a)) }
          : s,
      ),
    )
  }

  const addStage = () => {
    setStages((prev) => [
      ...prev,
      { name: `Stage ${prev.length + 1}`, agents: [roleIds[0] || 'developer'], parallel: false },
    ])
  }

  const removeStage = (i: number) => {
    setStages((prev) => prev.filter((_, idx) => idx !== i))
  }

  const stagesForApi = () =>
    stages.map((s) => ({
      name: s.name.toLowerCase(),
      agents: s.agents,
      parallel: s.parallel,
    }))

  const handleStart = async () => {
    if (!requirement.trim() || stages.length === 0) return
    setSubmitting(true)
    const res = await apiPost('/api/pipeline/start', {
      requirement: requirement.trim(),
      stages: stagesForApi(),
    })
    setSubmitting(false)
    if (res.ok) {
      onClose()
      setRequirement('')
    }
  }

  const handleSaveAsDefault = async () => {
    if (stages.length === 0) return
    setSavingWorkflow(true)
    const res = await apiPut('/api/workflow', { stages: stagesForApi() })
    setSavingWorkflow(false)
    if (res.ok) {
      setSource('workflow')
      useToastStore.getState().add('success', 'Saved as project default')
    }
  }

  const handleResetToDefaults = async () => {
    setStages(DEFAULT_STAGES)
    const res = await apiDelete('/api/workflow')
    if (res.ok) {
      setSource('default')
      useToastStore.getState().add('success', 'Reverted to built-in defaults')
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className="bg-gray-900 border border-gray-700 rounded-xl w-[640px] max-w-[95vw] max-h-[85vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-5 py-4 border-b border-gray-800">
          <h2 className="text-white font-semibold text-base">Start Pipeline</h2>
          <p className="text-gray-500 text-xs mt-1">
            Define the requirement and configure execution stages.
          </p>
          <p className="text-[10px] mt-1.5">
            {source === 'workflow' ? (
              <span className="text-indigo-400">
                Loaded from <code className="text-indigo-300">workflow.yaml</code>
              </span>
            ) : (
              <span className="text-gray-500">Using built-in defaults (no workflow.yaml)</span>
            )}
          </p>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">
          {/* Requirement */}
          <div>
            <label className="block text-xs text-gray-400 uppercase tracking-wide mb-1.5">
              Requirement
            </label>
            <textarea
              autoFocus
              value={requirement}
              onChange={(e) => setRequirement(e.target.value)}
              placeholder="Describe what you want to build..."
              rows={4}
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 resize-none focus:outline-none focus:border-indigo-500"
            />
          </div>

          {/* Stages */}
          <div>
            <label className="block text-xs text-gray-400 uppercase tracking-wide mb-1.5">
              Stages
            </label>
            <div className="space-y-2">
              {stages.map((stage, si) => (
                <div key={si} className="bg-gray-800/60 border border-gray-700/50 rounded-lg p-3">
                  <div className="flex items-center gap-2 mb-2">
                    {/* Stage number badge */}
                    <span className="w-5 h-5 rounded-full bg-indigo-600 text-white text-[10px] flex items-center justify-center shrink-0">
                      {si + 1}
                    </span>
                    <input
                      value={stage.name}
                      onChange={(e) => updateStage(si, { name: e.target.value })}
                      className="bg-transparent text-sm text-white font-medium flex-1 focus:outline-none border-b border-transparent focus:border-indigo-500"
                    />
                    <label className="flex items-center gap-1 text-[11px] text-gray-400 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={stage.parallel}
                        onChange={(e) => updateStage(si, { parallel: e.target.checked })}
                        className="accent-indigo-500"
                      />
                      Parallel
                    </label>
                    <button
                      onClick={() => removeStage(si)}
                      className="text-gray-500 hover:text-red-400 text-sm"
                      title="Remove stage"
                    >
                      &times;
                    </button>
                  </div>

                  {/* Agents in this stage */}
                  <div className="flex flex-wrap gap-1.5">
                    {stage.agents.map((roleId, ai) => (
                      <div key={ai} className="flex items-center gap-1 bg-gray-900 rounded px-2 py-0.5">
                        <select
                          value={roleId}
                          onChange={(e) => setAgentRole(si, ai, e.target.value)}
                          className="bg-transparent text-xs text-gray-300 focus:outline-none cursor-pointer"
                        >
                          {roleIds.map((rid) => (
                            <option key={rid} value={rid}>
                              {roles[rid]?.name || rid}
                            </option>
                          ))}
                        </select>
                        {stage.agents.length > 1 && (
                          <button
                            onClick={() => removeAgentFromStage(si, ai)}
                            className="text-gray-600 hover:text-red-400 text-[10px]"
                          >
                            &times;
                          </button>
                        )}
                      </div>
                    ))}
                    <button
                      onClick={() => addAgentToStage(si)}
                      className="text-[11px] text-indigo-400 hover:text-indigo-300 px-1"
                    >
                      + agent
                    </button>
                  </div>
                </div>
              ))}
            </div>
            <button
              onClick={addStage}
              className="mt-2 text-xs text-gray-500 hover:text-gray-300 border border-dashed border-gray-700 rounded-lg px-3 py-1.5 w-full hover:border-gray-500 transition-colors"
            >
              + Add Stage
            </button>
          </div>
        </div>

        {/* Footer */}
        <div className="px-5 py-3 border-t border-gray-800 flex justify-between items-center gap-2">
          <div className="flex gap-2">
            <button
              onClick={handleSaveAsDefault}
              disabled={stages.length === 0 || savingWorkflow}
              className="px-3 py-1.5 text-xs text-gray-300 border border-gray-700 rounded-md hover:bg-gray-800 disabled:text-gray-600 disabled:cursor-not-allowed"
              title="Persist these stages to workflow.yaml so future pipelines reuse them"
            >
              {savingWorkflow ? 'Saving…' : 'Save as default'}
            </button>
            {source === 'workflow' && (
              <button
                onClick={handleResetToDefaults}
                className="px-3 py-1.5 text-xs text-gray-500 hover:text-gray-300"
                title="Remove workflow.yaml and fall back to the built-in pipeline"
              >
                Reset
              </button>
            )}
          </div>
          <div className="flex gap-2">
            <button
              onClick={onClose}
              className="px-3 py-1.5 text-sm text-gray-400 hover:text-gray-200"
            >
              Cancel
            </button>
            <button
              onClick={handleStart}
              disabled={!requirement.trim() || stages.length === 0 || submitting}
              className="px-4 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:bg-gray-700 disabled:text-gray-500 text-white text-sm rounded-md transition-colors"
            >
              {submitting ? 'Starting...' : 'Start Pipeline'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
