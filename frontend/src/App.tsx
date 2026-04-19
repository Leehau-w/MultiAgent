import { useCallback, useEffect, useMemo, useState } from 'react'
import { useAgentStore } from './stores/agentStore'
import { useWebSocket } from './hooks/useWebSocket'
import Dashboard from './components/Dashboard'
import OutputStream from './components/OutputStream'
import ChatPanel from './components/ChatPanel'
import ContextViewer from './components/ContextViewer'
import ErrorPanel from './components/ErrorPanel'
import PermissionPanel from './components/PermissionPanel'
import PipelineModal from './components/PipelineModal'
import PipelineProgress from './components/PipelineProgress'
import ProjectSelector from './components/ProjectSelector'
import RolesEditor from './components/RolesEditor'
import ToastContainer from './components/Toast'
import type { WSEvent } from './types'

function App() {
  const { setRoles, setAgents, setGlobalPermissionMode, handleWSEvent, errors } = useAgentStore()
  const [pipelineOpen, setPipelineOpen] = useState(false)
  const [rolesOpen, setRolesOpen] = useState(false)
  const [errorsOpen, setErrorsOpen] = useState(false)

  const unresolvedErrorCount = useMemo(
    () => errors.filter((e) => e.final).length,
    [errors],
  )

  const onEvent = useCallback(
    (event: WSEvent) => handleWSEvent(event),
    [handleWSEvent],
  )
  useWebSocket(onEvent)

  // Initial data fetch
  useEffect(() => {
    fetch('/api/roles')
      .then((r) => r.json())
      .then((d) => setRoles(d.roles))
      .catch(console.error)

    fetch('/api/agents')
      .then((r) => r.json())
      .then((d) => setAgents(d.agents))
      .catch(console.error)

    fetch('/api/permission/mode')
      .then((r) => r.json())
      .then((d) => setGlobalPermissionMode(d.mode))
      .catch(console.error)
  }, [setRoles, setAgents, setGlobalPermissionMode])

  return (
    <div className="h-screen flex flex-col bg-gray-950 text-gray-200">
      {/* Header */}
      <header className="flex items-center justify-between px-6 py-3 border-b border-gray-800 shrink-0">
        <div className="flex items-center gap-3">
          <h1 className="text-lg font-semibold tracking-tight text-white shrink-0">
            MultiAgent Studio
          </h1>
          <ProjectSelector />
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setErrorsOpen(true)}
            className={`relative px-3 py-1.5 text-sm border rounded-md transition-colors ${
              unresolvedErrorCount > 0
                ? 'border-rose-700/60 text-rose-300 hover:text-rose-200'
                : 'border-gray-700 text-gray-400 hover:text-gray-200 hover:border-gray-500'
            }`}
          >
            Errors
            {unresolvedErrorCount > 0 && (
              <span className="ml-2 px-1.5 py-0.5 rounded bg-rose-600/80 text-white text-[10px] font-semibold">
                {unresolvedErrorCount}
              </span>
            )}
          </button>
          <button
            onClick={() => setRolesOpen(true)}
            className="px-3 py-1.5 text-gray-400 hover:text-gray-200 text-sm border border-gray-700 hover:border-gray-500 rounded-md transition-colors"
          >
            Roles
          </button>
          <button
            onClick={() => setPipelineOpen(true)}
            className="px-4 py-1.5 bg-indigo-600 hover:bg-indigo-500 text-white text-sm rounded-md transition-colors"
          >
            Start Pipeline
          </button>
        </div>
      </header>

      {/* Pipeline progress bar */}
      <PipelineProgress />

      {/* Agent cards */}
      <Dashboard />

      {/* Main content area */}
      <div className="flex-1 flex min-h-0 border-t border-gray-800">
        {/* Left: cross-agent permission approvals */}
        <PermissionPanel />
        {/* Middle: output + chat */}
        <div className="flex-1 flex flex-col min-w-0 border-r border-gray-800">
          <OutputStream />
          <ChatPanel />
        </div>
        {/* Right: context viewer */}
        <div className="w-[420px] shrink-0 flex flex-col min-h-0">
          <ContextViewer />
        </div>
      </div>

      {/* Modals */}
      <PipelineModal open={pipelineOpen} onClose={() => setPipelineOpen(false)} />
      <RolesEditor open={rolesOpen} onClose={() => setRolesOpen(false)} />
      <ErrorPanel open={errorsOpen} onClose={() => setErrorsOpen(false)} />

      {/* Toast notifications */}
      <ToastContainer />
    </div>
  )
}

export default App
