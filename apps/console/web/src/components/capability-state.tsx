import type {CapabilityState as CapabilityStateValue} from "../api/generated"

const stateLabels = {
  ready: "Ready",
  blocked: "Blocked",
  degraded: "Degraded",
  not_delivered: "Not delivered",
} satisfies Record<CapabilityStateValue, string>

interface CapabilityStateProps {
  readonly state: CapabilityStateValue
}

export function CapabilityState({state}: CapabilityStateProps) {
  return (
    <span className={`capability-state capability-state--${state}`}>
      <span aria-hidden="true" className="capability-state__mark" />
      {stateLabels[state]}
    </span>
  )
}
