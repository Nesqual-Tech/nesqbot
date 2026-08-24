import { useEffect, useRef, useState } from "react"
import * as Notifications from "expo-notifications"
import { useRouter } from "expo-router"
import { getEasProjectId } from "../api/client"
import { usePreferences } from "../storage/preferences"
import { clearBadge, notificationTarget, registerForPushNotifications, type PushRegistrationResult } from "./index"
import { refreshInbox } from "../state/inbox"

/**
 * Registers this device for approval pushes and deep-links notification taps to
 * `/approvals/{id}` -- including a tap that cold-started the app.
 *
 * Mounted once, from the root layout, and only while signed in.
 */
export function useApprovalNotifications(enabled: boolean): PushRegistrationResult | null {
  const router = useRouter()
  const { notificationsEnabled } = usePreferences()
  const [registration, setRegistration] = useState<PushRegistrationResult | null>(null)
  const handledColdStart = useRef(false)

  const active = enabled && notificationsEnabled

  useEffect(() => {
    if (!active) return
    let cancelled = false
    void registerForPushNotifications(getEasProjectId()).then((result) => {
      if (!cancelled) setRegistration(result)
    })
    return () => {
      cancelled = true
    }
  }, [active])

  useEffect(() => {
    if (!active) return undefined

    const open = (data: unknown): void => {
      void clearBadge()
      // Pull the queue in behind the tap. The notification is the fast path,
      // but whatever else piled up while the phone was locked should already be
      // there when the person backs out of the item they opened.
      void refreshInbox().catch(() => undefined)

      const target = notificationTarget(data)
      switch (target.kind) {
        case "approval":
          router.push({ pathname: "/approvals/[id]", params: { id: target.id } })
          break
        case "takeover":
          router.push({ pathname: "/takeover/[runId]", params: { runId: target.runId } })
          break
        default:
          // The inbox is the tab index, so an unrecognised payload lands on the
          // queue rather than nowhere.
          router.push("/")
      }
    }

    // Tap while the app was running.
    const responseSub = Notifications.addNotificationResponseReceivedListener((response) => {
      open(response.notification.request.content.data)
    })

    // Received in the foreground: no navigation, but keep the badge honest.
    const receivedSub = Notifications.addNotificationReceivedListener(() => {
      /* the foreground handler in configureNotificationHandler presents it */
    })

    // Tap that launched the app from cold.
    if (!handledColdStart.current) {
      handledColdStart.current = true
      void Notifications.getLastNotificationResponseAsync().then((response) => {
        if (response) open(response.notification.request.content.data)
      })
    }

    return () => {
      responseSub.remove()
      receivedSub.remove()
    }
  }, [active, router])

  return registration
}
