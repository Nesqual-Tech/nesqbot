/**
 * Loading/error/refetch plumbing shared by every data hook.
 * Deliberately tiny — no cache, no dedupe, no dependencies.
 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type DependencyList,
  type Dispatch,
  type SetStateAction,
} from "react"
import { isAbortError } from "../api/client"

export interface AsyncResource<T> {
  data: T
  error: unknown
  loading: boolean
  /** True until the first load settles — drives skeletons vs. spinners. */
  initialising: boolean
  refetch: () => Promise<void>
  setData: Dispatch<SetStateAction<T>>
  setError: Dispatch<SetStateAction<unknown>>
}

export interface AsyncOptions<T> {
  initialData: T
  enabled?: boolean
  /** Poll interval in ms; 0/undefined disables polling. */
  pollMs?: number
}

export function useAsyncResource<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  deps: DependencyList,
  options: AsyncOptions<T>,
): AsyncResource<T> {
  const { initialData, enabled = true, pollMs } = options
  const [data, setData] = useState<T>(initialData)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState<boolean>(enabled)
  const [initialising, setInitialising] = useState<boolean>(enabled)

  const loaderRef = useRef(loader)
  loaderRef.current = loader
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const run = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    try {
      const controller = signal ? null : new AbortController()
      const result = await loaderRef.current(signal ?? controller!.signal)
      if (!mounted.current || signal?.aborted) return
      setData(result)
      setError(null)
    } catch (err) {
      if (!mounted.current || isAbortError(err)) return
      setError(err)
    } finally {
      if (mounted.current && !signal?.aborted) {
        setLoading(false)
        setInitialising(false)
      }
    }
  }, [])

  useEffect(() => {
    if (!enabled) {
      setLoading(false)
      setInitialising(false)
      return
    }
    const controller = new AbortController()
    void run(controller.signal)
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, enabled, run])

  useEffect(() => {
    if (!enabled || !pollMs) return
    const timer = setInterval(() => {
      void run()
    }, pollMs)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, enabled, pollMs, run])

  const refetch = useCallback(async () => {
    if (!enabled) return
    await run()
  }, [enabled, run])

  return { data, error, loading, initialising, refetch, setData, setError }
}

/**
 * Wrapper for one-shot mutations: tracks `pending` and the last error so
 * buttons can disable themselves without every component re-implementing it.
 */
export function useMutation<Args extends unknown[], Result>(
  fn: (...args: Args) => Promise<Result>,
): {
  run: (...args: Args) => Promise<Result | undefined>
  pending: boolean
  error: unknown
  reset: () => void
} {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const fnRef = useRef(fn)
  fnRef.current = fn
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const run = useCallback(async (...args: Args) => {
    setPending(true)
    setError(null)
    try {
      return await fnRef.current(...args)
    } catch (err) {
      if (mounted.current) setError(err)
      throw err
    } finally {
      if (mounted.current) setPending(false)
    }
  }, [])

  const reset = useCallback(() => setError(null), [])

  return { run, pending, error, reset }
}
