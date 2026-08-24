/**
 * @nesqbot/connector-sdk — author and validate connector manifests.
 *
 * A connector is a named set of actions, each carrying a risk class. The API
 * stores the manifest (`POST /integrations/connectors`, `RegisterConnectorIn`
 * in `apps/api/app/schemas.py`), binds it to a bot with a Key Vault
 * `secret_ref`, and refuses to execute any action whose risk requires
 * approval until a human has decided.
 *
 * This package is the authoring side of that contract: the types, a builder
 * that fails loudly on a malformed manifest, and a validator that returns
 * structured errors you can render in the connector builder UI. It performs
 * no I/O and has no runtime dependencies — execution belongs to the API.
 *
 * See `docs/connectors.md` for the manifest shape and a worked example.
 */

import {
  RISK_CLASSES,
  type ConnectorAction,
  type ConnectorAuthKind,
  type ConnectorManifest,
  type RegisterConnectorRequest,
  type RiskClass,
} from "@nesqbot/protocol"

export type { ConnectorAction, ConnectorAuthKind, ConnectorManifest, RiskClass }

/**
 * Single source of truth for "does this need a human?" — re-exported from
 * `@nesqbot/protocol` so the SDK, the clients and the API cannot drift.
 */
export { requiresApproval, RISK_CLASSES } from "@nesqbot/protocol"

/* ------------------------------------------------------------------ *
 * Handler
 * ------------------------------------------------------------------ */

export interface ConnectorContext {
  botId: string
  userId: string
  /** Opaque secret material resolved from Key Vault — never logged. */
  secrets: Record<string, string>
}

export interface ConnectorHandler {
  manifest: ConnectorManifest
  execute(action: string, input: Record<string, unknown>, ctx: ConnectorContext): Promise<unknown>
}

export interface DefineConnectorOptions {
  id: string
  name: string
  version?: string
  auth: ConnectorAuthKind
  scopes?: string[]
  risk_default?: RiskClass
  first_party?: boolean
  actions: ConnectorAction[]
  execute: ConnectorHandler["execute"]
}

/**
 * Build a connector handler from a manifest description.
 *
 * Throws {@link ConnectorManifestError} if the manifest is invalid. That is
 * deliberate: a connector with a typo in a risk class is a governance hole,
 * and it should fail at author time rather than at approval time.
 */
export function defineConnector(opts: DefineConnectorOptions): ConnectorHandler {
  const manifest: ConnectorManifest = {
    id: opts.id,
    name: opts.name,
    version: opts.version ?? "1.0.0",
    auth: opts.auth,
    scopes: opts.scopes ?? [],
    actions: opts.actions,
    risk_default: opts.risk_default ?? "observe",
    first_party: opts.first_party ?? false,
  }

  const result = validateManifest(manifest)
  if (!result.ok) throw new ConnectorManifestError(result.errors)

  return { manifest: result.manifest, execute: opts.execute }
}

/* ------------------------------------------------------------------ *
 * Introspection
 * ------------------------------------------------------------------ */

/** Risk of one action, falling back to the manifest default. */
export function actionRisk(manifest: ConnectorManifest, actionName: string): RiskClass {
  const action = manifest.actions.find((a) => a.name === actionName)
  return action?.risk ?? manifest.risk_default
}

export function findAction(manifest: ConnectorManifest, actionName: string): ConnectorAction | undefined {
  return manifest.actions.find((a) => a.name === actionName)
}

/** Actions that will always stop at a human. Show these in the bind screen. */
export function gatedActions(manifest: ConnectorManifest): ConnectorAction[] {
  return manifest.actions.filter((a) => APPROVAL_RISKS.includes(a.risk))
}

const APPROVAL_RISKS: readonly RiskClass[] = ["send", "spend", "delete"]

/** The POST body for `/integrations/connectors`. */
export function toRegisterRequest(manifest: ConnectorManifest): RegisterConnectorRequest {
  return {
    id: manifest.id,
    name: manifest.name,
    version: manifest.version,
    auth: manifest.auth,
    scopes: manifest.scopes,
    actions: manifest.actions,
    risk_default: manifest.risk_default,
    first_party: manifest.first_party,
  }
}

/* ------------------------------------------------------------------ *
 * Validation
 * ------------------------------------------------------------------ */

export type ManifestErrorCode = "required" | "invalid_type" | "invalid_value" | "invalid_format" | "duplicate" | "empty"

/** One problem with a manifest, addressed by JSON path. */
export interface ManifestError {
  /** e.g. `"id"`, `"actions[2].risk"`. */
  path: string
  code: ManifestErrorCode
  message: string
}

export type ManifestValidation =
  | { ok: true; manifest: ConnectorManifest; errors: never[] }
  | { ok: false; manifest: null; errors: ManifestError[] }

/** Thrown by {@link defineConnector} and {@link assertValidManifest}. */
export class ConnectorManifestError extends Error {
  readonly errors: ManifestError[]

  constructor(errors: ManifestError[]) {
    const summary = errors.map((e) => `${e.path}: ${e.message}`).join("; ")
    super(`Invalid connector manifest — ${summary}`)
    this.name = "ConnectorManifestError"
    this.errors = errors
  }
}

/** Connector ids are used as URL path segments and as the DB primary key. */
export const CONNECTOR_ID_PATTERN = /^[a-z][a-z0-9_]{1,63}$/
/** Action names appear in audit rows and approval payloads. */
export const ACTION_NAME_PATTERN = /^[a-z][a-z0-9_]{0,63}$/
export const VERSION_PATTERN = /^\d+\.\d+\.\d+$/

export const AUTH_KINDS = ["oauth2", "api_key", "none"] as const satisfies readonly ConnectorAuthKind[]

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

/**
 * Validate an unknown value as a connector manifest.
 *
 * Collects every problem rather than failing on the first, so the connector
 * builder can highlight all of them at once.
 */
export function validateManifest(input: unknown): ManifestValidation {
  const errors: ManifestError[] = []
  const fail = (path: string, code: ManifestErrorCode, message: string): void => {
    errors.push({ path, code, message })
  }

  if (!isRecord(input)) {
    return {
      ok: false,
      manifest: null,
      errors: [{ path: "", code: "invalid_type", message: "Manifest must be an object" }],
    }
  }

  // id
  const id = input["id"]
  if (typeof id !== "string" || id.length === 0) {
    fail("id", "required", "id is required")
  } else if (!CONNECTOR_ID_PATTERN.test(id)) {
    fail("id", "invalid_format", "id must be lower_snake_case, 2–64 chars, starting with a letter")
  }

  // name
  const name = input["name"]
  if (typeof name !== "string" || name.trim().length === 0) {
    fail("name", "required", "name is required")
  }

  // version
  const version = input["version"]
  if (version !== undefined && (typeof version !== "string" || !VERSION_PATTERN.test(version))) {
    fail("version", "invalid_format", "version must look like 1.0.0")
  }

  // auth
  const auth = input["auth"]
  if (typeof auth !== "string" || !(AUTH_KINDS as readonly string[]).includes(auth)) {
    fail("auth", "invalid_value", `auth must be one of ${AUTH_KINDS.join(", ")}`)
  }

  // scopes
  const scopes = input["scopes"]
  if (scopes !== undefined) {
    if (!Array.isArray(scopes)) {
      fail("scopes", "invalid_type", "scopes must be an array of strings")
    } else {
      scopes.forEach((scope, i) => {
        if (typeof scope !== "string" || scope.length === 0) {
          fail(`scopes[${i}]`, "invalid_type", "scope must be a non-empty string")
        }
      })
    }
  }

  // risk_default
  const riskDefault = input["risk_default"]
  if (riskDefault !== undefined && !isRiskClass(riskDefault)) {
    fail("risk_default", "invalid_value", `risk_default must be one of ${RISK_CLASSES.join(", ")}`)
  }

  // first_party
  const firstParty = input["first_party"]
  if (firstParty !== undefined && typeof firstParty !== "boolean") {
    fail("first_party", "invalid_type", "first_party must be a boolean")
  }

  // actions
  const actions = input["actions"]
  if (!Array.isArray(actions)) {
    fail("actions", "required", "actions must be an array (use [] for a catalog-only connector)")
  } else {
    const seen = new Set<string>()
    actions.forEach((raw, i) => {
      const at = `actions[${i}]`
      if (!isRecord(raw)) {
        fail(at, "invalid_type", "action must be an object")
        return
      }
      const actionName = raw["name"]
      if (typeof actionName !== "string" || actionName.length === 0) {
        fail(`${at}.name`, "required", "action name is required")
      } else if (!ACTION_NAME_PATTERN.test(actionName)) {
        fail(`${at}.name`, "invalid_format", "action name must be lower_snake_case")
      } else if (seen.has(actionName)) {
        fail(`${at}.name`, "duplicate", `duplicate action name "${actionName}"`)
      } else {
        seen.add(actionName)
      }

      const description = raw["description"]
      if (typeof description !== "string" || description.trim().length === 0) {
        fail(`${at}.description`, "empty", "every action needs a description — it is shown in approval cards")
      }

      if (!isRiskClass(raw["risk"])) {
        fail(`${at}.risk`, "invalid_value", `risk must be one of ${RISK_CLASSES.join(", ")}`)
      }

      const schema = raw["input_schema"]
      if (!isRecord(schema)) {
        fail(`${at}.input_schema`, "invalid_type", "input_schema must be a JSON Schema object")
      } else if (schema["type"] !== undefined && schema["type"] !== "object") {
        fail(`${at}.input_schema.type`, "invalid_value", 'input_schema.type must be "object"')
      }
    })
  }

  if (errors.length > 0) return { ok: false, manifest: null, errors }

  const record = input as Record<string, unknown>
  const manifest: ConnectorManifest = {
    id: record["id"] as string,
    name: record["name"] as string,
    version: (record["version"] as string | undefined) ?? "1.0.0",
    auth: record["auth"] as ConnectorAuthKind,
    scopes: (record["scopes"] as string[] | undefined) ?? [],
    actions: record["actions"] as ConnectorAction[],
    risk_default: (record["risk_default"] as RiskClass | undefined) ?? "observe",
    first_party: (record["first_party"] as boolean | undefined) ?? false,
  }
  return { ok: true, manifest, errors: [] }
}

function isRiskClass(value: unknown): value is RiskClass {
  return typeof value === "string" && (RISK_CLASSES as readonly string[]).includes(value)
}

/** Validate or throw. Returns the normalised manifest (defaults filled in). */
export function assertValidManifest(input: unknown): ConnectorManifest {
  const result = validateManifest(input)
  if (!result.ok) throw new ConnectorManifestError(result.errors)
  return result.manifest
}
