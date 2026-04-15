import { useState, useEffect } from 'react'
import { useAgentStore } from '../stores/agentStore'
import { apiFetch, apiPut } from '../utils/api'
import { useToastStore } from '../stores/toastStore'

interface Props {
  open: boolean
  onClose: () => void
}

export default function RolesEditor({ open, onClose }: Props) {
  const [content, setContent] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)

  const { setRoles } = useAgentStore()

  useEffect(() => {
    if (!open) return
    setLoading(true)
    apiFetch('/api/config/roles')
      .then((r) => r.json())
      .then((d) => setContent(d.content || ''))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [open])

  const handleSave = async () => {
    setSaving(true)
    const res = await apiPut('/api/config/roles', { content })
    if (res.ok) {
      useToastStore.getState().add('success', 'Roles saved successfully')
      // Refresh roles in store
      const rolesRes = await apiFetch('/api/roles')
      if (rolesRes.ok) {
        const data = await rolesRes.json()
        setRoles(data.roles)
      }
      onClose()
    }
    setSaving(false)
  }

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className="bg-gray-900 border border-gray-700 rounded-xl w-[700px] max-w-[95vw] max-h-[85vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-5 py-4 border-b border-gray-800 flex items-center justify-between">
          <div>
            <h2 className="text-white font-semibold text-base">Role Configuration</h2>
            <p className="text-gray-500 text-xs mt-0.5">Edit roles.yaml to configure agent roles</p>
          </div>
        </div>

        {/* Editor */}
        <div className="flex-1 min-h-0 p-4">
          {loading ? (
            <div className="text-gray-500 text-sm p-4">Loading...</div>
          ) : (
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              spellCheck={false}
              className="w-full h-full min-h-[400px] bg-gray-950 border border-gray-700 rounded-lg px-4 py-3 text-sm text-gray-300 font-mono resize-none focus:outline-none focus:border-indigo-500 leading-relaxed"
            />
          )}
        </div>

        {/* Footer */}
        <div className="px-5 py-3 border-t border-gray-800 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="px-3 py-1.5 text-sm text-gray-400 hover:text-gray-200"
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={saving || loading}
            className="px-4 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:bg-gray-700 disabled:text-gray-500 text-white text-sm rounded-md transition-colors"
          >
            {saving ? 'Saving...' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}
