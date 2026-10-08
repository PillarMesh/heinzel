import {useEffect, useState} from "react"

import type {
  ConsoleEnvelopeRunsView,
  LeasedRunView,
  RunAttemptView,
  RunView,
} from "../../api/generated"
import {ConsoleApiError} from "../../api/client"

/* The state vocabulary is the evidence store's own. Internal references stay in
 * the disclosure until an owning service can project a human product label. */
const stateLabels = {
  created: "Created",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  non_conforming: "Non-conforming",
} satisfies Record<RunView["state"], string>

/* State's own lifecycle reading. Retry and cancellation are not offered here: they
 * belong to the incident recovery flow on the Operations page. */
const leasedStatusLabels = {
  pending: "not yet started",
  leased: "running under a lease",
  lease_expired: "lease expired, awaiting a new attempt",
  retryable: "failed and retryable",
  succeeded: "succeeded",
  failed: "failed permanently",
  cancelled: "cancelled",
} satisfies Record<LeasedRunView["status"], string>

const triggerLabels = {
  scheduled: "Scheduled",
  run_now: "Run-now",
  backfill: "Backfill",
  retry: "Retry",
} satisfies Record<LeasedRunView["trigger_reason"], string>

function attemptSummary(attempt: RunAttemptView): string {
  const label = `Attempt ${attempt.attempt_number}, epoch ${attempt.epoch}`
  if (attempt.outcome === "succeeded") return `${label}: succeeded`
  if (attempt.outcome === "failed") return `${label}: failed (${attempt.failure_classification})`
  return `${label}: lease ended without an outcome`
}

function LeasedRun({run}: {readonly run: LeasedRunView}) {
  const attempts = run.attempts ?? []
  const latest = attempts.at(-1)
  return (
    <li className="capability-ledger__item">
      <div>
        <h2>
          {triggerLabels[run.trigger_reason]} run, {leasedStatusLabels[run.status]}
        </h2>
        <p>
          Window {new Date(run.window_starts_at).toISOString()} to{" "}
          {new Date(run.window_ends_at).toISOString()}.{" "}
          {latest === undefined
            ? "No attempts yet."
            : `${attempts.length} ${attempts.length === 1 ? "attempt" : "attempts"}, latest epoch ${latest.epoch}.`}
        </p>
        <details>
          <summary>Technical details</summary>
          <dl>
            <dt>Run reference</dt>
            <dd><code>{run.run_id}</code></dd>
            <dt>Contract</dt>
            <dd><code>{run.contract_id}</code> revision {run.contract_revision}</dd>
            <dt>Last durable boundary</dt>
            <dd>
              {run.last_durable_boundary_ref == null ? (
                "No durable boundary proved yet."
              ) : (
                <code>{run.last_durable_boundary_ref}</code>
              )}
            </dd>
            <dt>Observed</dt>
            <dd>{new Date(run.observed_at).toISOString()}</dd>
          </dl>
          {attempts.length === 0 ? null : (
            <ol aria-label="Attempts">
              {attempts.map((attempt) => (
                <li key={attempt.attempt_number}>
                  <p>
                    {attemptSummary(attempt)}. Worker <code>{attempt.worker_ref}</code>, claimed{" "}
                    {new Date(attempt.claimed_at).toISOString()}, lease until{" "}
                    {new Date(attempt.lease_expires_at).toISOString()}
                    {(attempt.lease_extensions ?? 0) === 0
                      ? "."
                      : ` after ${attempt.lease_extensions} ${attempt.lease_extensions === 1 ? "renewal" : "renewals"}.`}
                  </p>
                  {attempt.durable_boundary_ref == null ? null : (
                    <p>
                      Boundary <code>{attempt.durable_boundary_ref}</code>
                    </p>
                  )}
                </li>
              ))}
            </ol>
          )}
        </details>
      </div>
    </li>
  )
}

export interface RunsClient {
  getRuns(): Promise<ConsoleEnvelopeRunsView>
}

interface RunsPageProps {
  readonly client: RunsClient
}

export function RunsPage({client}: RunsPageProps) {
  const [runs, setRuns] = useState<readonly RunView[] | null>(null)
  const [leasedRuns, setLeasedRuns] = useState<readonly LeasedRunView[]>([])
  const [leasedRunsAvailable, setLeasedRunsAvailable] = useState(true)
  const [failure, setFailure] = useState<string | null>(null)

  useEffect(() => {
    let abandoned = false
    client
      .getRuns()
      .then((envelope) => {
        if (abandoned) return
        setLeasedRuns(envelope.data.leased_runs ?? [])
        setLeasedRunsAvailable(envelope.data.leased_runs_available ?? true)
        setRuns(envelope.data.runs ?? [])
      })
      .catch((error: unknown) => {
        if (abandoned) return
        // The server's own safe message, never one composed here.
        setFailure(error instanceof ConsoleApiError ? error.message : "The run listing is unavailable.")
      })
    return () => {
      abandoned = true
    }
  }, [client])

  return (
    <section aria-labelledby="runs-title" className="summary-page">
      <p className="eyebrow">Governed capability</p>
      <h1 id="runs-title">Runs</h1>
      <p className="summary-page__lead">
        Runs owned by state under this tenant&rsquo;s contracts, and runs witnessed under the
        contracts it has activated.
      </p>
      {failure === null ? null : <p role="alert">{failure}</p>}
      {failure !== null || runs === null || leasedRunsAvailable ? null : (
        <p role="status">State-owned runs are unavailable right now; witnessed runs are shown below.</p>
      )}
      {failure !== null || runs === null || leasedRuns.length === 0 ? null : (
        <ul aria-label="State-owned runs" className="capability-ledger">
          {leasedRuns.map((run) => (
            <LeasedRun key={run.run_id} run={run} />
          ))}
        </ul>
      )}
      {failure !== null || runs === null ? null : runs.length === 0 ? (
        leasedRuns.length > 0 || !leasedRunsAvailable ? null : (
        <div className="empty-state">
          <strong>No runs recorded</strong>
          <p>Runs appear here once an activated contract executes.</p>
        </div>
        )
      ) : (
        <ul aria-label="Recorded runs" className="capability-ledger">
          {runs.map((run) => (
            <li className="capability-ledger__item" key={run.run_id}>
              <div>
                <h2>{stateLabels[run.state]} run</h2>
                <p>
                  Recorded {new Date(run.created_at).toISOString()}, last changed{" "}
                  {new Date(run.updated_at).toISOString()}
                </p>
                <details>
                  <summary>Technical details</summary>
                  <dl>
                    <dt>Run reference</dt>
                    <dd><code>{run.run_id}</code></dd>
                    <dt>Contract digest</dt>
                    <dd><code>{run.contract_digest}</code></dd>
                  </dl>
                </details>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
