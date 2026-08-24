/**
 * Server-Sent Events for React Native.
 *
 * RN's `fetch` (whatwg-fetch over XHR) does not expose a streaming body, so
 * `res.body.getReader()` is unavailable. This module implements the SSE wire format on
 * top of `XMLHttpRequest` progress: RN fires `readystatechange` with `readyState === 3`
 * (LOADING) as bytes arrive and keeps appending to `responseText`, so we slice off the
 * part we have not parsed yet and decode complete `\n\n`-terminated frames.
 *
 * Screens never touch this directly -- they consume `streamThreadMessage()` from
 * `endpoints.ts`, which layers a non-streaming fallback on top.
 */
import { ApiError, authHeaders, buildUrl, NetworkError, type QueryValue } from "./client"

export interface SseFrame {
  event: string
  data: string
  id?: string
}

export interface EventStreamOptions {
  method?: "GET" | "POST"
  body?: unknown
  query?: Record<string, QueryValue>
  /** Fired for every complete frame. `data` is the raw text. */
  onFrame: (frame: SseFrame) => void
  /** Fired once when the stream ends without a transport error. */
  onClose?: () => void
  /** Fired once on transport/HTTP failure. The stream is closed afterwards. */
  onError?: (error: Error) => void
  /**
   * Abort the request if the server sends nothing at all within this window.
   * Once the first byte arrives the stream may stay open indefinitely.
   */
  firstByteTimeoutMs?: number
}

export interface EventStreamHandle {
  /** Aborts the request. Safe to call more than once; fires neither onClose nor onError. */
  close: () => void
  /** True once the stream finished, errored, or was closed by the caller. */
  readonly isClosed: boolean
}

const FRAME_BOUNDARY = /\r\n\r\n|\n\n|\r\r/

function parseFrame(chunk: string): SseFrame | null {
  let event = "message"
  let id: string | undefined
  const dataLines: string[] = []

  for (const rawLine of chunk.split(/\r\n|\n|\r/)) {
    if (rawLine === "" || rawLine.startsWith(":")) continue
    const colon = rawLine.indexOf(":")
    const field = colon === -1 ? rawLine : rawLine.slice(0, colon)
    let value = colon === -1 ? "" : rawLine.slice(colon + 1)
    if (value.startsWith(" ")) value = value.slice(1)

    if (field === "event") event = value
    else if (field === "data") dataLines.push(value)
    else if (field === "id") id = value
  }

  if (dataLines.length === 0 && event === "message") return null
  return { event, data: dataLines.join("\n"), id }
}

/** Opens an SSE connection. Returns immediately with a handle that can abort it. */
export function openEventStream(path: string, options: EventStreamOptions): EventStreamHandle {
  const method = options.method ?? "GET"
  const url = buildUrl(path, options.query)

  const xhr = new XMLHttpRequest()
  let closedByCaller = false
  let finished = false
  let consumed = 0
  let buffer = ""

  const state = {
    get isClosed(): boolean {
      return finished || closedByCaller
    },
    close(): void {
      if (finished || closedByCaller) return
      closedByCaller = true
      clearTimeout(firstByteTimer)
      try {
        xhr.abort()
      } catch {
        /* already gone */
      }
    },
  }

  const fail = (error: Error): void => {
    if (finished || closedByCaller) return
    finished = true
    clearTimeout(firstByteTimer)
    options.onError?.(error)
  }

  const succeed = (): void => {
    if (finished || closedByCaller) return
    finished = true
    clearTimeout(firstByteTimer)
    options.onClose?.()
  }

  const firstByteTimer = setTimeout(() => {
    if (consumed === 0 && !finished && !closedByCaller) {
      try {
        xhr.abort()
      } catch {
        /* ignore */
      }
      fail(new NetworkError("The stream did not start in time."))
    }
  }, options.firstByteTimeoutMs ?? 25000)

  const drain = (): void => {
    const text = xhr.responseText ?? ""
    if (text.length <= consumed) return
    buffer += text.slice(consumed)
    consumed = text.length

    for (;;) {
      const match = FRAME_BOUNDARY.exec(buffer)
      if (!match) break
      const chunk = buffer.slice(0, match.index)
      buffer = buffer.slice(match.index + match[0].length)
      const frame = parseFrame(chunk)
      if (frame && !closedByCaller) options.onFrame(frame)
    }
  }

  xhr.onreadystatechange = (): void => {
    if (closedByCaller || finished) return
    // 3 === LOADING: RN delivers partial bodies here. 4 === DONE.
    if (xhr.readyState === 3) {
      if (xhr.status >= 400) return // wait for DONE so we can read the error body
      drain()
      return
    }
    if (xhr.readyState !== 4) return

    if (xhr.status === 0) {
      fail(new NetworkError("The stream connection dropped."))
      return
    }
    if (xhr.status >= 400) {
      let detail = `HTTP ${xhr.status}`
      let code = `http_${xhr.status}`
      try {
        const parsed = JSON.parse(xhr.responseText) as { detail?: string; code?: string }
        if (parsed.detail) detail = parsed.detail
        if (parsed.code) code = parsed.code
      } catch {
        /* non-JSON error body */
      }
      fail(new ApiError(xhr.status, { detail, code }))
      return
    }
    drain()
    // Flush a trailing frame that was not newline-terminated.
    if (buffer.trim().length > 0) {
      const frame = parseFrame(buffer)
      buffer = ""
      if (frame) options.onFrame(frame)
    }
    succeed()
  }

  xhr.onerror = (): void => fail(new NetworkError("Cannot reach the Nesq Bot API."))
  xhr.ontimeout = (): void => fail(new NetworkError("The stream timed out."))

  try {
    xhr.open(method, url, true)
    xhr.setRequestHeader("Accept", "text/event-stream")
    xhr.setRequestHeader("Cache-Control", "no-cache")
    if (options.body !== undefined) xhr.setRequestHeader("Content-Type", "application/json")
    for (const [key, value] of Object.entries(authHeaders())) {
      xhr.setRequestHeader(key, value)
    }
    xhr.send(options.body === undefined ? null : JSON.stringify(options.body))
  } catch (error) {
    fail(error instanceof Error ? error : new NetworkError())
  }

  return state
}

/*
 * Event typing lives in `@nesqbot/protocol` (`parseSseEvent` for the turn stream,
 * `parseThreadEvent` for the passive channel). This module is transport only: it
 * hands raw `{event, data}` frames to `endpoints.ts`, which parses them.
 *
 * The hand-rolled union that used to live here is gone on purpose -- it guessed the
 * final-text field as `content` when the API writes `message`, which silently broke
 * the passive channel. One parser, owned by the protocol, is the fix.
 */
