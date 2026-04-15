import { useState } from 'react'
import { useAgentStore } from '../stores/agentStore'
import { apiPost } from '../utils/api'
import AgentCard from './AgentCard'

export default function Dashboard() {
  const { agents, roles } = useAgentStore()
  const [showCreate, setShowCreate] = useState(false)
  const [newRoleId, setNewRoleId] = useState('')
  const [newAgentId, setNewAgentId] = useState('')

  const handleCreate = async () => {
    if (!newRoleId) return
    const body: Record<string, string> = { role_id: newRoleId }
    if (newAgentId.trim()) body.agent_id = newAgentId.trim()

    const res = await apiPost('/api/agents', body)
    if (res.ok) {
      const agent = await res.json()
      useAgentStore.setState((s) => ({
        agents: { ...s.agents, [agent.id]: agent },
      }))
      setShowCreate(false)
      setNewRoleId('')
      setNewAgentId('')
    }
  }

  const agentList = Object.values(agents)

  return (
    <div className="px-6 py-3 border-b border-gray-800 shrink-0">
      <div className="flex items-center gap-3 flex-wrap">
        {agentList.map((agent) => (
          <AgentCard key={agent.id} agent={agent} />
        ))}

        {/* Add Agent button */}
        {!showCreate ? (
          <button
            onClick={() => setShowCreate(true)}
            className="h-[88px] w-[160px] border border-dashed border-gray-700 rounded-lg
              flex items-center justify-center text-gray-500 hover:text-gray-300
              hover:border-gray-500 transition-colors text-sm"
          >
            + Add Agent
          </button>
        ) : (
          <div className="h-[88px] flex items-center gap-2 px-3 bg-gray-900 rounded-lg border border-gray-700">
            <select
              value={newRoleId}
              onChange={(e) => setNewRoleId(e.target.value)}
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
              value={newAgentId}
              onChange={(e) => setNewAgentId(e.target.value)}
              className="bg-gray-800 text-sm rounded px-2 py-1 border border-gray-700 w-24 text-gray-200"
            />
            <button
              onClick={handleCreate}
              className="text-sm px-2 py-1 bg-indigo-600 hover:bg-indigo-500 rounded text-white"
            >
              Create
            </button>
            <button
              onClick={() => setShowCreate(false)}
              className="text-gray-500 hover:text-gray-300 text-sm"
            >
              Cancel
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
