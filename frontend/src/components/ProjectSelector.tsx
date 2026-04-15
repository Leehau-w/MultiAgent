import { useState, useEffect, useRef } from 'react'
import { apiFetch, apiPut } from '../utils/api'
import { useToastStore } from '../stores/toastStore'

export default function ProjectSelector() {
  const [projectPath, setProjectPath] = useState('')
  const [recent, setRecent] = useState<string[]>([])
  const [open, setOpen] = useState(false)
  const [inputValue, setInputValue] = useState('')
  const dropdownRef = useRef<HTMLDivElement>(null)

  // Fetch current project on mount
  useEffect(() => {
    apiFetch('/api/project')
      .then((r) => r.json())
      .then((d) => {
        setProjectPath(d.path || '')
        setRecent(d.recent || [])
      })
      .catch(() => {})
  }, [])

  // Close dropdown on outside click
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const switchProject = async (path: string) => {
    const res = await apiPut('/api/project', { path })
    if (res.ok) {
      const d = await res.json()
      setProjectPath(d.path)
      setRecent(d.recent || [])
      setOpen(false)
      setInputValue('')
      useToastStore.getState().add('success', `Switched to ${d.path}`)
    }
  }

  const handleSubmit = () => {
    const v = inputValue.trim()
    if (v) switchProject(v)
  }

  // Display name: last folder name
  const displayName = projectPath
    ? projectPath.replace(/\\/g, '/').split('/').filter(Boolean).pop() || projectPath
    : 'No project'

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => { setOpen(!open); setInputValue(projectPath) }}
        className="flex items-center gap-2 px-3 py-1.5 rounded-md border border-gray-700 hover:border-gray-500 bg-gray-900 transition-colors max-w-[280px]"
        title={projectPath || 'Select project'}
      >
        <svg className="w-3.5 h-3.5 text-gray-500 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
        </svg>
        <span className="text-sm text-gray-300 truncate">{displayName}</span>
        <svg className="w-3 h-3 text-gray-500 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {open && (
        <div className="absolute top-full left-0 mt-1 w-[420px] bg-gray-900 border border-gray-700 rounded-lg shadow-xl z-50 overflow-hidden">
          {/* Path input */}
          <div className="p-3 border-b border-gray-800">
            <label className="block text-[11px] text-gray-500 uppercase tracking-wide mb-1.5">
              Project Path
            </label>
            <div className="flex gap-2">
              <input
                autoFocus
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') handleSubmit() }}
                placeholder="D:\Project\MyApp"
                className="flex-1 bg-gray-800 border border-gray-700 rounded px-2.5 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-indigo-500 font-mono"
              />
              <button
                onClick={handleSubmit}
                disabled={!inputValue.trim()}
                className="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:bg-gray-700 disabled:text-gray-500 text-white text-sm rounded transition-colors shrink-0"
              >
                Open
              </button>
            </div>
          </div>

          {/* Recent projects */}
          {recent.length > 0 && (
            <div className="max-h-[240px] overflow-y-auto">
              <div className="px-3 pt-2 pb-1">
                <span className="text-[11px] text-gray-500 uppercase tracking-wide">Recent</span>
              </div>
              {recent.map((p) => {
                const name = p.replace(/\\/g, '/').split('/').filter(Boolean).pop() || p
                const isCurrent = p.replace(/\\/g, '/') === projectPath.replace(/\\/g, '/')
                return (
                  <button
                    key={p}
                    onClick={() => switchProject(p)}
                    className={`w-full text-left px-3 py-2 hover:bg-gray-800 flex items-center gap-2 transition-colors ${
                      isCurrent ? 'bg-indigo-500/10' : ''
                    }`}
                  >
                    <svg className="w-3.5 h-3.5 text-gray-600 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
                    </svg>
                    <div className="min-w-0 flex-1">
                      <div className="text-sm text-gray-200 truncate">{name}</div>
                      <div className="text-[11px] text-gray-500 truncate font-mono">{p}</div>
                    </div>
                    {isCurrent && (
                      <span className="text-[10px] text-indigo-400 shrink-0">current</span>
                    )}
                  </button>
                )
              })}
            </div>
          )}

          {recent.length === 0 && (
            <div className="px-3 py-4 text-sm text-gray-600 text-center">
              No recent projects. Enter a path above to get started.
            </div>
          )}
        </div>
      )}
    </div>
  )
}
