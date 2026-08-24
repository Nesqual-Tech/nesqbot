/**
 * Push notifications for approvals -- the reason this app exists on a phone.
 *
 * Flow: ask permission -> get the Expo push token -> POST it to the API so the worker
 * can notify this device when an approval is created -> tapping the notification deep
 * links to /approvals/{id}.
 *
 * `POST /me/devices` is live and answers 201. A 404/405 is still tolerated so an older
 * API build degrades to in-app-only approvals instead of failing to start.
 */
import * as Device from "expo-device"
import * as Notifications from "expo-notifications"
import { Platform } from "react-native"
import { api } from "../api/endpoints"
import { ApiError } from "../api/client"
import { notificationTarget } from "./target"

export const APPROVALS_CHANNEL_ID = "approvals"

export type PushRegistrationStatus =
  | "registered"
  | "denied"
  | "unsupported"
  | "no-project-id"
  /**
   * A real device with permission granted, but Expo could not mint a token
   * because the build carries no APNs/FCM credentials. Distinguished from
   * `failed` because it is not a bug — it is the Apple Developer Program
   * dependency, and telling someone "could not register" when the honest answer
   * is "this build cannot receive push at all" sends them debugging the wrong
   * thing.
   */
  | "no-credentials"
  | "api-missing"
  | "failed"

export interface PushRegistrationResult {
  status: PushRegistrationStatus
  token?: string
  error?: unknown
}

/** Foreground presentation. Installed once, at app start. */
export function configureNotificationHandler(): void {
  Notifications.setNotificationHandler({
    handleNotification: async () => ({
      shouldShowAlert: true,
      shouldPlaySound: true,
      shouldSetBadge: true,
    }),
  })
}

async function ensureAndroidChannel(): Promise<void> {
  if (Platform.OS !== "android") return
  await Notifications.setNotificationChannelAsync(APPROVALS_CHANNEL_ID, {
    name: "Approvals",
    importance: Notifications.AndroidImportance.MAX,
    vibrationPattern: [0, 250, 250, 250],
    lockscreenVisibility: Notifications.AndroidNotificationVisibility.PUBLIC,
  })
}

export interface PermissionSnapshot {
  granted: boolean
  canAskAgain: boolean
  status: string
}

export async function getPermissionStatus(): Promise<PermissionSnapshot> {
  const settings = await Notifications.getPermissionsAsync()
  return {
    granted: settings.granted,
    canAskAgain: settings.canAskAgain !== false,
    status: String(settings.status),
  }
}

/**
 * Requests permission (if not already granted), fetches the Expo push token and
 * registers it with the API. Never throws -- callers get a status they can display.
 */
export async function registerForPushNotifications(projectId?: string): Promise<PushRegistrationResult> {
  try {
    if (!Device.isDevice) return { status: "unsupported" }

    await ensureAndroidChannel()

    const existing = await Notifications.getPermissionsAsync()
    let granted = existing.granted || existing.ios?.status === 3 // PROVISIONAL
    if (!granted && existing.canAskAgain !== false) {
      const requested = await Notifications.requestPermissionsAsync({
        ios: { allowAlert: true, allowBadge: true, allowSound: true },
      })
      granted = requested.granted
    }
    if (!granted) return { status: "denied" }

    if (!projectId) {
      // getExpoPushTokenAsync needs an EAS project id outside of Expo Go.
      return { status: "no-project-id" }
    }

    let token: string
    try {
      token = (await Notifications.getExpoPushTokenAsync({ projectId })).data
    } catch (caught) {
      // Expo Go and any build without push credentials fail here rather than
      // returning an empty token. Reported as its own status so the Settings
      // screen can say what is actually missing.
      const message = caught instanceof Error ? caught.message : String(caught)
      if (/credential|aps-environment|entitlement|FirebaseApp|not registered/i.test(message)) {
        return { status: "no-credentials", error: caught }
      }
      return { status: "failed", error: caught }
    }

    const platform: "ios" | "android" | "web" =
      Platform.OS === "ios" ? "ios" : Platform.OS === "android" ? "android" : "web"

    try {
      await api.devices.register({ token, platform })
    } catch (caught) {
      if (caught instanceof ApiError && (caught.status === 404 || caught.status === 405)) {
        return { status: "api-missing", token }
      }
      return { status: "failed", token, error: caught }
    }

    return { status: "registered", token }
  } catch (error) {
    return { status: "failed", error }
  }
}

export function describeRegistration(result: PushRegistrationResult): string {
  switch (result.status) {
    case "registered":
      return "This device will receive approval alerts."
    case "denied":
      return "Notifications are blocked. Enable them in system settings to get approval alerts."
    case "unsupported":
      return "Push notifications need a physical device."
    case "no-project-id":
      return "No EAS project id configured, so a push token cannot be issued."
    case "no-credentials":
      return "This build has no push credentials. It needs a development or TestFlight build signed by an Apple Developer account."
    case "api-missing":
      return "This API build has no POST /me/devices route, so the token could not be stored."
    default:
      return "Could not register this device for approval alerts."
  }
}

/**
 * Routing a notification payload lives in `./target`, which imports nothing
 * native so it can be loaded — and checked — outside a running app. Re-exported
 * here because every caller already imports from this module.
 */
export { notificationTarget, type NotificationTarget } from "./target"

/**
 * Just the approval id, for callers that only care about that.
 *
 * @deprecated Prefer {@link notificationTarget}, which also routes a takeover.
 */
export function approvalIdFromData(data: unknown): string | null {
  const target = notificationTarget(data)
  return target.kind === "approval" ? target.id : null
}

export async function clearBadge(): Promise<void> {
  try {
    await Notifications.setBadgeCountAsync(0)
  } catch {
    /* badges are best-effort */
  }
}
