/**
 * Live subscription to `GET /threads/{id}/events` so worker- and routine-driven
 * turns show up in the open thread. Handlers are kept in a ref so changing them
 * never tears down the SSE connection.
 */
import { useEffect, useRef, useState } from "react"
import * as api from "../api/endpoints"
import { errorMessage } from "../api/client"
import type { SseHandle } from "../api/sse"
import type { ThreadEvent } from "../types"

export interface ThreadEventsOptions {
  enabled?: boolean
  onEvent?: (event: ThreadEvent) => void
}

export interface ThreadEventsApi {
  connected: boolean
  /** Last transport error, cleared on reconnect. */
  error: string | null
  retrying: boolean
}

export function useThreadEvents(threadId: string | null, options: ThreadEventsOptions = {}): ThreadEventsApi {
  const { enabled = true, onEvent } = options
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [retrying, setRetrying] = useState(false)

  const handlerRef = useRef<((event: ThreadEvent) => void) | undefined>(onEvent)
  handlerRef.current = onEvent

  useEffect(() => {
    if (!threadId || !enabled) {
      setConnected(false)
      return
    }

    let handle: SseHandle | null = null
    let disposed = false

    handle = api.openThreadEvents(threadId, {
      onOpen: () => {
        if (disposed) return
        setConnected(true)
        setRetrying(false)
        setError(null)
      },
      onEvent: (event) => {
        if (disposed) return
        handlerRef.current?.(event)
      },
      onError: (err, willRetry) => {
        if (disposed) return
        setConnected(false)
        setRetrying(willRetry)
        setError(errorMessage(err))
      },
      onClose: () => {
        if (disposed) return
        setConnected(false)
      },
    })

    return () => {
      disposed = true
      handle?.close()
    }
  }, [threadId, enabled])

  return { connected, error, retrying }
}
