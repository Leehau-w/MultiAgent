import { useEffect, useRef, useCallback } from 'react'
import type { WSEvent } from '../types'

type WSEventHandler = (event: WSEvent) => void

export function useWebSocket(onEvent: WSEventHandler) {
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>(undefined)

  const connect = useCallback(function connectWS() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${protocol}//${window.location.host}/ws`
    const ws = new WebSocket(url)

    ws.onopen = () => {
      console.log('[WS] Connected')
    }

    ws.onmessage = (e) => {
      try {
        const event: WSEvent = JSON.parse(e.data)
        onEvent(event)
      } catch {
        console.warn('[WS] Failed to parse message', e.data)
      }
    }

    ws.onclose = () => {
      // Only reconnect if this is still the active WebSocket.
      // Prevents StrictMode double-mount from creating duplicate connections.
      if (wsRef.current !== ws) return
      console.log('[WS] Disconnected, reconnecting in 2s...')
      reconnectTimer.current = setTimeout(connect, 2000)
    }

    ws.onerror = (err) => {
      console.error('[WS] Error', err)
      ws.close()
    }

    wsRef.current = ws
  }, [onEvent])

  useEffect(() => {
    connect()
    return () => {
      clearTimeout(reconnectTimer.current)
      const ws = wsRef.current
      wsRef.current = null   // clear ref BEFORE closing so onclose sees mismatch
      ws?.close()
    }
  }, [connect])

  const send = useCallback((data: unknown) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data))
    }
  }, [])

  return { send }
}
