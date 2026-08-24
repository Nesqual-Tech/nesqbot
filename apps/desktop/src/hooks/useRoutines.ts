/** Routine list, teach-from-recording, scheduling and runs. */
import { useCallback, useState } from "react"
import * as api from "../api/endpoints"
import { errorMessage } from "../api/client"
import { useAsyncResource } from "./useAsync"
import type {
  RecordedStep,
  RecorderStep,
  Routine,
  RoutineRun,
  RoutineRunStart,
  TeachRoutineInput,
  UpdateRoutineInput,
} from "../types"

export interface RoutineRunsState {
  loading: boolean
  error: string | null
  runs: RoutineRun[]
}

export interface RoutinesApi {
  routines: Routine[]
  loading: boolean
  initialising: boolean
  error: unknown
  refetch: () => Promise<void>
  teach: (input: TeachRoutineInput) => Promise<Routine>
  update: (id: string, input: UpdateRoutineInput) => Promise<Routine>
  remove: (id: string) => Promise<void>
  runNow: (id: string) => Promise<RoutineRunStart>
  running: Record<string, boolean>
  runs: Record<string, RoutineRunsState>
  loadRuns: (id: string) => Promise<void>
}

/** Recorder rows → the `recorded_steps` wire payload. */
export function toRecordedPayload(steps: RecorderStep[]): RecordedStep[] {
  return steps.map((step) => {
    const payload: RecordedStep = {
      type: step.type || "desktop",
      action: step.action,
    }
    if (typeof step.x === "number") payload.x = Math.round(step.x)
    if (typeof step.y === "number") payload.y = Math.round(step.y)
    if (step.text !== undefined && step.text !== "") payload.text = step.text
    if (step.button) payload.button = step.button
    if (step.keys && step.keys.length > 0) payload.keys = step.keys
    if (step.label) payload.label = step.label
    return payload
  })
}

export function useRoutines(botId?: string | null): RoutinesApi {
  const resource = useAsyncResource<Routine[]>((signal) => api.listRoutines(botId ?? undefined, signal), [botId], {
    initialData: [],
  })
  const { setData } = resource
  const [running, setRunning] = useState<Record<string, boolean>>({})
  const [runs, setRuns] = useState<Record<string, RoutineRunsState>>({})

  const teach = useCallback(
    async (input: TeachRoutineInput) => {
      const routine = await api.teachRoutine(input)
      setData((prev) => [routine, ...prev.filter((r) => r.id !== routine.id)])
      return routine
    },
    [setData],
  )

  const update = useCallback(
    async (id: string, input: UpdateRoutineInput) => {
      const routine = await api.updateRoutine(id, input)
      setData((prev) => prev.map((r) => (r.id === id ? { ...r, ...routine } : r)))
      return routine
    },
    [setData],
  )

  const remove = useCallback(
    async (id: string) => {
      await api.deleteRoutine(id)
      setData((prev) => prev.filter((r) => r.id !== id))
    },
    [setData],
  )

  const loadRuns = useCallback(async (id: string) => {
    setRuns((prev) => ({ ...prev, [id]: { loading: true, error: null, runs: prev[id]?.runs ?? [] } }))
    try {
      const list = await api.listRoutineRuns(id)
      setRuns((prev) => ({ ...prev, [id]: { loading: false, error: null, runs: list ?? [] } }))
    } catch (err) {
      setRuns((prev) => ({ ...prev, [id]: { loading: false, error: errorMessage(err), runs: [] } }))
    }
  }, [])

  const runNow = useCallback(
    async (id: string) => {
      setRunning((prev) => ({ ...prev, [id]: true }))
      try {
        const started = await api.runRoutine(id)
        void loadRuns(id)
        return started
      } finally {
        setRunning((prev) => {
          const next = { ...prev }
          delete next[id]
          return next
        })
      }
    },
    [loadRuns],
  )

  return {
    routines: resource.data,
    loading: resource.loading,
    initialising: resource.initialising,
    error: resource.error,
    refetch: resource.refetch,
    teach,
    update,
    remove,
    runNow,
    running,
    runs,
    loadRuns,
  }
}
