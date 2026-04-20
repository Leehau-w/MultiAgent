import { useEffect, useMemo, useState } from 'react'
import { useAgentStore } from '../stores/agentStore'
import { apiPost } from '../utils/api'
import type { CoordinatorNotification, NotificationLevel } from '../types'

// Max notifications rendered as stacked toasts. Anything beyond this drops
// to the "N more" overflow dropdown so the UI stays usable during a storm.
const MAX_VISIBLE = 3

// Auto-dismiss for info toasts. Warnings and blockers are persistent.
const INFO_DISMISS_MS = 5000

const LEVEL_STYLES: Record<NotificationLevel, string> = {
  info: 'border-sky-700/70 bg-sky-950/70 text-sky-100',
  warning: 'border-amber-700/70 bg-amber-950/70 text-amber-100',
  blocker: 'border-rose-700/70 bg-rose-950/80 text-rose-100',
}

const LEVEL_LABEL: Record<NotificationLevel, string> = {
  info: 'Info',
  warning: 'Warning',
  blocker: 'Blocker',
}

function formatTime(ts: string): string {
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

async function overrideStage(reason: string) {
  await apiPost('/api/pipeline/approve', { summary: reason })
}

export default function NotificationsContainer() {
  const notifications = useAgentStore((s) => s.notifications)
  const dismissNotification = useAgentStore((s) => s.dismissNotification)
  const seedNotifications = useAgentStore((s) => s.seedNotifications)
  const [overflowOpen, setOverflowOpen] = useState(false)

  // Replay endpoint fetch on mount — a newly-opened browser tab needs to
  // see outstanding blockers that fired before it connected.
  useEffect(() => {
    let cancelled = false
    fetch('/api/notifications?limit=200')
      .then((r) => (r.ok ? r.json() : { notifications: [] }))
      .then((d) => {
        if (cancelled || !Array.isArray(d.notifications)) return
        const items: CoordinatorNotification[] = d.notifications.map(
          (n: {
            id: string
            level: NotificationLevel
            message: string
            action_required: boolean
            timestamp: string
          }) => ({
            id: n.id,
            level: n.level,
            message: n.message,
            action_required: n.action_required,
            timestamp: n.timestamp,
            dismissed: false,
          }),
        )
        seedNotifications(items)
      })
      .catch(() => {
        /* ignore — replay is best-effort */
      })
    return () => {
      cancelled = true
    }
  }, [seedNotifications])

  // Auto-dismiss info toasts after INFO_DISMISS_MS.
  useEffect(() => {
    const timers: ReturnType<typeof setTimeout>[] = []
    for (const n of notifications) {
      if (n.level === 'info' && !n.dismissed) {
        timers.push(
          setTimeout(() => dismissNotification(n.id), INFO_DISMISS_MS),
        )
      }
    }
    return () => {
      for (const t of timers) clearTimeout(t)
    }
  }, [notifications, dismissNotification])

  const active = useMemo(
    () => notifications.filter((n) => !n.dismissed),
    [notifications],
  )
  const visible = active.slice(-MAX_VISIBLE).reverse() // newest first, capped
  const overflow = active.slice(0, Math.max(0, active.length - MAX_VISIBLE))

  if (active.length === 0) return null

  return (
    <div className="fixed top-3 right-3 z-50 flex flex-col items-end gap-2 max-w-[400px]">
      {visible.map((n) => (
        <NotificationCard
          key={n.id}
          notification={n}
          onDismiss={() => dismissNotification(n.id)}
          onOverride={
            n.action_required
              ? async () => {
                  await overrideStage(
                    `override via blocker: ${n.message.slice(0, 200)}`,
                  )
                  dismissNotification(n.id)
                }
              : undefined
          }
        />
      ))}
      {overflow.length > 0 && (
        <button
          onClick={() => setOverflowOpen((v) => !v)}
          className="text-[11px] px-2 py-1 rounded-md border border-gray-700 bg-gray-900/80 text-gray-300 hover:text-white"
        >
          {overflowOpen ? 'Hide' : 'Show'} {overflow.length} more
        </button>
      )}
      {overflowOpen && (
        <div className="w-[320px] max-h-[320px] overflow-y-auto bg-gray-900/95 border border-gray-700 rounded-md p-2 flex flex-col gap-1">
          {overflow
            .slice()
            .reverse()
            .map((n) => (
              <div
                key={n.id}
                className="flex items-start gap-2 text-[11px] p-1.5 rounded hover:bg-gray-800/60"
              >
                <span
                  className={`shrink-0 px-1.5 py-0.5 rounded text-[9px] uppercase tracking-wider border ${
                    LEVEL_STYLES[n.level]
                  }`}
                >
                  {LEVEL_LABEL[n.level]}
                </span>
                <div className="flex-1 min-w-0">
                  <div className="text-gray-200 break-words">{n.message}</div>
                  <div className="text-gray-500 text-[9px]">
                    {formatTime(n.timestamp)}
                  </div>
                </div>
                <button
                  onClick={() => dismissNotification(n.id)}
                  className="text-gray-500 hover:text-gray-200 text-xs leading-none"
                >
                  ×
                </button>
              </div>
            ))}
        </div>
      )}
    </div>
  )
}

function NotificationCard({
  notification,
  onDismiss,
  onOverride,
}: {
  notification: CoordinatorNotification
  onDismiss: () => void
  onOverride?: () => void | Promise<void>
}) {
  const { level, message, action_required, timestamp } = notification
  return (
    <div
      className={`w-[360px] border rounded-md shadow-lg px-3 py-2 backdrop-blur ${
        LEVEL_STYLES[level]
      }`}
    >
      <div className="flex items-center justify-between gap-2 mb-1">
        <span className="text-[10px] uppercase tracking-wider font-semibold">
          {LEVEL_LABEL[level]}
          {action_required && (
            <span className="ml-1.5 text-[9px] px-1 py-0.5 rounded bg-rose-700/60 text-rose-100">
              action required
            </span>
          )}
        </span>
        <div className="flex items-center gap-2">
          <span className="text-[10px] opacity-70">{formatTime(timestamp)}</span>
          <button
            onClick={onDismiss}
            className="text-xs opacity-70 hover:opacity-100 leading-none"
            title="Dismiss"
          >
            ×
          </button>
        </div>
      </div>
      <div className="text-sm whitespace-pre-wrap break-words">{message}</div>
      {onOverride && (
        <div className="mt-2 flex justify-end">
          <button
            onClick={() => {
              void onOverride()
            }}
            className="text-[11px] px-2 py-1 rounded-md border border-rose-400/60 text-rose-100 hover:bg-rose-900/60"
          >
            Override &amp; continue
          </button>
        </div>
      )}
    </div>
  )
}
