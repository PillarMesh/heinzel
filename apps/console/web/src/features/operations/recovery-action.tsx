import {useState} from "react"

import {ConsoleApiError, type MutationRequestContext} from "../../api/client"
import type {
  ConsoleEnvelopeIncidentView,
  IncidentRecoveryCommand,
  IncidentView,
  OperationalRecoveryAction,
  SessionView,
} from "../../api/generated"

export interface IncidentRecoveryClient {
  recoverIncident(
    incidentId: string,
    command: IncidentRecoveryCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeIncidentView>
}

const actionLabels = {
  cancel_unstarted_work: "Cancel work before it starts",
  reconcile_external_effect: "Reconcile catalog publication",
  retry_transient_attempt: "Retry failed attempt",
} satisfies Record<OperationalRecoveryAction, string>

interface RecoveryActionProps {
  readonly client: IncidentRecoveryClient
  readonly idempotencyKeyFactory: () => string
  readonly incident: IncidentView
  readonly onRecovered: (incident: IncidentView) => void
  readonly session: SessionView
}

export function RecoveryAction({
  client,
  idempotencyKeyFactory,
  incident,
  onRecovered,
  session,
}: RecoveryActionProps) {
  const [reason, setReason] = useState("")
  const [pending, setPending] = useState<OperationalRecoveryAction | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const actions = incident.allowed_operator_actions ?? []

  if (actions.length === 0) {
    return incident.recovery_recorded ? (
      <p className="recovery-action__recorded" role="status">Recovery request recorded.</p>
    ) : null
  }

  const submit = async (action: OperationalRecoveryAction) => {
    const normalizedReason = reason.trim()
    if (normalizedReason.length === 0) return
    if (session.active_role !== "data_architect" && session.active_role !== "data_owner") return
    setPending(action)
    setFailure(null)
    try {
      const response = await client.recoverIncident(
        incident.incident_id,
        {
          action,
          active_role: session.active_role,
          expected_revision: incident.revision,
          reason: normalizedReason,
        },
        {csrfToken: session.csrf_token, idempotencyKey: idempotencyKeyFactory()},
      )
      onRecovered(response.data)
    } catch (error: unknown) {
      setFailure(
        error instanceof ConsoleApiError
          ? error.message
          : "The recovery request could not be completed.",
      )
    } finally {
      setPending(null)
    }
  }

  return (
    <div className="recovery-action">
      <label htmlFor={`incident-reason-${incident.incident_id}`}>Reason for this action</label>
      <textarea
        id={`incident-reason-${incident.incident_id}`}
        onChange={(event) => setReason(event.target.value)}
        placeholder="Describe what changed or why this action is safe."
        rows={3}
        value={reason}
      />
      <div className="recovery-action__buttons">
        {actions.map((action) => (
          <button
            disabled={reason.trim().length === 0 || pending !== null}
            key={action}
            onClick={() => void submit(action)}
            type="button"
          >
            {pending === action ? "Recording…" : actionLabels[action]}
          </button>
        ))}
      </div>
      {failure === null ? null : <p role="alert">{failure}</p>}
    </div>
  )
}
