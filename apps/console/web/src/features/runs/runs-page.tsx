import {useEffect, useState} from "react"

import type {ConsoleEnvelopeRunsView, RunView} from "../../api/generated"
import {ConsoleApiError} from "../../api/client"

/*
 * A run is shown as the evidence store witnessed it.
 *
 * The state vocabulary is the store's own rather than the shared `OperationState`,
 * because `non_conforming` is a witnessed outcome and the nearest shared value,
 * `outcome_unknown`, would report it as ignorance. The contract digest is shown in
 * place of a product name because no service asserts one.
 */
const stateLabels = {
  created: "Created",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  non_conforming: "Non-conforming",
} satisfies Record<RunView["state"], string>

export interface RunsClient {
  getRuns(): Promise<ConsoleEnvelopeRunsView>
}

interface RunsPageProps {
  readonly client: RunsClient
}

export function RunsPage({client}: RunsPageProps) {
  const [runs, setRuns] = useState<readonly RunView[] | null>(null)
  const [failure, setFailure] = useState<string | null>(null)

  useEffect(() => {
    let abandoned = false
    client
      .getRuns()
      .then((envelope) => {
        if (!abandoned) setRuns(envelope.data.runs ?? [])
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
        Runs witnessed under the contracts this tenant has activated.
      </p>
      {failure === null ? null : <p role="alert">{failure}</p>}
      {failure !== null || runs === null ? null : runs.length === 0 ? (
        <p className="summary-page__guidance">
          No runs have been recorded under this tenant&rsquo;s activated contracts.
        </p>
      ) : (
        <ul aria-label="Recorded runs" className="capability-ledger">
          {runs.map((run) => (
            <li className="capability-ledger__item" key={run.run_id}>
              <div>
                <h2>{run.run_id}</h2>
                <p className="capability-ledger__dependency">
                  <span>Contract</span> <code>{run.contract_digest}</code>
                </p>
                <p>
                  Recorded {new Date(run.created_at).toISOString()}, last changed{" "}
                  {new Date(run.updated_at).toISOString()}
                </p>
              </div>
              <span>{stateLabels[run.state]}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
