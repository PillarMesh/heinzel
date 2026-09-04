import {useEffect, useState} from "react"

import type {
  AcquisitionReceiptView,
  ConsoleEnvelopeAcquisitionReceiptsView,
} from "../../api/generated"
import {ConsoleApiError} from "../../api/client"

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
}

interface AcquisitionReceiptsPageProps {
  readonly client: AcquisitionReceiptsClient
}

export function AcquisitionReceiptsPage({client}: AcquisitionReceiptsPageProps) {
  const [receipts, setReceipts] = useState<readonly AcquisitionReceiptView[] | null>(null)
  const [failure, setFailure] = useState<string | null>(null)

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

  return (
    <section aria-labelledby="acquisition-receipts-title" className="summary-page">
      <p className="eyebrow">Governed capability</p>
      <h1 id="acquisition-receipts-title">Acquisition evidence</h1>
      <p className="summary-page__lead">
        Receipts recorded by acquisitions run for this tenant. A receipt records the work
        an acquisition performed; it does not assert that data was delivered anywhere.
      </p>
      {failure === null ? null : <p role="alert">{failure}</p>}
      {failure !== null || receipts === null ? null : receipts.length === 0 ? (
        <p className="summary-page__guidance">
          No acquisition has recorded a receipt for this tenant.
        </p>
      ) : (
        <ul aria-label="Acquisition receipts" className="capability-ledger">
          {receipts.map((receipt) => (
            <li className="capability-ledger__item" key={receipt.evidence_id}>
              <div>
                <h2>{receipt.evidence_id}</h2>
                <p className="capability-ledger__dependency">
                  <span>Contract</span> <code>{receipt.contract_ref}</code>
                </p>
                <p className="capability-ledger__dependency">
                  <span>Source binding</span> <code>{receipt.source_binding_ref}</code>
                </p>
                <p className="capability-ledger__dependency">
                  <span>Objects</span> <code>{receipt.logical_object_refs.join(", ")}</code>
                </p>
                <p>
                  {receipt.acquisition_mode} acquisition recorded{" "}
                  {new Date(receipt.created_at).toISOString()}
                </p>
                {receipt.reason_codes === undefined || receipt.reason_codes.length === 0 ? null : (
                  <p className="capability-ledger__dependency">
                    <span>Reasons</span> <code>{receipt.reason_codes.join(", ")}</code>
                  </p>
                )}
              </div>
              <span>{outcomeLabels[receipt.outcome]}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
