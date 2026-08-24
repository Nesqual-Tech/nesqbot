/**
 * The tab bar, and the one background job that keeps it honest.
 *
 * Tab order is the app's argument about what a phone is for. **Inbox is first**
 * because unblocking a stopped agent is the thing this product does better from
 * a pocket than from a desk; the roster, the work ledger and the spend are
 * context for that decision, in decreasing order of how often you need them.
 * Five tabs is the iOS limit before the system collapses the rest into "More",
 * so this is exactly full — anything new displaces something.
 */
import { useEffect } from "react"
import { AppState, StyleSheet, Text } from "react-native"
import { Tabs } from "expo-router"
import { useAuth } from "../../src/auth"
import { useTheme } from "../../src/theme"
import { refreshInbox, useInboxCount } from "../../src/state/inbox"

/**
 * How often the badge refreshes while the app is open but the inbox tab is not.
 *
 * The inbox screen polls faster when it is actually showing; this is the
 * background floor, and it stops entirely when the app leaves the foreground.
 */
const BADGE_POLL_MS = 30000

function TabGlyph({ glyph, color }: { glyph: string; color: string }): JSX.Element {
  // `allowFontScaling={false}`: these are icons drawn with glyphs, and at the
  // largest Dynamic Type settings a scaled glyph overflows the tab bar's fixed
  // height and clips the label underneath it. The label itself still scales.
  return (
    <Text style={[styles.glyph, { color }]} allowFontScaling={false}>
      {glyph}
    </Text>
  )
}

export default function TabsLayout(): JSX.Element {
  const theme = useTheme()
  const { status } = useAuth()
  const waiting = useInboxCount()
  const authenticated = status === "authenticated"

  /**
   * Keeps the badge live wherever the user is in the app.
   *
   * Errors are swallowed on purpose: a badge that could not refresh must never
   * surface as a crash or an alert. The screens show the failure; the tab bar
   * just goes stale. `refreshInbox` already resolves rather than rejects, so
   * the catch here is belt and braces against a future change.
   */
  useEffect(() => {
    if (!authenticated) return undefined
    let cancelled = false

    const tick = (): void => {
      if (cancelled || AppState.currentState !== "active") return
      void refreshInbox().catch(() => undefined)
    }

    tick()
    const timer = setInterval(tick, BADGE_POLL_MS)
    const subscription = AppState.addEventListener("change", (next) => {
      if (next === "active") tick()
    })

    return () => {
      cancelled = true
      clearInterval(timer)
      subscription.remove()
    }
  }, [authenticated])

  return (
    <Tabs
      screenOptions={{
        headerStyle: { backgroundColor: theme.palette.surface },
        headerTintColor: theme.palette.text,
        headerShadowVisible: false,
        tabBarActiveTintColor: theme.palette.accent,
        tabBarInactiveTintColor: theme.palette.textDim,
        tabBarStyle: {
          backgroundColor: theme.palette.surface,
          borderTopColor: theme.palette.border,
        },
        tabBarBadgeStyle: {
          backgroundColor: theme.palette.danger,
          color: theme.palette.text,
          fontSize: 11,
        },
      }}
    >
      <Tabs.Screen
        name="index"
        options={{
          title: "Inbox",
          tabBarLabel: "Inbox",
          // The badge counts held approvals *and* runs parked on a human: from
          // here they are one thing — something stopped and only you restart it.
          tabBarBadge: waiting > 0 ? waiting : undefined,
          tabBarAccessibilityLabel:
            waiting > 0
              ? `Inbox, ${waiting} ${waiting === 1 ? "item" : "items"} waiting for you`
              : "Inbox, nothing waiting",
          tabBarIcon: ({ color }) => <TabGlyph glyph="✓" color={color} />,
        }}
      />
      <Tabs.Screen
        name="bots"
        options={{
          title: "Bots",
          tabBarAccessibilityLabel: "Bots",
          tabBarIcon: ({ color }) => <TabGlyph glyph="◉" color={color} />,
        }}
      />
      <Tabs.Screen
        name="work"
        options={{
          title: "Work",
          tabBarAccessibilityLabel: "Work items and handovers",
          tabBarIcon: ({ color }) => <TabGlyph glyph="≡" color={color} />,
        }}
      />
      <Tabs.Screen
        name="usage"
        options={{
          title: "Usage",
          tabBarAccessibilityLabel: "Usage and budgets",
          tabBarIcon: ({ color }) => <TabGlyph glyph="◑" color={color} />,
        }}
      />
      <Tabs.Screen
        name="settings"
        options={{
          title: "Settings",
          tabBarAccessibilityLabel: "Settings",
          tabBarIcon: ({ color }) => <TabGlyph glyph="⚙" color={color} />,
        }}
      />
    </Tabs>
  )
}

const styles = StyleSheet.create({
  glyph: { fontSize: 20, lineHeight: 24 },
})
