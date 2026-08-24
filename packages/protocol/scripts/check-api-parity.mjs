#!/usr/bin/env node
/**
 * Compare @nesqbot/protocol interfaces against the pydantic models they mirror.
 *
 *   node packages/protocol/scripts/check-api-parity.mjs
 *
 * Why this exists
 * ---------------
 * The protocol package is hand-transcribed from `apps/api/app/schemas.py`. Twice
 * now a field's *nullability* changed on the Python side after the TypeScript was
 * written, and presence-only review did not catch it: `UsageSummary.entries` and
 * `Run.thread_id`. Both were latent — nothing rendered the field yet — so the
 * first component to trust the type would have inherited the bug.
 *
 * What it checks, per mapped model:
 *   - a pydantic field with no TS counterpart          -> missing
 *   - pydantic nullable, TS neither optional nor null  -> nullability drift
 *   - pydantic required, TS optional                   -> looser than the API (info)
 *   - TS field with no pydantic counterpart            -> possibly invented (info)
 *
 * What it deliberately does NOT do: parse Python properly, resolve type
 * equivalence, or follow nested models. It is a lint for the one failure mode
 * that has actually bitten, not a schema compiler. Treat a clean run as "the
 * fields and their nullability line up", nothing stronger.
 *
 * Exit code is 1 only for `missing` and `nullability` findings. Info findings
 * are printed and ignored, because both are sometimes correct on purpose.
 */

import { readFileSync, readdirSync } from "node:fs"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(here, "..", "..", "..")
const schemasPath = join(repoRoot, "apps", "api", "app", "schemas.py")
const protocolSrc = join(repoRoot, "packages", "protocol", "src")

/**
 * pydantic class -> TypeScript interface.
 *
 * Response models only. Request models are intentionally absent: a client may
 * legitimately omit optional inputs, so "missing field" is not a finding there.
 * Add a pair here when you transcribe a new response model.
 */
const MAPPING = {
  UserOut: "User",
  BotOut: "Bot",
  ThreadOut: "Thread",
  MessageOut: "Message",
  RunOut: "Run",
  AuditEventOut: "AuditEvent",
  ApprovalOut: "Approval",
  DesktopOut: "BotDesktop",
  ConnectorOut: "ConnectorManifest",
  BotConnectorOut: "BotConnectorBinding",
  McpOut: "McpServer",
  McpToolsOut: "McpToolList",
  RoutineOut: "Routine",
  RoutineRunOut: "RoutineRunHandle",
  MemoryOut: "Memory",
  KbArticleOut: "KbSearchResult",
  UsageOut: "UsageSummary",
  TokenOut: "TokenResponse",
  DeviceOut: "Device",
  ScreenshotOut: "DesktopScreenshot",
  DesktopWindowsOut: "DesktopWindowList",
  DesktopStreamTicketOut: "DesktopStreamTicket",
  ResumeRunOut: "ResumeRunResponse",
  PendingApprovalOut: "PendingApprovalResponse",
  HealthOut: "Health",
  HealthDeepOut: "DeepHealth",
  OkOut: "OkResponse",
  WorkItemOut: "WorkItem",
  WorkItemKeyOut: "WorkItemKey",
  WorkItemTransferOut: "WorkItemTransfer",
  WorkItemTransferResultOut: "WorkItemTransferResult",
  InboundSourceOut: "InboundSource",
  InboundEventOut: "InboundEvent",
  InboundAckOut: "InboundAck",
  InboundPollOut: "InboundPollResult",
  StandingApprovalOut: "StandingApproval",
  StandingApprovalListOut: "StandingApprovalList",
}

/**
 * Fields the TypeScript deliberately spells differently or omits, with the
 * reason. Anything here is exempt from the `missing` check.
 */
const EXEMPT = {
  // Deliberate deviations. Each one is a decision, not an oversight.
  PendingApprovalOut__approval_id:
    "pydantic declares UUID|None defensively, but routers/integrations.py and " +
    "routers/desktop.py both build it from a freshly persisted approval.id, and " +
    "it is the discriminant isPendingApproval() branches on — so TS requires it",
  // `Bot.system_prompt` is write-only: BotOut never returns it, so the TS has it
  // optional. Covered by the nullability rules below rather than an exemption.
  ApprovalOut__execution: "typed as the ApprovalExecution union rather than a bare dict",
  HealthDeepOut__checks: "typed as DeepHealthChecks with named dependencies",
  UsageOut__entries: "typed as UsageEntry[] rather than list[dict]",
  RoutineOut__steps: "typed as RoutineStep[] rather than list[Any]",
  ConnectorOut__actions: "typed as ConnectorAction[] rather than list[Any]",
  ConnectorOut__scopes: "typed as string[] rather than list[Any]",
  McpOut__tool_allowlist: "typed as string[] rather than list[Any]",
  BotConnectorOut__actions: "typed as ConnectorAction[] rather than list[Any]",
  McpToolsOut__tools: "typed as McpTool[] rather than list[dict]",
  DesktopWindowsOut__windows: "typed as DesktopWindow[] rather than list[dict]",
}

/* ---------------------------------------------------------------- python -- */

/** Parse `class X(BaseModel):` bodies into `{field: {nullable, hasDefault}}`. */
function parsePydantic(source) {
  const models = {}
  let current = null
  // Inside a triple-quoted docstring. Tracked because a wrapped line of prose
  // that happens to start with a word and a colon — "instead: short-lived, …"
  // in DesktopStreamTicketOut — is otherwise read as a field, and the model is
  // then reported as missing a field that does not exist. The TypeScript side
  // already strips its comments before matching; this is the same rule.
  let inDocstring = false
  for (const rawLine of source.split("\n")) {
    const line = rawLine.replace(/\s+$/, "")

    // Toggle on each triple quote. An odd count opens or closes; an even count
    // is a one-line docstring that opens and closes on the same line.
    const quotes = (line.match(/"""/g) ?? []).length
    if (quotes > 0) {
      if (quotes % 2 === 1) inDocstring = !inDocstring
      continue
    }
    if (inDocstring) continue

    const classMatch = /^class\s+([A-Za-z0-9_]+)\s*\(\s*BaseModel\s*\)\s*:/.exec(line)
    if (classMatch) {
      current = classMatch[1]
      models[current] = {}
      continue
    }
    if (line.length > 0 && !/^\s/.test(line)) {
      current = null // dedent: left the class body
      continue
    }
    if (current === null) continue

    // `    name: type` or `    name: type = default`
    const fieldMatch = /^ {4}([a-z_][A-Za-z0-9_]*)\s*:\s*(.+)$/.exec(line)
    if (fieldMatch === null) continue
    const name = fieldMatch[1]
    let annotation = fieldMatch[2]
    let hasDefault = false

    // Split on a top-level `=` that is not inside brackets.
    let depth = 0
    for (let i = 0; i < annotation.length; i += 1) {
      const ch = annotation[i]
      if (ch === "[" || ch === "(" || ch === "{") depth += 1
      else if (ch === "]" || ch === ")" || ch === "}") depth -= 1
      else if (ch === "=" && depth === 0) {
        hasDefault = true
        annotation = annotation.slice(0, i)
        break
      }
    }
    annotation = annotation.trim()
    const nullable = /\|\s*None\b/.test(annotation) || /^Optional\[/.test(annotation)
    models[current][name] = { nullable, hasDefault, annotation }
  }
  return models
}

/* ------------------------------------------------------------ typescript -- */

/** Parse `export interface X { ... }` bodies into `{field: {optional, nullable}}`. */
function parseTypescript(sources) {
  const interfaces = {}
  for (const source of sources) {
    const re = /export interface ([A-Za-z0-9_]+)(?:\s+extends\s+([A-Za-z0-9_<>, ]+?))?\s*\{/g
    let match
    while ((match = re.exec(source)) !== null) {
      const name = match[1]
      const parents = (match[2] ?? "")
        .split(",")
        .map((x) => x.trim().replace(/<.*$/, ""))
        .filter(Boolean)
      // Walk to the matching close brace.
      let depth = 1
      let i = re.lastIndex
      for (; i < source.length && depth > 0; i += 1) {
        if (source[i] === "{") depth += 1
        else if (source[i] === "}") depth -= 1
      }
      const body = source.slice(re.lastIndex, i - 1)
      const fields = {}
      // Strip block comments so a `?:` inside prose is not read as a field.
      const clean = body.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "")
      const fieldRe = /^\s{2}([a-z_][A-Za-z0-9_]*)(\?)?\s*:\s*([^\n]+)$/gm
      let f
      while ((f = fieldRe.exec(clean)) !== null) {
        fields[f[1]] = {
          optional: f[2] === "?",
          nullable: /\bnull\b/.test(f[3]),
          type: f[3].replace(/,?\s*$/, ""),
        }
      }
      interfaces[name] = { fields, parents }
    }
  }
  return interfaces
}

/** Fields of an interface plus everything it inherits. */
function fieldsOf(interfaces, name, seen = new Set()) {
  const entry = interfaces[name]
  if (entry === undefined || seen.has(name)) return {}
  seen.add(name)
  const inherited = {}
  for (const parent of entry.parents) Object.assign(inherited, fieldsOf(interfaces, parent, seen))
  return { ...inherited, ...entry.fields }
}

/* ------------------------------------------------------------------ run -- */

const pydantic = parsePydantic(readFileSync(schemasPath, "utf8"))
const tsSources = readdirSync(protocolSrc)
  .filter((f) => f.endsWith(".ts"))
  .map((f) => readFileSync(join(protocolSrc, f), "utf8"))
const typescript = parseTypescript(tsSources)

const problems = []
const info = []

for (const [pyName, tsName] of Object.entries(MAPPING)) {
  const pyFields = pydantic[pyName]
  const tsFields = typescript[tsName] === undefined ? undefined : fieldsOf(typescript, tsName)
  if (pyFields === undefined) {
    problems.push(`${pyName}: no such pydantic model in schemas.py (mapping is stale)`)
    continue
  }
  if (tsFields === undefined) {
    problems.push(`${tsName}: no such TypeScript interface (mapping is stale)`)
    continue
  }

  for (const [field, py] of Object.entries(pyFields)) {
    const ts = tsFields[field]
    if (ts === undefined) {
      if (EXEMPT[`${pyName}__${field}`] === undefined) {
        problems.push(`${tsName}.${field}: missing — ${pyName} declares it as \`${py.annotation}\``)
      }
      continue
    }
    if (py.nullable && !ts.optional && !ts.nullable && EXEMPT[`${pyName}__${field}`] === undefined) {
      problems.push(`${tsName}.${field}: ${pyName} is \`${py.annotation}\` (nullable) but TS is required and non-null`)
    }
    if (!py.nullable && !py.hasDefault && ts.optional) {
      info.push(`${tsName}.${field}: optional in TS, required in ${pyName}`)
    }
  }

  for (const field of Object.keys(tsFields)) {
    if (pyFields[field] === undefined) {
      info.push(`${tsName}.${field}: not present on ${pyName}`)
    }
  }
}

const models = Object.keys(MAPPING).length
if (info.length > 0) {
  console.log(`info (${info.length}) — not failures, review occasionally:`)
  for (const line of info) console.log(`  · ${line}`)
  console.log("")
}
if (problems.length > 0) {
  console.error(`FAIL — ${problems.length} finding(s) across ${models} mapped models:`)
  for (const line of problems) console.error(`  ✗ ${line}`)
  process.exit(1)
}
console.log(`ok — ${models} models match schemas.py on field presence and nullability`)
