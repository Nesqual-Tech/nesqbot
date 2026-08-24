import { useMemo, type ReactNode } from "react"
import { RefreshControl, ScrollView, StyleSheet, View, type StyleProp, type ViewStyle } from "react-native"
import { useSafeAreaInsets } from "react-native-safe-area-context"
import { useTheme, type Theme } from "../theme"
import { ErrorView } from "./ErrorView"
import { Spinner } from "./Spinner"

export interface ScreenProps {
  children: ReactNode
  /** Wraps the content in a ScrollView. Set false for FlatList-owned screens. */
  scroll?: boolean
  /** Shown instead of children on the first load. */
  loading?: boolean
  loadingLabel?: string
  /** Shown instead of children when set and there is nothing cached to display. */
  error?: unknown
  errorTitle?: string
  onRetry?: () => void
  refreshing?: boolean
  onRefresh?: () => void
  /** Pinned below the scroll area (composer, action bar...). */
  footer?: ReactNode
  padded?: boolean
  contentContainerStyle?: StyleProp<ViewStyle>
  style?: StyleProp<ViewStyle>
  testID?: string
}

/**
 * Every screen's shell: themed background, safe-area padding, and the three
 * non-happy states (loading / error+retry / pull-to-refresh) in one place so no screen
 * can accidentally render blank.
 */
export function Screen({
  children,
  scroll = true,
  loading = false,
  loadingLabel,
  error,
  errorTitle,
  onRetry,
  refreshing = false,
  onRefresh,
  footer,
  padded = true,
  contentContainerStyle,
  style,
  testID,
}: ScreenProps): JSX.Element {
  const theme = useTheme()
  const insets = useSafeAreaInsets()
  const styles = useMemo(() => makeStyles(theme), [theme])

  const bottomPad = Math.max(insets.bottom, theme.spacing.lg)

  const hasError = error !== undefined && error !== null
  const showingFallback = loading || hasError

  let body: ReactNode
  if (loading) {
    body = <Spinner label={loadingLabel ?? "Loading"} />
  } else if (hasError) {
    body = <ErrorView error={error} onRetry={onRetry} title={errorTitle} />
  } else {
    body = children
  }

  const refreshControl = onRefresh ? (
    <RefreshControl
      refreshing={refreshing}
      onRefresh={onRefresh}
      tintColor={theme.palette.accent}
      colors={[theme.palette.accent]}
      progressBackgroundColor={theme.palette.surface}
    />
  ) : undefined

  return (
    <View style={[styles.root, style]} testID={testID}>
      {scroll ? (
        <ScrollView
          style={styles.flex}
          contentContainerStyle={[
            styles.content,
            padded && styles.padded,
            { paddingBottom: footer ? theme.spacing.lg : bottomPad },
            showingFallback && styles.centered,
            contentContainerStyle,
          ]}
          keyboardShouldPersistTaps="handled"
          keyboardDismissMode="on-drag"
          refreshControl={refreshControl}
        >
          {body}
        </ScrollView>
      ) : (
        <View style={[styles.flex, padded && styles.padded]}>{body}</View>
      )}
      {footer ? <View style={[styles.footer, { paddingBottom: bottomPad }]}>{footer}</View> : null}
    </View>
  )
}

function makeStyles(theme: Theme) {
  const { palette, spacing } = theme
  return StyleSheet.create({
    root: { flex: 1, backgroundColor: palette.bg },
    flex: { flex: 1 },
    content: { flexGrow: 1, gap: spacing.md },
    padded: { paddingHorizontal: spacing.lg, paddingTop: spacing.lg },
    centered: { justifyContent: "center" },
    footer: {
      borderTopWidth: StyleSheet.hairlineWidth,
      borderTopColor: palette.border,
      backgroundColor: palette.surface,
      paddingHorizontal: spacing.md,
      paddingTop: spacing.md,
    },
  })
}
