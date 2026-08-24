/**
 * Theme selection.
 *
 * Every colour, size, radius and duration in the app is a CSS custom property
 * generated from the `@nesqbot/ui` palettes. Those properties are **not**
 * installed from here any more: they are emitted at build time by the
 * `nesq-design-tokens` plugin in `vite.config.ts` and bundled into the app's
 * ordinary stylesheet.
 *
 * That move is not cosmetic. This module used to build the token block as a
 * string and push it into a `document.createElement("style")`. It worked in
 * `vite dev` and in a plain browser, and it was broken in the shipped app:
 * Tauri substitutes a per-load nonce for `__TAURI_STYLE_NONCE__` in the
 * configured `style-src`, and a nonce in `style-src` makes CSP Level 3 ignore
 * `'unsafe-inline'`. A script-created `<style>` carries no nonce, so the whole
 * token block was refused — the packaged app rendered as serif text on white
 * with the layout grid still working, because `styles.css` is a `'self'`
 * stylesheet and survived. Anything that generates CSS at runtime hits the same
 * wall; generate it at build time instead.
 *
 * What is left here is the only genuinely dynamic part: which of the two
 * emitted blocks applies, selected by `data-theme` on `<html>`.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react"

export type ThemeName = "dark" | "light"

const STORAGE_KEY = "nesq.theme"

export function readStoredTheme(): ThemeName {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored === "light" || stored === "dark") return stored
    if (typeof matchMedia === "function" && matchMedia("(prefers-color-scheme: light)").matches) {
      return "light"
    }
  } catch {
    /* private mode / no DOM */
  }
  return "dark"
}

/**
 * Select a theme. Called once from `main.tsx` before React renders, so the
 * first paint is already in the right scheme.
 */
export function applyTheme(theme: ThemeName): void {
  if (typeof document === "undefined") return
  document.documentElement.dataset.theme = theme
}

interface ThemeContextValue {
  theme: ThemeName
  setTheme: (theme: ThemeName) => void
  toggleTheme: () => void
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<ThemeName>(() => readStoredTheme())

  useEffect(() => {
    applyTheme(theme)
    try {
      localStorage.setItem(STORAGE_KEY, theme)
    } catch {
      /* ignore */
    }
  }, [theme])

  const setTheme = useCallback((next: ThemeName) => setThemeState(next), [])
  const toggleTheme = useCallback(() => setThemeState((t) => (t === "dark" ? "light" : "dark")), [])

  const value = useMemo<ThemeContextValue>(() => ({ theme, setTheme, toggleTheme }), [theme, setTheme, toggleTheme])
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error("useTheme must be used inside <ThemeProvider>")
  return ctx
}
