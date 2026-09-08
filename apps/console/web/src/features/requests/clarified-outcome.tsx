import {useEffect, useState} from "react"

import {ConsoleApiError, ConsoleMutationOutcomeUnknown} from "../../api/client"
import type {ClarifiedOutcomeView, DataProvenance, SessionView} from "../../api/generated"
import type {IdempotencyKeyFactory, RequesterClient} from "./my-requests"

interface ClarifiedOutcomeProps {
  readonly client: RequesterClient
  readonly dataProvenance: DataProvenance
  readonly idempotencyKeyFactory: IdempotencyKeyFactory
  readonly onDecided: () => void
  readonly requestId: string
  readonly session: SessionView
}

export function ClarifiedOutcome({
  client,
  dataProvenance,
  idempotencyKeyFactory,
  onDecided,
  requestId,
  session,
}: ClarifiedOutcomeProps) {
  const [outcome, setOutcome] = useState<ClarifiedOutcomeView | null>(null)
  const [absent, setAbsent] = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [decisionError, setDecisionError] = useState<string | null>(null)
  const [recordedRevision, setRecordedRevision] = useState<number | null>(null)

  useEffect(() => {
    let active = true
    void client
      .getClarifiedOutcome(requestId)
      .then((envelope) => {
        if (!active) {
          return
        }
        if (
          envelope.meta.data_provenance !== dataProvenance ||
          envelope.data.request_id !== requestId
        ) {
          setAbsent(true)
          return
        }
        setOutcome(envelope.data)
      })
      .catch(() => {
        if (active) {
          setAbsent(true)
        }
      })
    return () => {
      active = false
    }
  }, [client, dataProvenance, requestId])

  async function reloadOutcome(): Promise<boolean> {
    try {
      const envelope = await client.getClarifiedOutcome(requestId)
      if (
        envelope.meta.data_provenance !== dataProvenance ||
        envelope.data.request_id !== requestId
      ) {
        return false
      }
      setOutcome(envelope.data)
      setConfirmed(false)
      return true
    } catch {
      return false
    }
  }

  async function decide(decision: "approve" | "request_changes"): Promise<void> {
    if (outcome === null || !confirmed || session.active_role !== "requester") {
      return
    }
    setSubmitting(true)
    setDecisionError(null)
    try {
      const envelope = await client.acceptClarifiedOutcome(
        requestId,
        {
          expected_revision: outcome.revision,
          clarified_outcome_digest: outcome.statement_digest,
          active_role: "requester",
          decision,
        },
        {csrfToken: session.csrf_token, idempotencyKey: idempotencyKeyFactory()},
      )
      if (
        envelope.meta.data_provenance !== dataProvenance ||
        envelope.data.request_id !== requestId
      ) {
        setDecisionError("The clarified outcome could not be reconciled safely.")
        return
      }
      setOutcome(envelope.data)
      setConfirmed(false)
      setRecordedRevision(envelope.data.revision)
      onDecided()
    } catch (error: unknown) {
      if (
        error instanceof ConsoleApiError &&
        error.status === 409 &&
        error.recoveryAction === "reload"
      ) {
        const reloaded = await reloadOutcome()
        setDecisionError(
          reloaded
            ? "The clarified outcome changed; review the new statement before responding."
            : "The refreshed clarified outcome could not be reconciled safely.",
        )
      } else if (error instanceof ConsoleMutationOutcomeUnknown) {
        setDecisionError("The acceptance outcome is unknown. Reload before deciding again.")
      } else {
        setDecisionError("The clarified outcome decision could not be reconciled safely.")
      }
    } finally {
      setSubmitting(false)
    }
  }

  if (absent) {
    return <p role="status">No clarified outcome has been prepared for this request yet.</p>
  }
  if (outcome === null) {
    return <p role="status">Loading the clarified outcome…</p>
  }

  const decisionsDisabled = !confirmed || submitting || session.active_role !== "requester"

  return (
    <section aria-label="Clarified outcome" className="clarified-outcome">
      <h2>Accept</h2>
      <dl>
        <div>
          <dt>Restated request</dt>
          <dd>{outcome.restated_request}</dd>
        </div>
        <div>
          <dt>Purpose</dt>
          <dd>{outcome.purpose}</dd>
        </div>
        <div>
          <dt>In scope</dt>
          <dd>{outcome.in_scope_summary}</dd>
        </div>
        <div>
          <dt>Out of scope</dt>
          <dd>{outcome.out_of_scope_summary}</dd>
        </div>
      </dl>
      {outcome.accepted ? (
        <>
          <p role="status">
            Acceptance recorded at revision {recordedRevision ?? outcome.revision}. Your approval of this scope is recorded.
          </p>
          <p>The answer stays withheld until a verified delivery receipt exists.</p>
        </>
      ) : (
        <>
          <p className="clarified-outcome__withheld">
            Review and accept the clarified scope before this request can be admitted.
          </p>
          <label className="digest-confirmation">
            <input
              checked={confirmed}
              onChange={(event) => setConfirmed(event.currentTarget.checked)}
              type="checkbox"
            />
            <span>I confirm the exact clarified-outcome digest {outcome.statement_digest}.</span>
          </label>
          <div className="clarified-outcome__actions">
            <button
              className="primary-action"
              disabled={decisionsDisabled}
              onClick={() => void decide("approve")}
              type="button"
            >
              {submitting ? "Recording acceptance…" : "Accept clarified outcome"}
            </button>
            <button
              disabled={decisionsDisabled}
              onClick={() => void decide("request_changes")}
              type="button"
            >
              Request changes
            </button>
          </div>
        </>
      )}
      {decisionError === null ? null : <p role="alert">{decisionError}</p>}
    </section>
  )
}
