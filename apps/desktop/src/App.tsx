/**
 * Composition only. Every behaviour lives in `hooks/`, `state/` or a component.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { REQUIRES_SIGN_IN } from "./api/client"
import { getHealth } from "./api/endpoints"
import { ApprovalsPanel } from "./components/ApprovalsPanel"
import { AuditPanel } from "./components/AuditPanel"
import { BuilderPanel } from "./components/BuilderPanel"
import { ChatPane } from "./components/ChatPane"
import { DesktopPane } from "./components/DesktopPane"
import { ErrorBoundary } from "./components/ErrorBoundary"
import { Icon, type IconName } from "./components/Icon"
import { IntegrationsPanel } from "./components/IntegrationsPanel"
import { KnowledgePanel } from "./components/KnowledgePanel"
import { PaneSplitter } from "./components/PaneSplitter"
import { RoutinesPanel } from "./components/RoutinesPanel"
import { SessionBootScreen, SignInScreen } from "./components/SignInScreen"
import { SetupWizard } from "./components/SetupWizard"
import { Sidebar } from "./components/Sidebar"
import { TakeoverBeacon } from "./components/TakeoverBeacon"
import { ToastViewport } from "./components/Toast"
import { UsagePanel } from "./components/UsagePanel"
import { useApprovals } from "./hooks/useApprovals"
import { useAsyncResource } from "./hooks/useAsync"
import { useBots } from "./hooks/useBots"
import { useDesktopLayout } from "./hooks/useDesktopLayout"
import { completeEntraRedirect, useAuth } from "./auth"
import { cx } from "./lib/format"
import { dur, ease, gsap, useGSAP } from "./lib/motion"
import { onShellEvent, parseDeepLink } from "./lib/tauri"
import { CommandPalette, type Command } from "./components/CommandPalette"
import { AppProviders, useSelection, useToast, type PanelTab } from "./state/AppState"
import { hasCompletedSetup, resetSetupCompletion } from "./state/setup"
import { useTakeover } from "./state/takeover"
import { ThemeProvider, useTheme } from "./state/theme"
import type { TakeoverRequest } from "./lib/takeover"
import type { HealthOut } from "./types"

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
 * `openSetup` reopens it from the running app — see `Shell`'s "Setup" command
 * and sidebar entry.
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

/** Reopens the setup wizard from anywhere inside the shell — see `SetupGate`. */
export function openSetup(): void {
  window.dispatchEvent(new Event("nesq:open-setup"))
}

/**
 * The sidebar's order, in one place.
 *
 * `Ctrl+1…6`, the palette's "Go to" group and the sidebar itself all have to
 * agree about which section is third, and three separate literal lists is how
 * they stop agreeing.
 */
const TAB_ORDER: PanelTab[] = ["chat", "approvals", "integrations", "routines", "usage", "audit", "knowledge", "builder"]

const TAB_LABELS: Record<PanelTab, string> = {
  chat: "Chat",
  approvals: "Approvals",
  integrations: "Integrations",
  routines: "Routines",
  usage: "Usage",
  audit: "Audit",
  knowledge: "Knowledge",
  builder: "Builder",
}

const TAB_GLYPHS: Record<PanelTab, IconName> = {
  chat: "chat",
  approvals: "shield",
  integrations: "plug",
  routines: "repeat",
  usage: "chart",
  audit: "list",
  knowledge: "book",
  builder: "blocks",
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

function Shell() {
  const toast = useToast()
  const { toggleTheme } = useTheme()
  const { tab, setTab, activeBotId, setActiveBotId, setActiveThreadId, focusApprovalId, setFocusApprovalId } =
    useSelection()

  const bots = useBots()
  const approvals = useApprovals("pending")
  const handoff = useTakeover()
  const [usageRefresh, setUsageRefresh] = useState(0)
  // Width, maximised-or-not and scale for the Bot Desktop pane, remembered
  // across restarts. Owned here because the splitter and the pane are siblings
  // in the shell grid, and the width is a grid track.
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

  const openApproval = useCallback(
    (approvalId: string) => {
      setTab("approvals")
      setFocusApprovalId(approvalId)
      void approvals.refetch()
    },
    [setTab, setFocusApprovalId, approvals],
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
    setUsageRefresh((value) => value + 1)
  }, [approvals])

  /* ------------------------------------------------------------------ *
   * Human handoff
   *
   * The state lives in `state/takeover`; what belongs here is the *staging* —
   * which bot is selected, which tab is up, and whether the Bot Desktop pane is
   * covering the window. Those are shell concerns and the provider deliberately
   * knows nothing about them.
   * ------------------------------------------------------------------ */

  const setExpanded = desktopLayout.setExpanded

  /*
   * Maximising for a takeover must not become the person's saved preference.
   *
   * `useDesktopLayout` persists `expanded` on purpose — somebody who works in
   * takeover should not re-expand the pane every morning. But that is about
   * *their* choice. A run parking on a login is not a choice, and letting it
   * write the preference means one interruption silently changes how the app
   * opens forever. So the pre-takeover value is held here and put back once
   * nothing is waiting. A ref, so toggling the pane does not rebuild
   * `focusTakeover` and re-run the effect that calls it.
   */
  const mainRef = useRef<HTMLElement | null>(null)

  const expandedRef = useRef(desktopLayout.expanded)
  expandedRef.current = desktopLayout.expanded
  const expandedBeforeTakeover = useRef<boolean | null>(null)

  /**
   * Put one parked run in front of the person: its bot, its thread, the chat
   * tab, and the desktop maximised so the screen they have to sign in on is
   * the biggest thing on the display. `TakeoverCard` renders itself once the
   * pane's bot matches.
   */
  const focusTakeover = useCallback(
    (request: TakeoverRequest) => {
      handoff.undismiss(request.runId)
      if (request.botId) setActiveBotId(request.botId)
      if (request.threadId) setActiveThreadId(request.threadId)
      setTab("chat")
      if (expandedBeforeTakeover.current === null) expandedBeforeTakeover.current = expandedRef.current
      setExpanded(true)
    },
    [handoff, setActiveBotId, setActiveThreadId, setTab, setExpanded],
  )

  const outstandingTakeovers = handoff.requests.length
  useEffect(() => {
    if (outstandingTakeovers > 0) return
    const previous = expandedBeforeTakeover.current
    expandedBeforeTakeover.current = null
    // Only ever un-maximise. If they were already maximised, or maximised it
    // themselves while dealing with this, that is their layout to keep.
    if (previous === false) setExpanded(false)
  }, [outstandingTakeovers, setExpanded])

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
          setTab("chat")
          setActiveThreadId(target.id)
        } else if (target.kind === "bot" && target.id) {
          setTab("chat")
          setActiveBotId(target.id)
        } else if (target.kind === "tab" && target.id) {
          setTab(target.id as PanelTab)
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
          setTab("chat")
          setActiveThreadId(null)
          break
        case "toggle-theme":
          toggleTheme()
          break
        case "open-approvals":
          setTab("approvals")
          break
        case "open-integrations":
          setTab("integrations")
          break
        case "open-routines":
          setTab("routines")
          break
        case "open-usage":
          setTab("usage")
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
  }, [openApproval, setActiveBotId, setActiveThreadId, setTab, toggleTheme, toast])

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
   *    opened with every shortcut already disabled and `Ctrl+1` — which the
   *    palette advertises next to "Chat" — did nothing at all until you clicked
   *    somewhere neutral first. A shortcut the product advertises and does not
   *    honour is worse than one it never mentions.
   *
   *    So the test is whether the chord could plausibly be text editing.
   *    Ctrl/Cmd with a digit cannot be; neither can Ctrl/Cmd+Shift+D. Those
   *    work everywhere. `Ctrl+N` is a real readline binding people use inside
   *    textareas, so that one still stands down while a field has focus.
   *  - `Ctrl/Cmd+Shift+D` for the desktop pane rather than F11, which the
   *    WebView reserves.
   * ------------------------------------------------------------------ */
  const toggleDesktop = desktopLayout.toggleExpanded
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
          toggleDesktop()
        }
        return
      }

      // Ctrl/Cmd+1…6 walks the sidebar in the order the sidebar is drawn in.
      // `event.code` rather than `event.key`, so it survives a layout where
      // the top row produces symbols instead of digits.
      const digit = /^Digit([1-9])$/.exec(event.code)?.[1]
      if (digit) {
        const index = Number(digit)
        if (index <= TAB_ORDER.length) {
          event.preventDefault()
          setTab(TAB_ORDER[index - 1])
        }
        return
      }

      // The one binding that stands down for a text field: Ctrl+N is a real
      // readline "next line" chord and people use it inside textareas.
      if (key === "n" && !typing) {
        event.preventDefault()
        setTab("chat")
        setActiveThreadId(null)
      }
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [toggleDesktop, setTab, setActiveThreadId])

  /*
   * What the palette can reach.
   *
   * Teammates first, because "talk to Vesna" is the most common intent in the
   * product and because it is the list that changes. Then the sections, then
   * the handful of window-level things. Nothing here decides an approval — see
   * the note at the top of `CommandPalette`.
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
        setActiveBotId(bot.id)
        setTab("chat")
      },
    }))

    for (const [index, entry] of TAB_ORDER.entries()) {
      items.push({
        id: `tab-${entry}`,
        label: TAB_LABELS[entry],
        detail: entry === "approvals" && approvals.pendingCount > 0 ? `${approvals.pendingCount} waiting` : undefined,
        shortcut: `Ctrl ${index + 1}`,
        group: "Go to",
        glyph: TAB_GLYPHS[entry],
        run: () => setTab(entry),
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
          setTab("chat")
          setActiveThreadId(null)
        },
      },
      {
        id: "act-desktop",
        label: desktopLayout.expanded ? "Restore the Bot Desktop pane" : "Maximise the Bot Desktop",
        shortcut: "Ctrl ⇧ D",
        group: "Actions",
        glyph: "monitor",
        keywords: "screen fullscreen expand",
        run: toggleDesktop,
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
          void approvals.refetch()
          void health.refetch()
        },
      },
    )

    return items
  }, [
    bots,
    approvals,
    health,
    activeBot,
    desktopLayout.expanded,
    setActiveBotId,
    setTab,
    setActiveThreadId,
    toggleDesktop,
    toggleTheme,
  ])

  const apiDown = Boolean(health.error)

  /*
   * What the footer reports.
   *
   * `version` is the API contract number, hand-maintained in the server source;
   * `build` is the image tag stamped at docker build time. Showing only the
   * former made a fresh deploy look like a stale one - the footer read
   * "API 0.2.0" while image v0.3.0 was serving. Prefer the build, because that
   * is the question someone reads this line to answer.
   */
  const build = health.data?.build
  const apiLabel =
    build && build !== "unknown" ? build : (health.data?.version ?? "connected")

  /*
   * Switching panels.
   *
   * Six tabs that used to swap their entire contents between one frame and the
   * next, which reads as a page reload rather than as a move within one app.
   * A short rise settles the incoming panel, so the change is legible as a
   * change.
   *
   * Opacity resolves over `fast` and only the offset takes `base`: the panel a
   * person just asked for must never be waiting on an animation. `revertOnUpdate`
   * so each switch starts from a clean slate rather than compounding transforms
   * on a pane that is scrolled.
   */
  useGSAP(
    () => {
      gsap.from(mainRef.current, { y: 8, duration: dur("base"), ease: ease("entrance") })
      gsap.from(mainRef.current, { autoAlpha: 0, duration: dur("fast"), ease: ease("entrance") })
    },
    { dependencies: [tab], revertOnUpdate: true },
  )

  return (
    <div
      ref={desktopLayout.shellRef}
      className={cx(
        "app",
        desktopLayout.expanded && "app--desktop-expanded",
        desktopLayout.resizing && "app--resizing",
      )}
    >
      <Sidebar
        tab={tab}
        onTabChange={setTab}
        onOpenPalette={() => setPaletteOpen(true)}
        pendingApprovals={approvals.pendingCount}
        statusLine={apiDown ? "API unreachable" : `API ${apiLabel}`}
        statusTone={apiDown ? "error" : "ok"}
        botList={{
          bots: bots.bots,
          loading: bots.initialising,
          error: bots.error,
          activeBotId,
          onSelect: (bot) => {
            setActiveBotId(bot.id)
            setTab("chat")
          },
          onRetry: () => void bots.refetch(),
        }}
      />

      <main className="main" ref={mainRef}>
        {tab === "chat" ? (
          <ErrorBoundary label="Chat">
            <ChatPane
              bots={bots.bots}
              botsLoading={bots.initialising}
              botsError={bots.error}
              onApprovalRaised={onApprovalRaised}
              onTurnComplete={onTurnComplete}
            />
          </ErrorBoundary>
        ) : null}

        {tab === "approvals" ? (
          <ErrorBoundary label="Approvals">
            <ApprovalsPanel approvals={approvals} bots={bots.bots} />
          </ErrorBoundary>
        ) : null}

        {tab === "integrations" ? (
          <ErrorBoundary label="Integrations">
            <IntegrationsPanel bots={bots.bots} activeBotId={activeBotId} onSelectBot={setActiveBotId} />
          </ErrorBoundary>
        ) : null}

        {tab === "routines" ? (
          <ErrorBoundary label="Routines">
            <RoutinesPanel bots={bots.bots} activeBotId={activeBotId} onSelectBot={setActiveBotId} />
          </ErrorBoundary>
        ) : null}

        {tab === "usage" ? (
          <ErrorBoundary label="Usage">
            <UsagePanel refreshKey={usageRefresh} />
          </ErrorBoundary>
        ) : null}

        {tab === "audit" ? (
          <ErrorBoundary label="Audit">
            <AuditPanel bots={bots.bots} />
          </ErrorBoundary>
        ) : null}

        {tab === "knowledge" ? (
          <ErrorBoundary label="Knowledge">
            <KnowledgePanel />
          </ErrorBoundary>
        ) : null}

        {tab === "builder" ? (
          <ErrorBoundary label="Builder">
            <BuilderPanel bots={bots} activeBotId={activeBotId} onSelectBot={setActiveBotId} />
          </ErrorBoundary>
        ) : null}
      </main>

      {desktopLayout.expanded ? null : (
        <PaneSplitter
          width={desktopLayout.width}
          min={desktopLayout.minWidth}
          max={desktopLayout.maxWidth}
          onPreview={desktopLayout.previewWidth}
          onCommit={desktopLayout.commitWidth}
          onReset={desktopLayout.resetWidth}
          onResizingChange={desktopLayout.setResizing}
        />
      )}

      <ErrorBoundary label="Bot Desktop">
        <DesktopPane bot={activeBot} layout={desktopLayout} />
      </ErrorBoundary>

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} commands={commands} />

      <ToastViewport />

      <TakeoverBeacon
        requests={offscreenTakeovers}
        nameFor={takeoverName}
        onOpen={focusTakeover}
        stacked={Boolean(focusApprovalId) && tab !== "approvals"}
      />

      {focusApprovalId && tab !== "approvals" ? (
        <button type="button" className="floating-hint" onClick={() => setTab("approvals")}>
          <Icon name="shield" size={15} />
          An approval is waiting — open Approvals
        </button>
      ) : null}
    </div>
  )
}
