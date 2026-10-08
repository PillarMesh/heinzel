import {formatInstant} from "../../format/instant"
import {type FormEvent, useEffect, useState} from "react"

import type {
  AcquisitionRunNowCommand,
  AcquisitionReceiptView,
  ConsoleEnvelopeAcquisitionReceiptView,
  ConsoleEnvelopeAcquisitionReceiptsView,
  SessionView,
} from "../../api/generated"
import {
  ConsoleApiError,
  ConsoleMutationOutcomeUnknown,
  type MutationRequestContext,
} from "../../api/client"

/*
 * A receipt is shown as the acquisition runtime recorded it.
 *
 * Refusals are listed beside successes because the receipt carries `outcome` and
 * `reason_codes` precisely so a refusal is publishable, and a refusal an operator
 * cannot see is one they cannot act on. Every identifier is a reference an owning
 * service allocated, so it is shown as a reference and given no invented name.
 */
const outcomeLabels = {
  prepared: "Prepared",
  acknowledged: "Acknowledged",
  no_valid_plan: "No valid plan",
  resynchronization_required: "Resynchronization required",
  failed: "Failed",
} satisfies Record<AcquisitionReceiptView["outcome"], string>

export interface AcquisitionReceiptsClient {
  getAcquisitionReceipts(): Promise<ConsoleEnvelopeAcquisitionReceiptsView>
  runAcquisitionNow?(
    command: AcquisitionRunNowCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeAcquisitionReceiptView>
}

interface AcquisitionReceiptsPageProps {
  readonly client: AcquisitionReceiptsClient
  readonly idempotencyKeyFactory?: () => string
  readonly session?: SessionView
  readonly triggerWindowFactory?: () => string
}

function defaultIdempotencyKey(): string {
  return `acquisition-${globalThis.crypto.randomUUID()}`
}

function currentUtcHourWindow(): string {
  const start = new Date()
  start.setUTCMinutes(0, 0, 0)
  const end = new Date(start.getTime() + 60 * 60 * 1_000)
  return `${start.toISOString()}/${end.toISOString()}`
}

export function AcquisitionReceiptsPage({
  client,
  idempotencyKeyFactory = defaultIdempotencyKey,
  session,
  triggerWindowFactory = currentUtcHourWindow,
}: AcquisitionReceiptsPageProps) {
  const [receipts, setReceipts] = useState<readonly AcquisitionReceiptView[] | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [contractRef, setContractRef] = useState("")
  const [runFailure, setRunFailure] = useState<string | null>(null)
  const [runMessage, setRunMessage] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [uncertainRun, setUncertainRun] = useState<{
    readonly idempotencyKey: string
    readonly triggerWindow: string
  } | null>(null)

  useEffect(() => {
    let abandoned = false
    client
      .getAcquisitionReceipts()
      .then((envelope) => {
        if (!abandoned) setReceipts(envelope.data.receipts ?? [])
      })
      .catch((error: unknown) => {
        if (abandoned) return
        // The server's own safe message, never one composed here.
        setFailure(
          error instanceof ConsoleApiError
            ? error.message
            : "The acquisition receipt listing is unavailable.",
        )
      })
    return () => {
      abandoned = true
    }
  }, [client])

  const activeRole =
    session?.active_role === "data_architect" || session?.active_role === "data_owner"
      ? session.active_role
      : null
  const canRun = client.runAcquisitionNow !== undefined && activeRole !== null

  const runAcquisition = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (
      !canRun ||
      activeRole === null ||
      session === undefined ||
      client.runAcquisitionNow === undefined
    )
      return
    const normalizedContractRef = contractRef.trim()
    if (normalizedContractRef.length === 0) return
    const idempotencyKey = uncertainRun?.idempotencyKey ?? idempotencyKeyFactory()
    const triggerWindow = uncertainRun?.triggerWindow ?? triggerWindowFactory()
    setSubmitting(true)
    setRunFailure(null)
    setRunMessage(null)
    try {
      const response = await client.runAcquisitionNow(
        {
          acquisition_mode: "snapshot",
          active_role: activeRole,
          contract_ref: normalizedContractRef,
          trigger_window: triggerWindow,
        },
        {csrfToken: session.csrf_token, idempotencyKey},
      )
      setReceipts((current) => {
        const withoutReplay = (current ?? []).filter(
          (item) => item.evidence_id !== response.data.evidence_id,
        )
        return [response.data, ...withoutReplay]
      })
      setUncertainRun(null)
      setRunMessage(
        response.data.outcome === "prepared"
          ? "Acquisition prepared. Its verified artifacts and evidence are ready for the next stage."
          : `Acquisition recorded: ${outcomeLabels[response.data.outcome]}.`,
      )
    } catch (error: unknown) {
      if (error instanceof ConsoleMutationOutcomeUnknown) {
        setUncertainRun({idempotencyKey, triggerWindow})
      }
      setRunFailure(
        error instanceof ConsoleApiError || error instanceof ConsoleMutationOutcomeUnknown
          ? error.message
          : "The acquisition could not be started.",
      )
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section aria-labelledby="acquisition-receipts-title" className="summary-page">
      <p className="eyebrow">Governed capability</p>
      <h1 id="acquisition-receipts-title">Acquisition evidence</h1>
      <p className="summary-page__lead">
        Receipts recorded by acquisitions run for this tenant. A receipt records the work
        an acquisition performed; it does not assert that data was delivered anywhere.
      </p>
      {canRun ? (
        <form className="summary-page__action" onSubmit={(event) => void runAcquisition(event)}>
          <label htmlFor="acquisition-contract-ref">Activated contract reference</label>
          <input
            autoComplete="off"
            disabled={submitting || uncertainRun !== null}
            id="acquisition-contract-ref"
            onChange={(event) => setContractRef(event.target.value)}
            placeholder="contract:managed-business-data:v1"
            required
            value={contractRef}
          />
          <button disabled={submitting || contractRef.trim().length === 0} type="submit">
            {submitting
              ? "Running acquisition…"
              : uncertainRun === null
                ? "Run acquisition"
                : "Reconcile acquisition"}
          </button>
          <p className="summary-page__guidance">
            Runs the current activated revision for this UTC hour. Repeating the same hour is
            replay safe.
          </p>
          {runFailure === null ? null : <p role="alert">{runFailure}</p>}
          {runMessage === null ? null : <p role="status">{runMessage}</p>}
        </form>
      ) : null}
      {failure === null ? null : <p role="alert">{failure}</p>}
      {failure !== null || receipts === null ? null : receipts.length === 0 ? (
        <div className="empty-state">
          <strong>No acquisition receipts</strong>
          <p>A receipt is recorded here each time a registered source is acquired.</p>
        </div>
      ) : (
        <ul aria-label="Acquisition receipts" className="capability-ledger">
          {receipts.map((receipt) => (
            <li className="capability-ledger__item" key={receipt.evidence_id}>
              <div>
                <h2>
                  {outcomeLabels[receipt.outcome]} {receipt.acquisition_mode} acquisition
                </h2>
                <p>
                  {receipt.logical_object_refs.join(", ")} · recorded{" "}
                  {formatInstant(receipt.created_at)}
                </p>
                <details className="responsive-disclosure">
                  <summary>Technical references</summary>
                  <p className="capability-ledger__dependency">
                    <span>Evidence</span> <code>{receipt.evidence_id}</code>
                  </p>
                  <p className="capability-ledger__dependency">
                    <span>Contract</span> <code>{receipt.contract_ref}</code>
                  </p>
                  <p className="capability-ledger__dependency">
                    <span>Source binding</span> <code>{receipt.source_binding_ref}</code>
                  </p>
                  {receipt.reason_codes === undefined || receipt.reason_codes.length === 0 ? null : (
                    <p className="capability-ledger__dependency">
                      <span>Reasons</span> <code>{receipt.reason_codes.join(", ")}</code>
                    </p>
                  )}
                </details>
              </div>
              <span>{outcomeLabels[receipt.outcome]}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
