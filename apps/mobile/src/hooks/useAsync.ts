import { useCallback, useEffect, useRef, useState } from "react"

export interface AsyncState<T> {
  data: T | null
  error: unknown
  /** First load, with nothing to show yet. */
  loading: boolean
  /** Pull-to-refresh in flight (data already on screen). */
  refreshing: boolean
  /** Re-runs showing the spinner. */
  reload: () => Promise<void>
  /** Re-runs keeping the current data on screen. */
  refresh: () => Promise<void>
  setData: (updater: T | ((previous: T | null) => T)) => void
}

/**
 * Runs an async loader and exposes the four states every screen needs.
 *
 * The loader receives an AbortSignal that fires on unmount or on a newer run, so a slow
 * response can never overwrite fresher data or set state after teardown.
 */
export function useAsync<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
  options: { enabled?: boolean } = {},
): AsyncState<T> {
  const enabled = options.enabled ?? true
  const [data, setDataState] = useState<T | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState<boolean>(enabled)
  const [refreshing, setRefreshing] = useState(false)

  const mounted = useRef(true)
  const controllerRef = useRef<AbortController | null>(null)
  const loaderRef = useRef(loader)
  loaderRef.current = loader
  const hasData = useRef(false)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      controllerRef.current?.abort()
    }
  }, [])

  const run = useCallback(async (mode: "load" | "refresh"): Promise<void> => {
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller

    if (mode === "refresh") setRefreshing(true)
    else setLoading(true)

    try {
      const result = await loaderRef.current(controller.signal)
      if (!mounted.current || controller.signal.aborted) return
      hasData.current = true
      setDataState(result)
      setError(null)
    } catch (caught) {
      if (!mounted.current || controller.signal.aborted) return
      setError(caught)
    } finally {
      if (mounted.current && !controller.signal.aborted) {
        setLoading(false)
        setRefreshing(false)
      }
    }
  }, [])

  useEffect(() => {
    if (!enabled) {
      setLoading(false)
      return
    }
    void run("load")
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, ...deps])

  const setData = useCallback((updater: T | ((previous: T | null) => T)) => {
    setDataState((previous) => (typeof updater === "function" ? (updater as (p: T | null) => T)(previous) : updater))
    hasData.current = true
  }, [])

  return {
    data,
    // Once something rendered, a later failure is surfaced inline instead of
    // replacing the screen -- callers decide via `data === null`.
    error,
    loading: loading && !hasData.current,
    refreshing,
    reload: useCallback(() => run("load"), [run]),
    refresh: useCallback(() => run("refresh"), [run]),
    setData,
  }
}
