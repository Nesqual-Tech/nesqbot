import { useState } from "react"
import { errorMessage } from "../api/client"
import { useMcp } from "../hooks/useMcp"
import { cx, prettyJson } from "../lib/format"
import { useToast } from "../state/AppState"
import { EmptyState, ErrorState } from "./EmptyState"
import { Icon } from "./Icon"
import { SkeletonCards } from "./Skeleton"
import { Spinner } from "./Spinner"
import type { McpTransport } from "../types"

export interface McpPanelProps {
  botId: string | null
  botName: string | null
}

const TRANSPORTS: McpTransport[] = ["http", "sse", "stdio"]

export function McpPanel({ botId, botName }: McpPanelProps) {
  const mcp = useMcp()
  const toast = useToast()

  const [name, setName] = useState("")
  const [transport, setTransport] = useState<McpTransport>("http")
  const [endpoint, setEndpoint] = useState("")
  const [command, setCommand] = useState("")
  const [allowlist, setAllowlist] = useState("")
  const [submitting, setSubmitting] = useState(false)
  const [allowlistDrafts, setAllowlistDrafts] = useState<Record<string, string>>({})
  const [busyId, setBusyId] = useState<string | null>(null)
  const [registerOpen, setRegisterOpen] = useState(false)
  const [confirmRemove, setConfirmRemove] = useState<string | null>(null)

  const needsEndpoint = transport !== "stdio"

  const register = async () => {
    if (!name.trim()) {
      toast.warning("Name the server", "Give the MCP server a recognisable name.")
      return
    }
    if (needsEndpoint && !endpoint.trim()) {
      toast.warning("Endpoint required", `${transport} transport needs a URL.`)
      return
    }
    if (!needsEndpoint && !command.trim()) {
      toast.warning("Command required", "stdio transport needs a command to run.")
      return
    }
    setSubmitting(true)
    try {
      const server = await mcp.register({
        name: name.trim(),
        transport,
        endpoint: needsEndpoint ? endpoint.trim() : null,
        command: needsEndpoint ? null : command.trim(),
        tool_allowlist: allowlist
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
      })
      toast.success("MCP server registered", server.name)
      setName("")
      setEndpoint("")
      setCommand("")
      setAllowlist("")
      setRegisterOpen(false)
    } catch (err) {
      toast.error("Could not register the MCP server", errorMessage(err))
    } finally {
      setSubmitting(false)
    }
  }

  const withBusy = async (id: string, fn: () => Promise<unknown>, okMessage?: string) => {
    setBusyId(id)
    try {
      await fn()
      if (okMessage) toast.success(okMessage)
    } catch (err) {
      toast.error("MCP request failed", errorMessage(err))
    } finally {
      setBusyId(null)
    }
  }

  return (
    <section className="subpanel">
      <header className="subpanel__header">
        <h3 className="subpanel__title">MCP servers</h3>
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={() => void mcp.refetch()}
          disabled={mcp.loading}
        >
          {mcp.loading ? <Spinner inline label="Refreshing" /> : "Refresh"}
        </button>
      </header>

      {/*
        Folded, like the connector manifest editor above it. Registering an MCP
        server is a setup action; the panel is opened to check what is attached.
        Four inputs and a Register button were the first thing on the screen
        every time, above the list of servers that already exist.
      */}
      <button
        type="button"
        className="disclosure disclosure--section"
        aria-expanded={registerOpen}
        aria-controls="register-mcp"
        onClick={() => setRegisterOpen((prev) => !prev)}
      >
        <Icon name={registerOpen ? "collapse" : "plus"} size={14} />
        {registerOpen ? "Close the form" : "Register an MCP server"}
      </button>

      {registerOpen ? (
      <div className="card reveal" id="register-mcp">
        <div className="form-grid">
          <label className="field">
            <span className="field__label">Name</span>
            <input
              className="input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Internal tools"
            />
          </label>
          <label className="field">
            <span className="field__label">Transport</span>
            <select className="select" value={transport} onChange={(e) => setTransport(e.target.value as McpTransport)}>
              {TRANSPORTS.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
          {needsEndpoint ? (
            <label className="field">
              <span className="field__label">Endpoint</span>
              <input
                className="input"
                value={endpoint}
                onChange={(e) => setEndpoint(e.target.value)}
                placeholder="http://localhost:3100"
              />
            </label>
          ) : (
            <label className="field">
              <span className="field__label">Command</span>
              <input
                className="input"
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                placeholder="npx -y @modelcontextprotocol/server-filesystem /data"
              />
            </label>
          )}
          <label className="field">
            <span className="field__label">Tool allowlist (comma separated)</span>
            <input
              className="input"
              value={allowlist}
              onChange={(e) => setAllowlist(e.target.value)}
              placeholder="search, fetch"
            />
          </label>
        </div>
        <div className="row-actions">
          <button
            type="button"
            className="btn btn--primary btn--sm"
            onClick={() => void register()}
            disabled={submitting}
          >
            {submitting ? <Spinner inline label="Registering" /> : "Register"}
          </button>
        </div>
      </div>
      ) : null}

      {mcp.initialising ? <SkeletonCards cards={2} /> : null}

      {mcp.error && mcp.servers.length === 0 && !mcp.initialising ? (
        <ErrorState error={mcp.error} title="MCP registry unavailable" onRetry={() => void mcp.refetch()} compact />
      ) : null}

      {!mcp.initialising && !mcp.error && mcp.servers.length === 0 ? (
        <EmptyState
          compact
          glyph="blocks"
          title="No MCP servers yet"
          description="Register one above to expose extra tools to your bots."
        />
      ) : null}

      {mcp.servers.map((server) => {
        const tools = mcp.tools[server.id]
        const attachKey = botId ? `${botId}:${server.id}` : ""
        const isAttached = attachKey ? mcp.attached[attachKey] : undefined
        const draft = allowlistDrafts[server.id] ?? (server.tool_allowlist ?? []).join(", ")
        return (
          <article className="card mcp" key={server.id}>
            <header className="connector__header">
              <div>
                <h4 className="card__title">{server.name}</h4>
                <p className="card__body">
                  <code>{server.transport}</code>
                  {server.endpoint ? ` · ${server.endpoint}` : ""}
                  {server.command ? ` · ${server.command}` : ""}
                </p>
              </div>
              <label className="switch">
                <input
                  type="checkbox"
                  checked={server.enabled}
                  disabled={busyId === server.id}
                  onChange={(event) =>
                    void withBusy(
                      server.id,
                      () => mcp.update(server.id, { enabled: event.target.checked }),
                      event.target.checked ? "Server enabled" : "Server disabled",
                    )
                  }
                />
                <span>{server.enabled ? "enabled" : "disabled"}</span>
              </label>
            </header>

            <label className="field">
              <span className="field__label">Tool allowlist</span>
              <div className="field__row">
                <input
                  className="input"
                  value={draft}
                  onChange={(event) => setAllowlistDrafts((prev) => ({ ...prev, [server.id]: event.target.value }))}
                  placeholder="empty = all tools"
                />
                <button
                  type="button"
                  className="btn btn--ghost btn--sm"
                  disabled={busyId === server.id}
                  onClick={() =>
                    void withBusy(
                      server.id,
                      () =>
                        mcp.update(server.id, {
                          tool_allowlist: draft
                            .split(",")
                            .map((t) => t.trim())
                            .filter(Boolean),
                        }),
                      "Allowlist saved",
                    )
                  }
                >
                  Save
                </button>
              </div>
            </label>

            <div className="row-actions">
              <button
                type="button"
                className="btn btn--ghost btn--sm"
                onClick={() => void mcp.loadTools(server.id)}
                disabled={tools?.loading}
              >
                {tools?.loading ? <Spinner inline label="Loading tools" /> : "Browse tools"}
              </button>
              {/*
                One of Attach and Detach, never both.

                The row used to render Attach, Detach and Delete side by side at
                identical weight, so two of the three were always the wrong move
                and the destructive one was the most saturated. Whether the
                server is attached to the selected bot is already known — the
                chip beside them was saying it — so the row shows the action
                that state actually permits. Same rule the Bot Desktop lifecycle
                row follows.
              */}
              {isAttached === true ? (
                <button
                  type="button"
                  className="btn btn--ghost btn--sm"
                  disabled={!botId || busyId === server.id}
                  onClick={() =>
                    botId &&
                    void withBusy(server.id, () => mcp.detach(botId, server.id), `Detached from ${botName ?? "bot"}`)
                  }
                >
                  Detach from {botName ?? "bot"}
                </button>
              ) : (
                <button
                  type="button"
                  className="btn btn--ghost btn--sm"
                  disabled={!botId || busyId === server.id}
                  title={botName ? `Attach to ${botName}` : "Select a bot first"}
                  onClick={() =>
                    botId &&
                    void withBusy(server.id, () => mcp.attach(botId, server.id), `Attached to ${botName ?? "bot"}`)
                  }
                >
                  {botName ? `Attach to ${botName}` : "Attach"}
                </button>
              )}

              {isAttached !== undefined ? (
                <span className={cx("chip", isAttached ? "chip--ok" : "chip--muted")}>
                  {isAttached ? `attached to ${botName ?? "bot"}` : "detached"}
                </span>
              ) : null}

              {/* Removing a server takes it away from every bot. Two presses. */}
              {confirmRemove === server.id ? (
                <span className="danger-confirm" role="alert">
                  <span className="danger-confirm__text">Removes {server.name} for every bot.</span>
                  <button
                    type="button"
                    className="btn btn--quiet-danger btn--sm"
                    disabled={busyId === server.id}
                    onClick={() => {
                      setConfirmRemove(null)
                      void withBusy(server.id, () => mcp.remove(server.id), "Server removed")
                    }}
                  >
                    Remove it
                  </button>
                  <button type="button" className="btn btn--ghost btn--sm" autoFocus onClick={() => setConfirmRemove(null)}>
                    Keep
                  </button>
                </span>
              ) : (
                <button
                  type="button"
                  className="btn btn--ghost btn--sm mcp__remove"
                  disabled={busyId === server.id}
                  onClick={() => setConfirmRemove(server.id)}
                >
                  Remove server
                </button>
              )}
            </div>

            {tools ? (
              <div className="mcp__tools">
                {tools.error ? <div className="inline-error">{tools.error}</div> : null}
                {tools.mock ? <span className="chip chip--warn">mock listing</span> : null}
                {tools.tools.length === 0 && !tools.loading && !tools.error ? (
                  <p className="muted">No tools reported.</p>
                ) : null}
                <ul className="mcp__tool-list">
                  {tools.tools.map((tool) => (
                    <li key={tool.name}>
                      <details>
                        <summary>
                          <code>{tool.name}</code>
                          {tool.description ? <span className="muted"> — {tool.description}</span> : null}
                        </summary>
                        <pre className="code-block code-block--scroll">{prettyJson(tool.inputSchema ?? {})}</pre>
                      </details>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </article>
        )
      })}
    </section>
  )
}
