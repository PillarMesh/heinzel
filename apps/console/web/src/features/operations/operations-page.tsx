import {PageFailure} from "../../components/unavailable"
import {asPageFailure, type PageFailureState} from "../../format/failure"
import {useEffect, useState} from "react"

import type {
  ConsoleEnvelopeIncidentsView,
  IncidentAutomaticAction,
  IncidentFailureClassification,
  IncidentKind,
  IncidentStage,
  IncidentView,
  SessionView,
} from "../../api/generated"
import {RecoveryAction, type IncidentRecoveryClient} from "./recovery-action"
import "./operations.css"

export interface OperationsClient extends IncidentRecoveryClient {
  getIncidents(): Promise<ConsoleEnvelopeIncidentsView>
}

const kindLabels = {
  catalog_pending: "Catalog publication pending",
  checkpoint_conflict: "Checkpoint conflict",
  dashboard_drift: "Dashboard drift",
  no_valid_plan: "No valid plan",
  query_failure: "Governed query failed",
  revocation_pending: "Access revocation pending",
  schema_drift: "Source schema changed",
  source_unavailable: "Source unavailable",
  stuck_lease: "Run lease is stuck",
  transform_rejection: "Transformation rejected",
} satisfies Record<IncidentKind, string>

const classificationLabels = {
  ambiguous_outcome: "Outcome needs reconciliation",
  authorization_denied: "Permission denied",
  conflict: "Changed while processing",
  integrity_failure: "Integrity check failed",
  no_valid_plan: "No valid plan",
  permanent: "Permanent failure",
  transient: "Temporary failure",
} satisfies Record<IncidentFailureClassification, string>

const stageLabels = {
  access_revocation: "Access revocation",
  catalog_publication: "Catalog publication",
  contract_activation: "Contract activation",
  dashboard_publication: "Dashboard publication",
  extract: "Source extraction",
  governed_query: "Governed query",
  land: "Warehouse landing",
  request_intake: "Request intake",
  transform: "Transformation",
} satisfies Record<IncidentStage, string>

const automaticActionLabels = {
  reconcile_external_effect: "Heinzel will reconcile the external effect before continuing.",
  retry_transient_attempt: "Heinzel will retry the failed attempt automatically.",
} satisfies Record<IncidentAutomaticAction, string>

function defaultIdempotencyKey(): string {
  return `incident-${globalThis.crypto.randomUUID()}`
}

interface OperationsPageProps {
  readonly client: OperationsClient
  readonly idempotencyKeyFactory?: () => string
  readonly session: SessionView
}

export function OperationsPage({
  client,
  idempotencyKeyFactory = defaultIdempotencyKey,
  session,
}: OperationsPageProps) {
  const [incidents, setIncidents] = useState<readonly IncidentView[] | null>(null)
  const [failure, setFailure] = useState<PageFailureState | null>(null)

  useEffect(() => {
    let abandoned = false
    client
      .getIncidents()
      .then((response) => {
        if (!abandoned) setIncidents(response.data.incidents ?? [])
      })
      .catch((error: unknown) => {
        if (!abandoned) {
          setFailure(asPageFailure(error, "The incident listing is unavailable."))
        }
      })
    return () => {
      abandoned = true
    }
  }, [client])

  const replaceIncident = (replacement: IncidentView) => {
    setIncidents((current) =>
      current?.map((incident) =>
        incident.incident_id === replacement.incident_id ? replacement : incident,
      ) ?? null,
    )
  }

  return (
    <section aria-labelledby="operations-title" className="operations-page">
      <p className="eyebrow">Governed capability</p>
      <h1 id="operations-title">Incidents</h1>
      <p className="operations-page__lead">
        Review failures and use only the recovery actions admitted by current run state.
      </p>
      <PageFailure expects="Failures from governed runs, with the recovery actions the current run state admits." failure={failure} />
      {failure !== null || incidents === null ? null : incidents.length === 0 ? (
        <div className="empty-state">
          <strong>No active incidents</strong>
          <p>Failures appear here with the recovery actions the current run state admits.</p>
        </div>
      ) : (
        <ul aria-label="Current incidents" className="incident-list">
          {incidents.map((incident) => (
            <li className="incident-card" key={incident.incident_id}>
              <header>
                <p>{classificationLabels[incident.classification]}</p>
                <h2>{kindLabels[incident.kind]}</h2>
              </header>
              <dl>
                <div>
                  <dt>Last successful stage</dt>
                  <dd>
                    {incident.last_successful_stage === null || incident.last_successful_stage === undefined
                      ? "No stage completed"
                      : stageLabels[incident.last_successful_stage]}
                  </dd>
                </div>
                <div>
                  <dt>Failed stage</dt>
                  <dd>{stageLabels[incident.failed_stage]}</dd>
                </div>
                <div>
                  <dt>Impact</dt>
                  <dd>{incident.user_impact}</dd>
                </div>
              </dl>
              {incident.next_automatic_action === null ||
              incident.next_automatic_action === undefined ? null : (
                <p className="incident-card__next">
                  {automaticActionLabels[incident.next_automatic_action]}
                </p>
              )}
              <RecoveryAction
                client={client}
                idempotencyKeyFactory={idempotencyKeyFactory}
                incident={incident}
                onRecovered={replaceIncident}
                session={session}
              />
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
