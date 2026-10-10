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

/*
 * The two modes a run can be commanded in here, and what each one means to the person
 * pressing. `reconciliation` is a third the command surface accepts and this page does not
 * offer: it is a repair of a disagreement between source and destination, not a refresh.
 */
type AcquisitionRunMode = Extract<
  AcquisitionRunNowCommand["acquisition_mode"],
  "snapshot" | "incremental"
>

const modeLabels = {
  snapshot: "Acquire the whole source",
  incremental: "Acquire what has changed since the last run",
} satisfies Record<AcquisitionRunMode, string>

/*
 * A snapshot is admitted only from checkpoint revision 0, and an acknowledged receipt is the
 * record of a checkpoint having advanced. So a contract with one has been acquired, and asking
 * for a snapshot of it is refused -- which is why this decides the default rather than leaving
 * the page on the mode that happens to work once.
 */
function defaultModeFor(
  receipts: readonly AcquisitionReceiptView[] | null,
  contractRef: string,
): AcquisitionRunMode {
  const acquired = (receipts ?? []).some(
    (receipt) => receipt.contract_ref === contractRef && receipt.outcome === "acknowledged",
  )
  return acquired ? "incremental" : "snapshot"
}

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

function runMessageFor(outcome: AcquisitionReceiptView["outcome"]): string {
  if (outcome === "acknowledged") {
    // Said once, in the guidance above the control, where it sets the expectation rather
    // than correcting one.
    return "Acquisition landed as a new generation, and the source now reads from after it."
  }
  if (outcome === "prepared") {
    return "Acquisition prepared. Its verified artifacts and evidence are ready for the next stage."
  }
  return `Acquisition recorded: ${outcomeLabels[outcome]}.`
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
  // Whether a run could be commanded at all, as the read itself reported it.
  const [runAvailable, setRunAvailable] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  const [contractRef, setContractRef] = useState("")
  const [modeSelection, setModeSelection] = useState<AcquisitionRunMode | null>(null)
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
        if (abandoned) return
        setReceipts(envelope.data.receipts ?? [])
        setRunAvailable(envelope.data.run_available ?? false)
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
  /*
    A control is offered only where something could act on it.

    This page listed every receipt an acquisition ever wrote and then offered a form to ask for
    another -- on a deployment that runs no acquisition application, where the press came back
    `capability_not_delivered`. The read now says whether a run can be commanded, the same way
    the publishable-dashboards read says whether publishing can be, so the browser is told
    rather than finding out by pressing.
  */
  const canRun = runAvailable && client.runAcquisitionNow !== undefined && activeRole !== null
  // The chosen mode, or the one this contract's own receipts say is the available one. A
  // choice survives a change of contract only while it remains the right default for it.
  const defaultMode = defaultModeFor(receipts, contractRef.trim())
  const mode = modeSelection ?? defaultMode

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
          acquisition_mode: mode,
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
      setModeSelection(null)
      setRunMessage(runMessageFor(response.data.outcome))
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
      {canRun || receipts === null ? null : (
        <p className="summary-page__guidance">
          Acquisition runs are not commanded from this deployment. The receipts below record the
          runs that have happened.
        </p>
      )}
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
          <label htmlFor="acquisition-mode">What to acquire</label>
          <select
            disabled={submitting || uncertainRun !== null}
            id="acquisition-mode"
            onChange={(event) => setModeSelection(event.target.value as AcquisitionRunMode)}
            value={mode}
          >
            <option value="incremental">{modeLabels.incremental}</option>
            <option value="snapshot">{modeLabels.snapshot}</option>
          </select>
          <button disabled={submitting || contractRef.trim().length === 0} type="submit">
            {submitting
              ? "Running acquisition…"
              : uncertainRun === null
                ? "Run acquisition"
                : "Reconcile acquisition"}
          </button>
          <p className="summary-page__guidance">
            {defaultMode === "incremental"
              ? "This source has been acquired, so only what has changed since can be acquired from it."
              : "This source has not been acquired yet, so the whole of it can be acquired once."}{" "}
            A run lands what it acquires as a new generation. Building a product over that
            generation is a separate step.
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
