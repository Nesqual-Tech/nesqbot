/**
 * The app's global state: a toast queue, the active bot/thread selection, the
 * desktop routine recorder, and the parked human-handoff runs. Four tiny
 * contexts, no external store.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { AuthProvider } from "../auth"
import { uid } from "../lib/format"
import { TakeoverProvider } from "./takeover"
import type { DesktopActionInput, RecorderStep } from "../types"

/* ------------------------------------------------------------------ toasts */

export type ToastTone = "info" | "success" | "error" | "warning"

export interface Toast {
  id: string
  tone: ToastTone
  title: string
  description?: string
  /** ms; 0 keeps it until dismissed */
  timeout: number
  createdAt: number
}

export interface ToastInput {
  tone?: ToastTone
  title: string
  description?: string
  timeout?: number
}

interface ToastContextValue {
  toasts: Toast[]
  push: (input: ToastInput) => string
  dismiss: (id: string) => void
  clear: () => void
  success: (title: string, description?: string) => string
  error: (title: string, description?: string) => string
  warning: (title: string, description?: string) => string
  info: (title: string, description?: string) => string
}

const ToastContext = createContext<ToastContextValue | null>(null)

const DEFAULT_TIMEOUT: Record<ToastTone, number> = {
  info: 4500,
  success: 4000,
  warning: 8000,
  error: 0,
}

function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>())

  const dismiss = useCallback((id: string) => {
    const timer = timers.current.get(id)
    if (timer) {
      clearTimeout(timer)
      timers.current.delete(id)
    }
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const push = useCallback(
    (input: ToastInput) => {
      const tone = input.tone ?? "info"
      const toast: Toast = {
        id: uid("toast"),
        tone,
        title: input.title,
        description: input.description,
        timeout: input.timeout ?? DEFAULT_TIMEOUT[tone],
        createdAt: Date.now(),
      }
      setToasts((prev) => [...prev.slice(-4), toast])
      if (toast.timeout > 0) {
        timers.current.set(
          toast.id,
          setTimeout(() => dismiss(toast.id), toast.timeout),
        )
      }
      return toast.id
    },
    [dismiss],
  )

  const clear = useCallback(() => {
    timers.current.forEach((timer) => clearTimeout(timer))
    timers.current.clear()
    setToasts([])
  }, [])

  useEffect(() => {
    const map = timers.current
    return () => {
      map.forEach((timer) => clearTimeout(timer))
      map.clear()
    }
  }, [])

  const value = useMemo<ToastContextValue>(
    () => ({
      toasts,
      push,
      dismiss,
      clear,
      success: (title, description) => push({ tone: "success", title, description }),
      error: (title, description) => push({ tone: "error", title, description }),
      warning: (title, description) => push({ tone: "warning", title, description }),
      info: (title, description) => push({ tone: "info", title, description }),
    }),
    [toasts, push, dismiss, clear],
  )

  return <ToastContext.Provider value={value}>{children}</ToastContext.Provider>
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error("useToast must be used inside <AppProviders>")
  return ctx
}

/* --------------------------------------------------------------- selection */

/*
 * No `tab` here any more.
 *
 * The shell used to keep a nine-value panel tab in this context and persist
 * it, because the left rail *was* the navigation. It is a conversation list
 * now and the other eight sections live in the settings sheet — which is
 * transient by design: an app that reopens on the Audit panel because that is
 * where you last were is an app that reopens on nothing you asked for. What is
 * worth remembering across launches is which conversation you were in, and
 * that is `activeThreadId`.
 */
interface SelectionContextValue {
  activeBotId: string | null
  setActiveBotId: (id: string | null) => void
  activeThreadId: string | null
  setActiveThreadId: (id: string | null) => void
  /** Approval the user arrived at via deep link — panels scroll it into view. */
  focusApprovalId: string | null
  setFocusApprovalId: (id: string | null) => void
}

const SelectionContext = createContext<SelectionContextValue | null>(null)

const SELECTION_KEY = "nesq.selection"

interface StoredSelection {
  activeBotId?: string | null
  activeThreadId?: string | null
}

function readSelection(): StoredSelection {
  try {
    const raw = localStorage.getItem(SELECTION_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as StoredSelection
    return typeof parsed === "object" && parsed !== null ? parsed : {}
  } catch {
    return {}
  }
}

function SelectionProvider({ children }: { children: ReactNode }) {
  const stored = useRef<StoredSelection>(readSelection())
  const [activeBotId, setActiveBotId] = useState<string | null>(stored.current.activeBotId ?? null)
  const [activeThreadId, setActiveThreadId] = useState<string | null>(stored.current.activeThreadId ?? null)
  const [focusApprovalId, setFocusApprovalId] = useState<string | null>(null)

  useEffect(() => {
    try {
      localStorage.setItem(SELECTION_KEY, JSON.stringify({ activeBotId, activeThreadId }))
    } catch {
      /* ignore */
    }
  }, [activeBotId, activeThreadId])

  const value = useMemo<SelectionContextValue>(
    () => ({
      activeBotId,
      setActiveBotId,
      activeThreadId,
      setActiveThreadId,
      focusApprovalId,
      setFocusApprovalId,
    }),
    [activeBotId, activeThreadId, focusApprovalId],
  )

  return <SelectionContext.Provider value={value}>{children}</SelectionContext.Provider>
}

export function useSelection(): SelectionContextValue {
  const ctx = useContext(SelectionContext)
  if (!ctx) throw new Error("useSelection must be used inside <AppProviders>")
  return ctx
}

/* ---------------------------------------------------------------- recorder */

export interface RecorderContextValue {
  recording: boolean
  steps: RecorderStep[]
  /** Bot whose desktop the current recording belongs to. */
  botId: string | null
  start: (botId: string) => void
  stop: () => void
  toggle: (botId: string) => void
  record: (action: DesktopActionInput, label?: string) => void
  addStep: (step: Omit<RecorderStep, "uid" | "at">) => void
  removeStep: (uid: string) => void
  moveStep: (uid: string, delta: -1 | 1) => void
  updateStep: (uid: string, patch: Partial<RecorderStep>) => void
  clear: () => void
}

const RecorderContext = createContext<RecorderContextValue | null>(null)

function describeAction(action: DesktopActionInput): string {
  switch (action.action) {
    case "click":
    case "double_click":
    case "right_click":
      return `${action.action.replace("_", " ")} at ${action.x ?? 0}, ${action.y ?? 0}`
    case "type":
      return `type "${(action.text ?? "").slice(0, 32)}"`
    case "key":
      return `press ${(action.keys ?? []).join("+") || action.text || "key"}`
    case "scroll":
      return `scroll ${action.y ?? 0}`
    default:
      return action.action
  }
}

function RecorderProvider({ children }: { children: ReactNode }) {
  const [recording, setRecording] = useState(false)
  const [steps, setSteps] = useState<RecorderStep[]>([])
  const [botId, setBotId] = useState<string | null>(null)
  const recordingRef = useRef(false)
  recordingRef.current = recording
  const botIdRef = useRef<string | null>(null)
  botIdRef.current = botId

  const start = useCallback((id: string) => {
    // Recording a different bot starts a fresh step list.
    if (botIdRef.current && botIdRef.current !== id) setSteps([])
    setBotId(id)
    setRecording(true)
  }, [])

  const stop = useCallback(() => setRecording(false), [])

  const toggle = useCallback(
    (id: string) => {
      if (recordingRef.current) stop()
      else start(id)
    },
    [start, stop],
  )

  const addStep = useCallback((step: Omit<RecorderStep, "uid" | "at">) => {
    setSteps((prev) => [...prev, { ...step, uid: uid("step"), at: Date.now() }])
  }, [])

  const record = useCallback((action: DesktopActionInput, label?: string) => {
    if (!recordingRef.current) return
    // The wire type allows null for an absent coordinate; RecorderStep uses
    // undefined so an omitted field stays omitted when the step is serialised
    // back to /routines/teach. Collapse null here rather than widening the
    // step type - a null x would be sent as a real coordinate.
    const orNull = <T,>(v: T | null | undefined): T | undefined => v ?? undefined
    setSteps((prev) => [
      ...prev,
      {
        uid: uid("step"),
        at: Date.now(),
        type: "desktop",
        action: action.action,
        x: orNull(action.x),
        y: orNull(action.y),
        text: orNull(action.text),
        button: orNull(action.button),
        keys: orNull(action.keys),
        label: label ?? describeAction(action),
      },
    ])
  }, [])

  const removeStep = useCallback((stepUid: string) => {
    setSteps((prev) => prev.filter((s) => s.uid !== stepUid))
  }, [])

  const moveStep = useCallback((stepUid: string, delta: -1 | 1) => {
    setSteps((prev) => {
      const index = prev.findIndex((s) => s.uid === stepUid)
      if (index === -1) return prev
      const target = index + delta
      if (target < 0 || target >= prev.length) return prev
      const next = [...prev]
      const [item] = next.splice(index, 1)
      next.splice(target, 0, item)
      return next
    })
  }, [])

  const updateStep = useCallback((stepUid: string, patchValue: Partial<RecorderStep>) => {
    setSteps((prev) => prev.map((s) => (s.uid === stepUid ? { ...s, ...patchValue } : s)))
  }, [])

  const clear = useCallback(() => {
    setSteps([])
    setRecording(false)
  }, [])

  const value = useMemo<RecorderContextValue>(
    () => ({
      recording,
      steps,
      botId,
      start,
      stop,
      toggle,
      record,
      addStep,
      removeStep,
      moveStep,
      updateStep,
      clear,
    }),
    [recording, steps, botId, start, stop, toggle, record, addStep, removeStep, moveStep, updateStep, clear],
  )

  return <RecorderContext.Provider value={value}>{children}</RecorderContext.Provider>
}

export function useRecorder(): RecorderContextValue {
  const ctx = useContext(RecorderContext)
  if (!ctx) throw new Error("useRecorder must be used inside <AppProviders>")
  return ctx
}

/* --------------------------------------------------------------- composite */

/**
 * `TakeoverProvider` sits inside `AuthProvider` because it polls a protected
 * route and must not fire before there is a session, and inside
 * `ToastProvider` because resuming a run reports its outcome as a toast. It
 * wraps the recorder rather than the other way round only so the takeover state
 * outlives a recording session; neither reads the other.
 */
export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <AuthProvider>
      <ToastProvider>
        <SelectionProvider>
          <TakeoverProvider>
            <RecorderProvider>{children}</RecorderProvider>
          </TakeoverProvider>
        </SelectionProvider>
      </ToastProvider>
    </AuthProvider>
  )
}
