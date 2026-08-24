import { useEffect, useState } from "react"
import { View } from "react-native"
import { Stack, useRootNavigationState, useRouter, useSegments } from "expo-router"
import { StatusBar } from "expo-status-bar"
import { SafeAreaProvider } from "react-native-safe-area-context"
import { brandNavy } from "@nesqbot/ui"
import { AuthProvider, useAuth } from "../src/auth"
import { ThemeProvider, useTheme } from "../src/theme"
import { hydratePreferences } from "../src/storage/preferences"
import { configureNotificationHandler } from "../src/notifications"
import { useApprovalNotifications } from "../src/notifications/useApprovalNotifications"

// Foreground presentation must be configured before any notification can arrive.
configureNotificationHandler()

export default function RootLayout(): JSX.Element {
  // Preferences hold the theme and the API URL override, so nothing renders until the
  // stored snapshot is in memory -- otherwise the app flashes the wrong theme and the
  // first request goes to the wrong host.
  const [hydrated, setHydrated] = useState(false)

  useEffect(() => {
    let cancelled = false
    void hydratePreferences().finally(() => {
      if (!cancelled) setHydrated(true)
    })
    return () => {
      cancelled = true
    }
  }, [])

  if (!hydrated) {
    return <View style={{ flex: 1, backgroundColor: brandNavy }} />
  }

  return (
    <SafeAreaProvider>
      <ThemeProvider>
        <AuthProvider>
          <RootNavigator />
        </AuthProvider>
      </ThemeProvider>
    </SafeAreaProvider>
  )
}

function RootNavigator(): JSX.Element {
  const theme = useTheme()
  const { status } = useAuth()
  const router = useRouter()
  const segments = useSegments()
  const navigationState = useRootNavigationState()

  useApprovalNotifications(status === "authenticated")

  // Redirect guard. Waits for the root navigator to report a key, otherwise the
  // navigation happens before the tree is mounted and is dropped.
  useEffect(() => {
    if (!navigationState?.key) return
    if (status === "loading") return

    const onLoginScreen = segments[0] === "login"
    if (status === "unauthenticated" && !onLoginScreen) {
      router.replace("/login")
    } else if (status === "authenticated" && onLoginScreen) {
      router.replace("/")
    }
  }, [navigationState?.key, status, segments, router])

  return (
    <>
      <StatusBar style={theme.scheme === "dark" ? "light" : "dark"} />
      <Stack
        screenOptions={{
          headerStyle: { backgroundColor: theme.palette.surface },
          headerTintColor: theme.palette.text,
          headerTitleStyle: { color: theme.palette.text },
          headerShadowVisible: false,
          contentStyle: { backgroundColor: theme.palette.bg },
        }}
      >
        <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
        <Stack.Screen name="login" options={{ headerShown: false, animation: "fade" }} />
        <Stack.Screen name="chat/[botId]" options={{ title: "Chat" }} />
        <Stack.Screen name="desktop/[botId]" options={{ title: "Bot Desktop" }} />
        <Stack.Screen name="approvals/[id]" options={{ title: "Approval" }} />
        {/* Presented as a card rather than a push: a handover is a task you
            step into and hand back, and the sheet gesture says that. */}
        <Stack.Screen name="takeover/[runId]" options={{ title: "Needs you", presentation: "card" }} />
        <Stack.Screen name="work-items/[id]" options={{ title: "Work item" }} />
        <Stack.Screen name="+not-found" options={{ title: "Not found" }} />
      </Stack>
    </>
  )
}
