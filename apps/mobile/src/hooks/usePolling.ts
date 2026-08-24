import { useCallback, useEffect, useRef } from "react"
import { AppState } from "react-native"
import { useFocusEffect } from "expo-router"

/**
 * Runs `task` every `intervalMs` while the screen is focused AND the app is in the
 * foreground. Stops on blur/background so a backgrounded phone is not polling the API.
 */
export function useFocusPolling(task: () => void | Promise<void>, intervalMs: number, enabled = true): void {
  const taskRef = useRef(task)
  taskRef.current = task

  useFocusEffect(
    useCallback(() => {
      if (!enabled || intervalMs <= 0) return undefined
      let cancelled = false

      const tick = (): void => {
        if (cancelled || AppState.currentState !== "active") return
        void taskRef.current()
      }

      const timer = setInterval(tick, intervalMs)
      return () => {
        cancelled = true
        clearInterval(timer)
      }
    }, [enabled, intervalMs]),
  )
}

/** Re-runs `task` whenever the app returns to the foreground. */
export function useAppForeground(task: () => void): void {
  const taskRef = useRef(task)
  taskRef.current = task

  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => {
      if (state === "active") taskRef.current()
    })
    return () => subscription.remove()
  }, [])
}
