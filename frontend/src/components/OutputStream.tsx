import { useEffect, useRef } from 'react'
import { useAgentStore } from '../stores/agentStore'

const TYPE_STYLES: Record<string, string> = {
  user: 'text-blue-400',
  text: 'text-gray-300',
  tool_use: 'text-yellow-400',
  tool_result: 'text-cyan-400',
  result: 'text-green-400 font-medium',
  error: 'text-red-400',
}

export default function OutputStream() {
  const { selectedAgentId, outputStreams, agents } = useAgentStore()
  const endRef = useRef<HTMLDivElement>(null)

  // Determine which streams to show
  const entries = selectedAgentId
    ? (outputStreams[selectedAgentId] || []).map((e) => ({ ...e, agentId: selectedAgentId }))
    : Object.entries(outputStreams).flatMap(([agentId, items]) =>
        items.map((e) => ({ ...e, agentId })),
      ).sort((a, b) => a.timestamp.localeCompare(b.timestamp))

  // Auto-scroll
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [entries.length])

  const agentLabel = (id: string) => {
    const a = agents[id]
    return a ? a.role_name : id
  }

  return (
    <div className="flex-1 overflow-y-auto p-4 font-mono text-xs leading-relaxed min-h-0">
      <div className="text-gray-500 text-[11px] mb-2 uppercase tracking-wide">
        Output Stream {selectedAgentId ? `- ${agentLabel(selectedAgentId)}` : '- All Agents'}
      </div>
      {entries.length === 0 && (
        <div className="text-gray-600 italic">No output yet. Start an agent to see its output here.</div>
      )}
      {entries.map((entry, i) => (
        <div key={i} className="flex gap-2 py-0.5">
          <span className="text-gray-600 shrink-0 w-[52px]">
            {new Date(entry.timestamp).toLocaleTimeString('en-GB', { hour12: false })}
          </span>
          {!selectedAgentId && (
            <span className="text-indigo-400 shrink-0 w-[80px] truncate">
              [{agentLabel(entry.agentId)}]
            </span>
          )}
          <span className={TYPE_STYLES[entry.type] || 'text-gray-300'}>
            {entry.type === 'user' && <span className="text-gray-500 mr-1">[you]</span>}
            {entry.type === 'tool_use' && <span className="text-gray-500 mr-1">[tool]</span>}
            {entry.content}
          </span>
        </div>
      ))}
      <div ref={endRef} />
    </div>
  )
}
