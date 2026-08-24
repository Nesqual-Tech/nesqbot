/**
 * EventSource-shaped helper built on `fetch` + `ReadableStream`.
 *
 * `EventSource` cannot POST and cannot send an `Authorization` header, and both
 * are required here: the chat turn is `POST /threads/{id}/messages/stream`, and
 * every route is authenticated. This parses `text/event-stream` frames by hand
 * and adds abort + reconnect-with-backoff on top.
 */
import { ApiError, authHeaders, buildUrl, isAbortError, type Query } from "./client"

export interface SseMessage {
  /** `event:` field, defaulting to `message` per the SSE spec. */
  event: string
  /** joined `data:` lines */
  data: string
  id?: string
}

export interface SseHandlers {
  onOpen?: () => void
  onMessage: (message: SseMessage) => void
  /** `willRetry` tells the caller whether a reconnect is scheduled. */
  onError?: (error: unknown, willRetry: boolean) => void
  /** Fired once, when the stream will not produce any more messages. */
  onClose?: (reason: "done" | "aborted" | "error") => void
}

export interface SseOptions extends SseHandlers {
  path: string
  method?: "GET" | "POST"
  body?: unknown
  query?: Query
  headers?: Record<string, string>
  /** Defaults to true for GET subscriptions, false for POST turns. */
  reconnect?: boolean
  maxRetries?: number
  baseDelayMs?: number
  maxDelayMs?: number
  signal?: AbortSignal
}

export interface SseHandle {
  close: () => void
  readonly closed: boolean
}

/** Split a raw SSE chunk buffer into complete frames. */
function parseFrame(frame: string): SseMessage | null {
  let event = "message"
  let id: string | undefined
  const dataLines: string[] = []
  let sawData = false

  for (const rawLine of frame.split("\n")) {
    const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine
    if (!line || line.startsWith(":")) continue
    const colon = line.indexOf(":")
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? "" : line.slice(colon + 1)
    if (value.startsWith(" ")) value = value.slice(1)

    switch (field) {
      case "event":
        event = value || "message"
        break
      case "data":
        dataLines.push(value)
        sawData = true
        break
      case "id":
        id = value
        break
      default:
        break
    }
  }

  if (!sawData) return null
  return { event, data: dataLines.join("\n"), id }
}

const sleep = (ms: number, signal?: AbortSignal): Promise<void> =>
  new Promise((resolve) => {
    const timer = setTimeout(resolve, ms)
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer)
        resolve()
      },
      { once: true },
    )
  })

/**
 * Open a server-sent event stream. Returns a handle whose `close()` aborts the
 * underlying request; the promise-based loop is fire-and-forget.
 */
export function openSse(options: SseOptions): SseHandle {
  const {
    path,
    method = "GET",
    body,
    query,
    headers,
    onOpen,
    onMessage,
    onError,
    onClose,
    reconnect = method === "GET",
    maxRetries = 8,
    baseDelayMs = 600,
    maxDelayMs = 15_000,
    signal: externalSignal,
  } = options

  const controller = new AbortController()
  let closed = false
  let lastEventId: string | undefined
  let serverRetryMs: number | null = null

  const abort = (): void => {
    if (closed) return
    closed = true
    controller.abort()
  }

  externalSignal?.addEventListener("abort", abort, { once: true })

  const finish = (reason: "done" | "aborted" | "error"): void => {
    if (!closed) closed = true
    onClose?.(reason)
  }

  void (async () => {
    let attempt = 0

    while (!closed) {
      let opened = false
      try {
        const res = await fetch(buildUrl(path, query), {
          method,
          signal: controller.signal,
          headers: authHeaders({
            Accept: "text/event-stream",
            "Cache-Control": "no-cache",
            ...(body === undefined ? {} : { "Content-Type": "application/json" }),
            ...(lastEventId ? { "Last-Event-ID": lastEventId } : {}),
            ...headers,
          }),
          body: body === undefined ? undefined : JSON.stringify(body),
        })

        if (!res.ok) {
          const text = await res.text().catch(() => "")
          let detail = text.slice(0, 300)
          let code = `http_${res.status}`
          try {
            const parsed = JSON.parse(text) as { detail?: string; code?: string }
            if (parsed.detail) detail = parsed.detail
            if (parsed.code) code = parsed.code
          } catch {
            /* text is fine */
          }
          throw new ApiError({
            status: res.status,
            code,
            detail: detail || `Stream failed (${res.status})`,
            requestId: res.headers.get("X-Request-Id"),
          })
        }

        if (!res.body) {
          throw new ApiError({
            status: 0,
            code: "stream_unsupported",
            detail: "This runtime cannot read a streaming response body.",
          })
        }

        opened = true
        attempt = 0
        onOpen?.()

        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ""

        // eslint-disable-next-line no-constant-condition
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })

          let sep = buffer.search(/\r?\n\r?\n/)
          while (sep !== -1) {
            const frame = buffer.slice(0, sep)
            const match = /\r?\n\r?\n/.exec(buffer.slice(sep))
            buffer = buffer.slice(sep + (match ? match[0].length : 2))
            const parsedFrame = parseFrame(frame)
            if (parsedFrame) {
              if (parsedFrame.id) lastEventId = parsedFrame.id
              if (!closed) onMessage(parsedFrame)
            }
            const retryMatch = /^retry:\s*(\d+)/m.exec(frame)
            if (retryMatch) serverRetryMs = Number(retryMatch[1])
            sep = buffer.search(/\r?\n\r?\n/)
          }
        }

        const tail = parseFrame(buffer)
        if (tail && !closed) onMessage(tail)
      } catch (err) {
        if (closed || isAbortError(err)) {
          finish("aborted")
          return
        }
        const willRetry = reconnect && attempt < maxRetries
        onError?.(err, willRetry)
        if (!willRetry) {
          finish("error")
          return
        }
        attempt += 1
        const delay = Math.min(serverRetryMs ?? baseDelayMs * 2 ** (attempt - 1), maxDelayMs)
        await sleep(delay + Math.random() * 250, controller.signal)
        continue
      }

      // Clean end of stream.
      if (closed) {
        finish("aborted")
        return
      }
      if (!reconnect) {
        finish("done")
        return
      }
      attempt = opened ? 0 : attempt + 1
      if (attempt > maxRetries) {
        finish("error")
        return
      }
      await sleep(Math.min(serverRetryMs ?? baseDelayMs * 2 ** attempt, maxDelayMs), controller.signal)
    }

    finish("aborted")
  })()

  return {
    close: abort,
    get closed() {
      return closed
    },
  }
}
