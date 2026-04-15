import { useAgentStore } from '../stores/agentStore'

const STAGE_STATUS_COLORS: Record<string, string> = {
  pending: 'border-gray-600 bg-gray-800 text-gray-500',
  running: 'border-indigo-500 bg-indigo-500/10 text-indigo-400',
  completed: 'border-green-500 bg-green-500/10 text-green-400',
}

export default function PipelineProgress() {
  const { pipeline } = useAgentStore()

  if (!pipeline || pipeline.status === 'idle') return null

  return (
    <div className="px-6 py-2 border-b border-gray-800 bg-gray-900/50 shrink-0">
      <div className="flex items-center gap-1.5">
        <span className="text-[11px] text-gray-500 uppercase tracking-wide mr-2 shrink-0">
          Pipeline
        </span>

        {pipeline.stages.map((stage, i) => {
          let status = 'pending'
          if (i < pipeline.currentStage) status = 'completed'
          else if (i === pipeline.currentStage && pipeline.status === 'running') status = 'running'

          return (
            <div key={i} className="flex items-center gap-1.5">
              {i > 0 && (
                <div
                  className={`w-6 h-px ${
                    i <= pipeline.currentStage ? 'bg-indigo-500' : 'bg-gray-700'
                  }`}
                />
              )}
              <div
                className={`px-2.5 py-1 rounded-md border text-[11px] font-medium transition-colors ${STAGE_STATUS_COLORS[status]}`}
              >
                <span className="mr-1">
                  {status === 'completed' ? '\u2713' : status === 'running' ? '\u25CF' : '\u25CB'}
                </span>
                {stage.name}
                {stage.agents.length > 1 && (
                  <span className="text-gray-500 ml-1">x{stage.agents.length}</span>
                )}
                {stage.parallel && <span className="text-yellow-500 ml-1">\u2225</span>}
              </div>
            </div>
          )
        })}

        {pipeline.status === 'completed' && (
          <span className="ml-2 text-[11px] text-green-400 font-medium">
            Done
          </span>
        )}
        {pipeline.status === 'running' && (
          <span className="ml-2 text-[11px] text-indigo-400 animate-pulse">
            Running...
          </span>
        )}
      </div>
    </div>
  )
}
