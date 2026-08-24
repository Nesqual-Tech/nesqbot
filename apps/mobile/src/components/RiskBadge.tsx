import type { RiskClass } from "@nesqbot/protocol"
import { Badge, type BadgeTone } from "./Badge"

/** Risk classes that must never be approved without an explicit confirmation step. */
const CONFIRM_REQUIRED: readonly RiskClass[] = ["send", "spend", "delete"]

export function requiresConfirmation(risk: RiskClass | string): boolean {
  return CONFIRM_REQUIRED.includes(risk as RiskClass)
}

export function riskTone(risk: RiskClass | string): BadgeTone {
  switch (risk) {
    case "send":
    case "spend":
    case "delete":
      return "danger"
    case "mutate":
      return "warning"
    case "draft":
      return "accent"
    default:
      return "neutral"
  }
}

export function riskDescription(risk: RiskClass | string): string {
  switch (risk) {
    case "observe":
      return "Reads data only."
    case "draft":
      return "Prepares content without sending it."
    case "mutate":
      return "Changes data in a connected system."
    case "send":
      return "Sends something on your behalf. This cannot be unsent."
    case "spend":
      return "Spends money."
    case "delete":
      return "Deletes data permanently."
    default:
      return "Unclassified action."
  }
}

export interface RiskBadgeProps {
  risk: RiskClass | string
}

export function RiskBadge({ risk }: RiskBadgeProps): JSX.Element {
  return (
    <Badge
      label={String(risk).toUpperCase()}
      tone={riskTone(risk)}
      accessibilityLabel={`Risk: ${risk}. ${riskDescription(risk)}`}
    />
  )
}
