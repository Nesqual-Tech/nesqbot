/**
 * Composition only. Every behaviour lives in `hooks/`, `state/` or a component.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { REQUIRES_SIGN_IN } from "./api/client"
import { getHealth } from "./api/endpoints"
import { ChatPane } from "./components/ChatPane"
import { ConversationList } from "./components/ConversationList"
import { DesktopPane } from "./components/DesktopPane"
import { ErrorBoundary } from "./components/ErrorBoundary"
import { Icon } from "./components/Icon"
import { PaneSplitter } from "./components/PaneSplitter"
import { SessionBootScreen, SignInScreen } from "./components/SignInScreen"
import { SettingsSheet, type SettingsSection } from "./components/SettingsSheet"
import { SetupWizard } from "./components/SetupWizard"
import { TakeoverBeacon } from "./components/TakeoverBeacon"
import { ToastViewport } from "./components/Toast"
import { useApprovals } from "./hooks/useApprovals"
import { useAsyncResource } from "./hooks/useAsync"
import { useBots } from "./hooks/useBots"
import { useDesktopLayout } from "./hooks/useDesktopLayout"
import { useThreads } from "./hooks/useThreads"
import { useUsage } from "./hooks/useUsage"
import { completeEntraRedirect, useAuth } from "./auth"
import { cx, usd } from "./lib/format"
import { dur, ease, gsap, useGSAP } from "./lib/motion"
import { onShellEvent, parseDeepLink } from "./lib/tauri"
import { CommandPalette, type Command } from "./components/CommandPalette"
import { AppProviders, useSelection, useToast } from "./state/AppState"
import { hasCompletedSetup, resetSetupCompletion } from "./state/setup"
import { useTakeover } from "./state/takeover"
import { ThemeProvider, useTheme } from "./state/theme"
import type { TakeoverRequest } from "./lib/takeover"
import type { HealthOut, Thread } from "./types"

export function App() {
  return (
    // `ThemeProvider` outside everything: the wizard renders themed UI
    // (`NesqualLockup` et al.) before setup is done, so theme — pure
    // localStorage/DOM state, no API side effects — has to be safe to mount
    // unconditionally.
    //
    // `AppProviders` outside `SetupGate`, not inside it: steps 2 and 3 of the
    // wizard (providers, per-bot model) call real protected endpoints
    // (`GET /bots/providers`, `GET /bots`, `PATCH /bots/{id}`) and need a
    // session to do it — see `SignInStep` in `SetupWizard.tsx`. This is safe
    // to mount before setup completes: `AuthProvider.restore()` only ever
    // makes a network call when a session token is already in the OS
    // credential store, and the rest of `AppProviders` (toast, selection,
    // takeover, recorder) is inert local state. What must not mount early is
    // `Shell` — it starts polling bots/approvals/health immediately — and
    // `SetupGate` still keeps that from happening: `AuthGate` (which renders
    // `Shell`) only appears once setup is `done`.
    <ThemeProvider>
      <AppProviders>
        <SetupGate>
          <AuthGate />
        </SetupGate>
      </AppProviders>
    </ThemeProvider>
  )
}

/**
 * Runs the setup wizard before `Shell` (and its immediate API polling) can
 * mount.
 *
 * `key={resetCount}` remounts the wizard (fresh internal step state) when
 * `openSetup` reopens it from the running app — see the General settings page.
 */
function SetupGate({ children }: { children: ReactNode }) {
  const [done, setDone] = useState(hasCompletedSetup())
  const [resetCount, setResetCount] = useState(0)

  useEffect(() => {
    const reopen = () => {
      resetSetupCompletion()
      setResetCount((n) => n + 1)
      setDone(false)
    }
    window.addEventListener("nesq:open-setup", reopen)
    return () => window.removeEventListener("nesq:open-setup", reopen)
  }, [])

  if (!done) return <SetupWizard key={resetCount} onDone={() => setDone(true)} />
  return <>{children}</>
}

/** Reopens the setup wizard from anywhere inside the shell. */
export function openSetup(): void {
  window.dispatchEvent(new Event("nesq:open-setup"))
}

/**
 * Decides whether the workspace may mount at all.
 *
 * `Shell` starts polling bots, approvals and health the moment it renders, and
 * every one of those endpoints is protected. Against the live API those calls
 * can only 401 until a session exists, so mounting the shell first and letting
 * each panel discover the failure independently is both a bad first impression
 * and pointless traffic. The gate keeps it simple: no session, no requests.
 *
 * It only gates where a session is genuinely required. A development build
 * still carries the `X-Nesq-Dev` bypass and works signed out exactly as it
 * always has — `REQUIRES_SIGN_IN` is the same build-time flag that decides
 * whether the bypass header is sent, so the two can never disagree.
 *
 * A build with no Entra registration falls through to the shell as well: a
 * sign-in screen with no sign-in on it is a dead end, and the shell's own error
 * states at least say what is wrong.
 */
function AuthGate() {
  const { status, entraAvailable } = useAuth()

  if (!REQUIRES_SIGN_IN || !entraAvailable) return <Shell />
  if (status === "loading") return <SessionBootScreen />
  if (status === "unauthenticated") return <SignInScreen />
  return <Shell />
}

/**
 * Where a deep link's `tab` id lands now that the sections live in the
 * settings sheet. `nesqbot://tab/usage` still has to go somewhere, and the
 * links are printed in notifications that outlive any one build.
 */
const DEEP_LINK_SECTIONS: Record<string, SettingsSection> = {
  approvals: "approvals",
  integrations: "connectors",
  connectors: "connectors",
  routines: "routines",
  usage: "usage",
  audit: "audit",
  knowledge: "knowledge",
  builder: "profile",
  profile: "profile",
  work: "work",
  models: "models",
  general: "general",
}

function Shell() {
  const toast = useToast()
  const { theme, toggleTheme } = useTheme()
  const { activeBotId, setActiveBotId, activeThreadId, setActiveThreadId, focusApprovalId, setFocusApprovalId } =
    useSelection()

  const bots = useBots()
  const threads = useThreads()
  const approvals = useApprovals("pending")
  const handoff = useTakeover()
  const usage = useUsage(1)
  const [usageRefresh, setUsageRefresh] = useState(0)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("general")
  // Width, maximised-or-not, open-or-closed and scale for the Agent Computer
  // pane, remembered across restarts. Owned here because the splitter and the
  // pane are siblings in the shell grid, and the width is a grid track.
  const desktopLayout = useDesktopLayout()

  const health = useAsyncResource<HealthOut | null>((signal) => getHealth(signal), [], {
    initialData: null,
    pollMs: 30_000,
  })

  const activeBot = useMemo(() => bots.bots.find((bot) => bot.id === activeBotId) ?? null, [bots.bots, activeBotId])

  // First bot wins until the user picks one.
  useEffect(() => {
    if (!activeBotId && bots.bots.length > 0) setActiveBotId(bots.bots[0].id)
  }, [activeBotId, bots.bots, setActiveBotId])

  const openSettings = useCallback((section: SettingsSection) => {
    setSettingsSection(section)
    setSettingsOpen(true)
  }, [])

  const openApproval = useCallback(
    (approvalId: string) => {
      setFocusApprovalId(approvalId)
      openSettings("approvals")
      void approvals.refetch()
    },
    [setFocusApprovalId, openSettings, approvals],
  )

  const onApprovalRaised = useCallback(
    (approvalId: string, title: string) => {
      toast.warning("Approval required", title)
      if (approvalId) setFocusApprovalId(approvalId)
      void approvals.refetch()
    },
    [toast, approvals, setFocusApprovalId],
  )

  const onTurnComplete = useCallback(() => {
    void approvals.refetch()
    void usage.refetch()
    setUsageRefresh((value) => value + 1)
  }, [approvals, usage])

  /* ------------------------------------------------------------------ *
   * Picking a conversation
   *
   * The list selects a *thread*, and the bot follows from it — the opposite of
   * the old rail, which selected a bot and let `ensureThreadForBot` guess at
   * the conversation. The guess is what made group threads unfindable: any
   * thread the bot was in would do, so the one you were last in was as likely
   * to be skipped as opened.
   * ------------------------------------------------------------------ */

  const selectThread = useCallback(
    (thread: Thread) => {
      setActiveThreadId(thread.id)
      const seated = thread.bot_ids ?? []
      // Keep the current bot if it is in this room — in a group, the person
      // answering is whoever was addressed, not whoever sorts first.
      if (seated.length > 0 && !seated.includes(activeBotId ?? "")) setActiveBotId(seated[0])
    },
    [setActiveThreadId, setActiveBotId, activeBotId],
  )

  const startWithBot = useCallback(
    (bot: { id: string; name: string; slug: string }) => {
      setActiveBotId(bot.id)
      void threads
        .ensureThreadForBot(bot as Parameters<typeof threads.ensureThreadForBot>[0])
        .then((thread) => setActiveThreadId(thread.id))
        .catch((err: unknown) => toast.error("Could not open a conversation", err instanceof Error ? err.message : undefined))
    },
    [threads, setActiveBotId, setActiveThreadId, toast],
  )

  const startGroup = useCallback(
    (botIds: string[]) => {
      const names = botIds.map((id) => bots.byId[id]?.name).filter(Boolean)
      void threads
        .createThread({ bot_ids: botIds, title: names.join(", ") || "Group" })
        .then((thread) => {
          setActiveThreadId(thread.id)
          if (botIds[0]) setActiveBotId(botIds[0])
        })
        .catch((err: unknown) => toast.error("Could not start the group", err instanceof Error ? err.message : undefined))
    },
    [threads, bots.byId, setActiveThreadId, setActiveBotId, toast],
  )

  /* ------------------------------------------------------------------ *
   * Human handoff
   *
   * The state lives in `state/takeover`; what belongs here is the *staging* —
   * which bot is selected, whether the pane is open, and whether it is
   * covering the window. Those are shell concerns and the provider deliberately
   * knows nothing about them.
   * ------------------------------------------------------------------ */

  const setExpanded = desktopLayout.setExpanded
  const setDesktopOpen = desktopLayout.setOpen

  /*
   * Maximising for a takeover must not become the person's saved preference.
   *
   * `useDesktopLayout` persists `expanded` and `open` on purpose — somebody who
   * works in takeover should not re-open the pane every morning. But that is
   * about *their* choice. A run parking on a login is not a choice, and letting
   * it write the preference means one interruption silently changes how the app
   * opens forever. So the pre-takeover values are held here and put back once
   * nothing is waiting. Refs, so toggling the pane does not rebuild
   * `focusTakeover` and re-run the effect that calls it.
   */
  const mainRef = useRef<HTMLElement | null>(null)

  const expandedRef = useRef(desktopLayout.expanded)
  expandedRef.current = desktopLayout.expanded
  const openRef = useRef(desktopLayout.open)
  openRef.current = desktopLayout.open
  const layoutBeforeTakeover = useRef<{ open: boolean; expanded: boolean } | null>(null)

  /**
   * Put one parked run in front of the person: its bot, its thread, and the
   * computer maximised so the screen they have to sign in on is the biggest
   * thing on the display. `TakeoverCard` renders itself once the pane's bot
   * matches.
   */
  const focusTakeover = useCallback(
    (request: TakeoverRequest) => {
      handoff.undismiss(request.runId)
      if (request.botId) setActiveBotId(request.botId)
      if (request.threadId) setActiveThreadId(request.threadId)
      if (layoutBeforeTakeover.current === null) {
        layoutBeforeTakeover.current = { open: openRef.current, expanded: expandedRef.current }
      }
      setDesktopOpen(true)
      setExpanded(true)
    },
    [handoff, setActiveBotId, setActiveThreadId, setDesktopOpen, setExpanded],
  )

  const outstandingTakeovers = handoff.requests.length
  useEffect(() => {
    if (outstandingTakeovers > 0) return
    const previous = layoutBeforeTakeover.current
    layoutBeforeTakeover.current = null
    if (!previous) return
    // Only ever put back what was there. If they were already maximised, or
    // maximised it themselves while dealing with this, that is their layout.
    if (!previous.expanded) setExpanded(false)
    if (!previous.open) setDesktopOpen(false)
  }, [outstandingTakeovers, setExpanded, setDesktopOpen])

  /*
   * Only a *live* arrival gets to grab the window — `unpresented` never yields
   * a run recovered from the parked-run poll. Otherwise every cold start with
   * an outstanding handoff would open maximised onto a desktop pane the person
   * did not ask for. Recovered runs get the beacon instead, which is loud
   * enough to find and quiet enough to ignore.
   */
  const unpresented = handoff.unpresented
  const markPresented = handoff.markPresented
  useEffect(() => {
    if (!unpresented) return
    markPresented(unpresented.runId)
    focusTakeover(unpresented)
    toast.warning(`${unpresented.botName} needs you`, unpresented.whatYouNeed)
  }, [unpresented, markPresented, focusTakeover, toast])

  /** Parked runs that are not already on screen as a card in the desktop pane. */
  const offscreenTakeovers = useMemo(
    () => handoff.requests.filter((r) => r.botId !== activeBotId || handoff.dismissed.has(r.runId)),
    [handoff.requests, handoff.dismissed, activeBotId],
  )

  const takeoverName = useCallback(
    (request: TakeoverRequest) => bots.bots.find((b) => b.id === request.botId)?.name ?? request.botName,
    [bots.bots],
  )

  // Native shell: deep links (nesqbot://approval/<id>) and window-menu commands.
  useEffect(() => {
    const offDeepLink = onShellEvent<string[] | string>("deep-link", (payload) => {
      const urls = Array.isArray(payload) ? payload : [payload]
      for (const url of urls) {
        const target = parseDeepLink(url)
        if (target.kind === "approval" && target.id) {
          openApproval(target.id)
        } else if (target.kind === "thread" && target.id) {
          setSettingsOpen(false)
          setActiveThreadId(target.id)
        } else if (target.kind === "bot" && target.id) {
          setSettingsOpen(false)
          setActiveBotId(target.id)
        } else if (target.kind === "tab" && target.id) {
          const section = DEEP_LINK_SECTIONS[target.id]
          if (section) openSettings(section)
          else setSettingsOpen(false)
        } else if (target.kind === "auth") {
          // An OAuth redirect. Never echo `target.raw` or `target.params` —
          // they carry a one-time authorization code. `completeEntraRedirect`
          // matches `state` and either redeems the code or ignores the link;
          // the sign-in promise in `auth/` reports the outcome, so there is
          // nothing to say here either way.
          completeEntraRedirect(target.params)
        } else {
          toast.info("Unrecognised link", target.raw)
        }
      }
    })

    const offMenu = onShellEvent<string>("menu", (command) => {
      switch (command) {
        case "new-thread":
          setSettingsOpen(false)
          setActiveThreadId(null)
          break
        case "toggle-theme":
          toggleTheme()
          break
        case "open-approvals":
          openSettings("approvals")
          break
        case "open-integrations":
          openSettings("connectors")
          break
        case "open-routines":
          openSettings("routines")
          break
        case "open-usage":
          openSettings("usage")
          break
        case "reload":
          location.reload()
          break
        default:
          break
      }
    })

    return () => {
      offDeepLink()
      offMenu()
    }
  }, [openApproval, openSettings, setActiveBotId, setActiveThreadId, toggleTheme, toast])

  /* ------------------------------------------------------------------ *
   * Keyboard
   *
   * One listener, because two listeners racing for the same modifier is how
   * shortcuts start swallowing each other.
   *
   * The rules it follows:
   *
   *  - Every binding is Ctrl/Cmd-based. A bare-letter shortcut in an app whose
   *    main surface is a text box is a trap.
   *  - The typing guard is applied **per binding**, not to the whole handler.
   *    A blanket "do nothing while a field has focus" was the first version and
   *    it was wrong in practice: the composer takes focus on load, so the app
   *    opened with every shortcut already disabled.
   *
   *    So the test is whether the chord could plausibly be text editing.
   *    Ctrl/Cmd+comma cannot be; neither can Ctrl/Cmd+Shift+D. Those work
   *    everywhere. `Ctrl+N` is a real readline binding people use inside
   *    textareas, so that one still stands down while a field has focus.
   *  - `Ctrl/Cmd+Shift+D` for the computer pane rather than F11, which the
   *    WebView reserves.
   * ------------------------------------------------------------------ */
  const toggleDesktopOpen = desktopLayout.toggleOpen
  const [paletteOpen, setPaletteOpen] = useState(false)

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const mod = event.ctrlKey || event.metaKey
      if (!mod || event.altKey) return

      const key = event.key.toLowerCase()
      const target = event.target as HTMLElement | null
      const typing =
        target?.isContentEditable === true ||
        target?.tagName === "INPUT" ||
        target?.tagName === "TEXTAREA" ||
        target?.tagName === "SELECT"

      // Reachable from anywhere, including a half-written message.
      if (key === "k") {
        event.preventDefault()
        setPaletteOpen((prev) => !prev)
        return
      }

      if (event.shiftKey) {
        if (key === "d") {
          event.preventDefault()
          toggleDesktopOpen()
        }
        return
      }

      if (key === ",") {
        event.preventDefault()
        openSettings("general")
        return
      }

      // The one binding that stands down for a text field: Ctrl+N is a real
      // readline "next line" chord and people use it inside textareas.
      if (key === "n" && !typing) {
        event.preventDefault()
        setSettingsOpen(false)
        setActiveThreadId(null)
      }
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [toggleDesktopOpen, openSettings, setActiveThreadId])

  /*
   * What the palette can reach.
   *
   * Teammates first, because "talk to Vesna" is the most common intent in the
   * product and because it is the list that changes. Then the conversations
   * themselves, then the settings sections, then the handful of window-level
   * things. Nothing here decides an approval — see the note at the top of
   * `CommandPalette`.
   */
  const commands = useMemo<Command[]>(() => {
    const items: Command[] = bots.bots.map((bot) => ({
      id: `bot-${bot.id}`,
      label: bot.name,
      detail: bot.role || undefined,
      group: "Teammates",
      glyph: "bot",
      keywords: bot.slug,
      run: () => {
        setSettingsOpen(false)
        startWithBot(bot)
      },
    }))

    for (const thread of threads.threads.slice(0, 12)) {
      const seated = (thread.bot_ids ?? []).map((id) => bots.byId[id]?.name).filter(Boolean)
      items.push({
        id: `thread-${thread.id}`,
        label: thread.title || "Untitled conversation",
        detail: seated.length > 1 ? seated.join(", ") : undefined,
        group: "Conversations",
        glyph: "chat",
        run: () => {
          setSettingsOpen(false)
          selectThread(thread)
        },
      })
    }

    const sections: Array<[SettingsSection, string]> = [
      ["general", "General"],
      ["models", "Models"],
      ["approvals", "Approvals"],
      ["connectors", "Connectors"],
      ["routines", "Routines"],
      ["usage", "Usage"],
      ["profile", "Profile"],
      ["work", "Work"],
      ["audit", "Audit"],
      ["knowledge", "Knowledge"],
    ]
    for (const [section, label] of sections) {
      items.push({
        id: `settings-${section}`,
        label,
        detail:
          section === "approvals" && approvals.pendingCount > 0
            ? `${approvals.pendingCount} waiting`
            : "Settings",
        group: "Go to",
        glyph: "sliders",
        run: () => openSettings(section),
      })
    }

    items.push(
      {
        id: "act-new-thread",
        label: "Start a new thread",
        detail: activeBot ? `with ${activeBot.name}` : undefined,
        shortcut: "Ctrl N",
        group: "Actions",
        glyph: "plus",
        run: () => {
          setSettingsOpen(false)
          setActiveThreadId(null)
        },
      },
      {
        id: "act-desktop",
        label: desktopLayout.open ? "Close the Agent Computer" : "Open the Agent Computer",
        shortcut: "Ctrl ⇧ D",
        group: "Actions",
        glyph: "monitor",
        keywords: "screen desktop computer",
        run: toggleDesktopOpen,
      },
      {
        id: "act-theme",
        label: "Switch theme",
        group: "Actions",
        glyph: "moon",
        keywords: "dark light appearance",
        run: toggleTheme,
      },
      {
        id: "act-setup",
        label: "Open setup",
        group: "Actions",
        glyph: "plug",
        keywords: "backend endpoint provider model configure wizard",
        run: openSetup,
      },
      {
        id: "act-refresh",
        label: "Reload everything",
        group: "Actions",
        glyph: "refresh",
        keywords: "reload refetch",
        run: () => {
          void bots.refetch()
          void threads.refetch()
          void approvals.refetch()
          void health.refetch()
        },
      },
    )

    return items
  }, [
    bots,
    threads,
    approvals,
    health,
    activeBot,
    desktopLayout.open,
    selectThread,
    startWithBot,
    openSettings,
    setActiveThreadId,
    toggleDesktopOpen,
    toggleTheme,
  ])

  const apiDown = Boolean(health.error)

  /*
   * A short rise settles the chat pane when the conversation changes, so the
   * switch is legible as a move within one app rather than a page load.
   * Opacity resolves over `fast` and only the offset takes `base`: what a
   * person just asked for must never be waiting on an animation.
   */
  useGSAP(
    () => {
      gsap.from(mainRef.current, { y: 8, duration: dur("base"), ease: ease("entrance") })
      gsap.from(mainRef.current, { autoAlpha: 0, duration: dur("fast"), ease: ease("entrance") })
    },
    { dependencies: [activeThreadId], revertOnUpdate: true },
  )

  return (
    <div
      ref={desktopLayout.shellRef}
      className={cx(
        "app",
        desktopLayout.open ? "app--desktop-open" : "app--desktop-closed",
        desktopLayout.open && desktopLayout.expanded && "app--desktop-expanded",
        desktopLayout.resizing && "app--resizing",
      )}
    >
      <ConversationList
        threads={threads.threads}
        bots={bots.bots}
        loading={threads.initialising || bots.initialising}
        error={threads.error ?? bots.error}
        activeThreadId={activeThreadId}
        onSelectThread={selectThread}
        onStartWithBot={startWithBot}
        onStartGroup={startGroup}
        spendLabel={apiDown ? "API unreachable" : usd(usage.totalSpend, true)}
        desktopOpen={desktopLayout.open}
        onToggleDesktop={toggleDesktopOpen}
        onOpenSettings={() => openSettings("general")}
        themeButton={
          <button
            type="button"
            className="rail__tool"
            onClick={toggleTheme}
            aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
            title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
          >
            <Icon name={theme === "dark" ? "sun" : "moon"} size={15} />
          </button>
        }
      />

      <main className="main" ref={mainRef}>
        <ErrorBoundary label="Chat">
          <ChatPane
            bots={bots.bots}
            botsLoading={bots.initialising}
            botsError={bots.error}
            threads={threads}
            onApprovalRaised={onApprovalRaised}
            onTurnComplete={onTurnComplete}
            desktopOpen={desktopLayout.open}
            onToggleDesktop={toggleDesktopOpen}
            onEditProfile={(botId) => {
              setActiveBotId(botId)
              openSettings("profile")
            }}
          />
        </ErrorBoundary>
      </main>

      {desktopLayout.open && !desktopLayout.expanded ? (
        <PaneSplitter
          width={desktopLayout.width}
          min={desktopLayout.minWidth}
          max={desktopLayout.maxWidth}
          onPreview={desktopLayout.previewWidth}
          onCommit={desktopLayout.commitWidth}
          onReset={desktopLayout.resetWidth}
          onResizingChange={desktopLayout.setResizing}
        />
      ) : null}

      {desktopLayout.open ? (
        <ErrorBoundary label="Agent Computer">
          <DesktopPane bot={activeBot} layout={desktopLayout} onClose={() => setDesktopOpen(false)} />
        </ErrorBoundary>
      ) : null}

      <SettingsSheet
        open={settingsOpen}
        section={settingsSection}
        onSection={setSettingsSection}
        onClose={() => setSettingsOpen(false)}
        bots={bots}
        approvals={approvals}
        activeBotId={activeBotId}
        onSelectBot={setActiveBotId}
        usageRefreshKey={usageRefresh}
      />

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} commands={commands} />

      <ToastViewport />

      <TakeoverBeacon
        requests={offscreenTakeovers}
        nameFor={takeoverName}
        onOpen={focusTakeover}
        stacked={Boolean(focusApprovalId) && !settingsOpen}
      />

      {focusApprovalId && !settingsOpen ? (
        <button type="button" className="floating-hint" onClick={() => openSettings("approvals")}>
          <Icon name="shield" size={15} />
          An approval is waiting — open Approvals
        </button>
      ) : null}
    </div>
  )
}
