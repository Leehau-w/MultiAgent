import { useMemo } from 'react'
import { useAgentStore } from '../stores/agentStore'
import { apiPost, apiPut } from '../utils/api'
import type { PermissionMode, PermissionRequest } from '../types'

const TOOL_COLORS: Record<string, string> = {
  Write: 'text-orange-300',
  Edit: 'text-orange-300',
  Bash: 'text-red-300',
  Read: 'text-gray-300',
  Glob: 'text-gray-300',
  Grep: 'text-gray-300',
}

function toolPreview(req: PermissionRequest): string {
  const input = req.tool_input ?? {}
  switch (req.tool_name) {
    case 'Write':
    case 'Edit': {
      const path = (input.file_path as string) || ''
      return path || JSON.stringify(input).slice(0, 140)
    }
    case 'Bash': {
      const cmd = (input.command as string) || ''
      return cmd
    }
    case 'Read': {
      return (input.file_path as string) || ''
    }
    case 'Glob': {
      return (input.pattern as string) || ''
    }
    case 'Grep': {
      const pattern = (input.pattern as string) || ''
      const path = (input.path as string) || ''
      return path ? `${pattern}  in  ${path}` : pattern
    }
    default:
      return JSON.stringify(input).slice(0, 140)
  }
}

function toolBody(req: PermissionRequest): string | null {
  const input = req.tool_input ?? {}
  if (req.tool_name === 'Write') {
    const content = (input.content as string) || ''
    return content.length ? content.slice(0, 400) : null
  }
  if (req.tool_name === 'Edit') {
    const oldStr = (input.old_string as string) || ''
    const newStr = (input.new_string as string) || ''
    if (!oldStr && !newStr) return null
    return `- ${oldStr.slice(0, 180)}\n+ ${newStr.slice(0, 180)}`
  }
  return null
}

function groupByAgent(queue: PermissionRequest[]): Record<string, PermissionRequest[]> {
  const groups: Record<string, PermissionRequest[]> = {}
  for (const req of queue) {
    if (!groups[req.agent_id]) groups[req.agent_id] = []
    groups[req.agent_id].push(req)
  }
  return groups
}

const MODE_LABELS: Record<PermissionMode, string> = {
  manual: 'Manual',
  workspace: 'Workspace',
  bypass: 'Bypass',
}

const MODE_HINTS: Record<PermissionMode, string> = {
  manual: 'Every write/exec asks for approval.',
  workspace: 'Writes inside the project folder auto-approve. Everything else still asks.',
  bypass: 'Every tool call is auto-approved. Use with caution.',
}

export default function PermissionPanel() {
  const { permissionQueue, agents, globalPermissionMode } = useAgentStore()

  const grouped = useMemo(() => groupByAgent(permissionQueue), [permissionQueue])
  const total = permissionQueue.length

  const agentLabel = (id: string) => agents[id]?.role_name || id

  const resolveOne = async (requestId: string, allow: boolean) => {
    await apiPost('/api/permission', { request_id: requestId, allow })
  }

  const resolveMany = async (reqs: PermissionRequest[], allow: boolean) => {
    await Promise.all(
      reqs.map((r) => apiPost('/api/permission', { request_id: r.request_id, allow })),
    )
  }

  const changeMode = async (mode: PermissionMode) => {
    await apiPut('/api/permission/mode', { mode })
  }

  return (
    <aside className="w-[300px] shrink-0 flex flex-col border-r border-gray-800 bg-gray-900/40 min-h-0">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-gray-800 shrink-0">
        <span className="text-[11px] text-gray-500 uppercase tracking-wide">
          Permissions
        </span>
        <div className="flex items-center gap-2">
          <select
            value={globalPermissionMode}
            onChange={(e) => changeMode(e.target.value as PermissionMode)}
            title={MODE_HINTS[globalPermissionMode]}
            className={`text-[10px] bg-gray-800 border border-gray-700 rounded px-1.5 py-0.5 focus:outline-none focus:border-indigo-500 ${
              globalPermissionMode === 'bypass'
                ? 'text-red-300'
                : globalPermissionMode === 'workspace'
                ? 'text-yellow-300'
                : 'text-gray-300'
            }`}
          >
            <option value="manual">Manual</option>
            <option value="workspace">Workspace</option>
            <option value="bypass">Bypass</option>
          </select>
          {total > 0 ? (
            <span className="text-[11px] text-yellow-400 font-medium animate-pulse">
              {total}
            </span>
          ) : null}
        </div>
      </div>

      {/* Bypass warning banner */}
      {globalPermissionMode === 'bypass' && (
        <div className="px-3 py-2 bg-red-950/60 border-b border-red-900 text-[11px] text-red-200 leading-snug">
          <div className="font-medium">Bypass mode active</div>
          <div className="text-red-300/80">
            All tool calls — including writes outside the project folder and
            shell commands — are auto-approved.
          </div>
        </div>
      )}

      {/* Workspace mode banner */}
      {globalPermissionMode === 'workspace' && (
        <div className="px-3 py-2 bg-yellow-950/40 border-b border-yellow-900 text-[11px] text-yellow-200 leading-snug">
          Writes inside the project folder auto-approve. Other tools still ask.
        </div>
      )}

      {/* Bulk Allow All button */}
      {total > 1 && (
        <div className="px-3 py-1.5 border-b border-gray-800">
          <button
            onClick={() => resolveMany(permissionQueue, true)}
            className="w-full text-[11px] px-2 py-1 bg-green-700 hover:bg-green-600 text-white rounded"
            title="Allow every pending request"
          >
            Allow all {total} pending
          </button>
        </div>
      )}

      {/* Body */}
      <div className="flex-1 overflow-y-auto min-h-0">
        {total === 0 ? (
          <div className="px-3 py-6 text-xs text-gray-600 italic leading-relaxed">
            No tool approvals are waiting. Pending write, edit, or execute
            requests from any agent will appear here.
          </div>
        ) : (
          <div className="p-2 space-y-2">
            {Object.entries(grouped).map(([agentId, reqs]) => (
              <section
                key={agentId}
                className="border border-gray-800 rounded-lg overflow-hidden bg-gray-950/60"
              >
                {/* Agent group header */}
                <header className="flex items-center justify-between px-2.5 py-1.5 bg-gray-800/60 border-b border-gray-800">
                  <div className="flex items-center gap-1.5 min-w-0">
                    <span className="w-1.5 h-1.5 rounded-full bg-yellow-400 animate-pulse shrink-0" />
                    <span className="text-xs font-medium text-indigo-300 truncate">
                      {agentLabel(agentId)}
                    </span>
                    <span className="text-[10px] text-gray-500 shrink-0">
                      ({reqs.length})
                    </span>
                  </div>
                  {reqs.length > 1 && (
                    <button
                      onClick={() => resolveMany(reqs, true)}
                      className="text-[10px] px-1.5 py-0.5 bg-green-800/70 hover:bg-green-700 text-white rounded"
                    >
                      Allow all
                    </button>
                  )}
                </header>

                {/* Permission cards */}
                <ul className="divide-y divide-gray-800/70">
                  {reqs.map((req) => {
                    const body = toolBody(req)
                    const color = TOOL_COLORS[req.tool_name] || 'text-yellow-300'
                    return (
                      <li key={req.request_id} className="px-2.5 py-2 space-y-1.5">
                        <div className="flex items-center gap-1.5">
                          <span className={`text-[11px] font-semibold ${color}`}>
                            {req.tool_name}
                          </span>
                          <span className="text-[10px] text-gray-600">
                            {new Date(req.timestamp).toLocaleTimeString('en-GB', {
                              hour12: false,
                            })}
                          </span>
                        </div>
                        <div
                          className="text-[11px] text-gray-300 font-mono break-all leading-snug"
                          title={toolPreview(req)}
                        >
                          {toolPreview(req) || <span className="text-gray-600 italic">(no args)</span>}
                        </div>
                        {body && (
                          <pre className="text-[10px] text-gray-500 bg-gray-900/80 border border-gray-800 rounded px-2 py-1 max-h-24 overflow-auto whitespace-pre-wrap break-all">
                            {body}
                          </pre>
                        )}
                        <div className="flex gap-1.5 pt-0.5">
                          <button
                            onClick={() => resolveOne(req.request_id, true)}
                            className="flex-1 px-2 py-1 bg-green-700 hover:bg-green-600 text-white text-[11px] rounded font-medium"
                          >
                            Allow
                          </button>
                          <button
                            onClick={() => resolveOne(req.request_id, false)}
                            className="flex-1 px-2 py-1 bg-red-700 hover:bg-red-600 text-white text-[11px] rounded font-medium"
                          >
                            Deny
                          </button>
                        </div>
                      </li>
                    )
                  })}
                </ul>
              </section>
            ))}
          </div>
        )}
      </div>
    </aside>
  )
}
