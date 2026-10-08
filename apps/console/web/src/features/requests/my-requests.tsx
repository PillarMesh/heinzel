import {formatInstant} from "../../format/instant"
import {RecoveryPage} from "../../routes/recovery-page"
import {useCallback, useEffect, useState} from "react"

import type {MutationRequestContext} from "../../api/client"
import type {
  AccessRevocationCommand,
  ClarifiedOutcomeAcceptanceCommand,
  ConsoleEnvelopeClarifiedOutcomeView,
  ConsoleEnvelopeConversationView,
  ConsoleEnvelopeJsonTupleHeinzelConsoleContractsRequesterRequestView,
  ConsoleEnvelopeRequesterRequestView,
  ConsoleEnvelopeSelectableAnswerTermsView,
  ConsoleEnvelopeAccessLifecycleView,
  ConversationMessageCommand,
  CreateRequestCommand,
  DataProvenance,
  RequesterRequestView,
  RequestWithdrawalCommand,
  SessionView,
} from "../../api/generated"
import {ClarifiedOutcome} from "./clarified-outcome"
import {RequestConversation} from "./request-conversation"
import {RequestIntake} from "./request-intake"
import "./requests.css"

/**
 * The only console interfaces this surface may reach for. Reviewer projections -- the inbox, the
 * request detail, and evidence -- are deliberately absent so no requester screen can consume them.
 */
export interface RequesterClient {
  acceptClarifiedOutcome(
    requestId: string,
    command: ClarifiedOutcomeAcceptanceCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeClarifiedOutcomeView>
  appendConversationMessage(
    requestId: string,
    command: ConversationMessageCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeConversationView>
  createRequest(
    command: CreateRequestCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeRequesterRequestView>
  getClarifiedOutcome(requestId: string): Promise<ConsoleEnvelopeClarifiedOutcomeView>
  getConversation(requestId: string): Promise<ConsoleEnvelopeConversationView>
  getRequesterRequests(): Promise<ConsoleEnvelopeJsonTupleHeinzelConsoleContractsRequesterRequestView>
  /** The governed terms a question may be composed from; the builder offers exactly these. */
  getSelectableAnswerTerms(): Promise<ConsoleEnvelopeSelectableAnswerTermsView>
  revokeAccess(
    requestId: string,
    command: AccessRevocationCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeAccessLifecycleView>
  withdrawRequest(
    requestId: string,
    command: RequestWithdrawalCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeRequesterRequestView>
}

export type IdempotencyKeyFactory = () => string
export type DigestText = (value: string) => Promise<string>

interface MyRequestsProps {
  readonly client: RequesterClient
  /** Whether the workspace publishes data-access intake as ready; the server enforces it too. */
  readonly dataAccessAvailable: boolean
  readonly dataProvenance: DataProvenance
  readonly digestText?: DigestText
  readonly idempotencyKeyFactory?: IdempotencyKeyFactory
  readonly requestedRequestRef?: string | undefined
  readonly session: SessionView
}

async function sha256Text(value: string): Promise<string> {
  const digest = await globalThis.crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  )
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("")
}

function defaultIdempotencyKey(): string {
  return `request-${globalThis.crypto.randomUUID()}`
}

function stateLabel(request: RequesterRequestView): string {
  return request.state.replaceAll("_", " ")
}

const terminalStates: ReadonlySet<RequesterRequestView["state"]> = new Set([
  "denied",
  "delivered",
  "no_valid_plan",
  "cancelled",
  "failed",
  "closed",
])

// A terminal request admits no requester decision or reply, whatever explanation it carries.
function isTerminal(request: RequesterRequestView): boolean {
  return terminalStates.has(request.state)
}

function OwnDecisions({request}: {readonly request: RequesterRequestView}) {
  const decisions = request.own_decisions ?? []
  if (decisions.length === 0) {
    return null
  }
  return (
    <div aria-label="Your decisions" className="own-decisions">
      {decisions.map((decision) => (
        <p key={`${decision.subject_label}:${decision.created_at}`}>
          {decision.subject_label}: {decision.decision.replaceAll("_", " ")} ·{" "}
          {decision.created_at}
        </p>
      ))}
    </div>
  )
}

function DeliveredAnswer({request}: {readonly request: RequesterRequestView}) {
  const delivery = request.delivered_answer
  if (delivery === null || delivery === undefined) {
    return null
  }
  const references = [
    ...(delivery.datasets ?? []).map((reference) => ({kind: "Dataset", reference})),
    ...(delivery.metrics ?? []).map((reference) => ({kind: "Metric", reference})),
    ...(delivery.lineage ?? []).map((reference) => ({kind: "Lineage", reference})),
    ...(delivery.quality_limitations ?? []).map((reference) => ({
      kind: "Quality limitation",
      reference,
    })),
  ]
  return (
    <section aria-labelledby="delivered-answer-title" className="delivered-answer">
      <p className="eyebrow">Governed delivery</p>
      <h2 id="delivered-answer-title">Delivered answer</h2>
      <p className="delivered-answer__text">{delivery.answer_text}</p>
      <p className="delivered-answer__context">
        As of {delivery.as_of} · freshness {delivery.freshness.replaceAll("_", " ")}
      </p>
      <p className="delivered-answer__context">
        Checked against the workspace's recorded warehouse binding and catalog publication.
      </p>
      {references.length === 0 ? null : (
        <ul aria-label="Governed answer references" className="delivered-answer__references">
          {references.map(({kind, reference}) => (
            <li key={`${kind}:${reference.digest}`}>
              {kind}: {deliveryReferenceLabel(kind, reference.artifact_id)} (version{" "}
              {reference.version})
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function DeliveredAccess({request}: {readonly request: RequesterRequestView}) {
  const delivery = request.delivered_access
  if (
    delivery === null ||
    delivery === undefined ||
    request.access_lifecycle?.state !== "active"
  ) {
    return null
  }
  return (
    <section aria-labelledby="delivered-access-title" className="delivered-answer">
      <p className="eyebrow">Governed delivery</p>
      <h2 id="delivered-access-title">Access is ready</h2>
      <p className="delivered-answer__text">
        You can {delivery.access_mode} the approved fields: {delivery.fields.join(", ")}.
      </p>
      <p className="delivered-answer__context">
        Available until {formatInstant(delivery.expires_at)}. Access expires automatically.
      </p>
      <p className="delivered-answer__context">
        Permissions: {delivery.permissions.join(", ")}.
      </p>
    </section>
  )
}

interface AccessLifecycleProps {
  readonly client: RequesterClient
  readonly idempotencyKeyFactory: IdempotencyKeyFactory
  readonly onRevoked: () => void
  readonly request: RequesterRequestView
  readonly session: SessionView
}

function AccessLifecycle({
  client,
  idempotencyKeyFactory,
  onRevoked,
  request,
  session,
}: AccessLifecycleProps) {
  const lifecycle = request.access_lifecycle
  const [confirming, setConfirming] = useState(false)
  const [reason, setReason] = useState("")
  const [submitting, setSubmitting] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  if (lifecycle === null || lifecycle === undefined) {
    return null
  }
  const expectedRevision = lifecycle.revision

  async function revoke(): Promise<void> {
    const explanatoryReason = reason.trim()
    if (explanatoryReason.length === 0) {
      setFailure("Explain why access should be removed.")
      return
    }
    setSubmitting(true)
    setFailure(null)
    try {
      await client.revokeAccess(
        request.request_id,
        {
          expected_revision: expectedRevision,
          active_role: "requester",
          reason: explanatoryReason,
        },
        {csrfToken: session.csrf_token, idempotencyKey: idempotencyKeyFactory()},
      )
      onRevoked()
    } catch {
      setFailure("Access removal could not be confirmed. Reload the request before trying again.")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section aria-labelledby="access-lifecycle-title" className="access-lifecycle">
      <p className="eyebrow">Access status</p>
      <h2 id="access-lifecycle-title">{lifecycle.title}</h2>
      <p className="access-lifecycle__summary">{lifecycle.summary}</p>
      <p className="access-lifecycle__term">
        Approved term: {formatInstant(lifecycle.effective_at)} to {formatInstant(lifecycle.expires_at)}.
      </p>
      {lifecycle.can_revoke ? (
        confirming ? (
          <div className="access-lifecycle__revocation">
            <label htmlFor="requester-access-revocation-reason">Reason for removing access</label>
            <textarea
              disabled={submitting}
              id="requester-access-revocation-reason"
              maxLength={512}
              onChange={(event) => setReason(event.target.value)}
              rows={3}
              value={reason}
            />
            <button disabled={submitting} onClick={() => void revoke()} type="button">
              {submitting ? "Removing access…" : "Confirm access removal"}
            </button>
            <button disabled={submitting} onClick={() => setConfirming(false)} type="button">
              Keep access
            </button>
          </div>
        ) : (
          <button onClick={() => setConfirming(true)} type="button">
            Remove access
          </button>
        )
      ) : null}
      {failure === null ? null : <p role="alert">{failure}</p>}
    </section>
  )
}

// Withdrawal is offered only before any work is admitted; the owning service refuses it after.
const withdrawableStates: ReadonlySet<RequesterRequestView["state"]> = new Set([
  "submitted",
  "clarifying",
  "investigating",
  "proposed",
  "awaiting_approval",
])

interface WithdrawRequestProps {
  readonly client: RequesterClient
  readonly idempotencyKeyFactory: IdempotencyKeyFactory
  readonly onWithdrawn: () => void
  readonly request: RequesterRequestView
  readonly session: SessionView
}

function WithdrawRequest({
  client,
  idempotencyKeyFactory,
  onWithdrawn,
  request,
  session,
}: WithdrawRequestProps) {
  const [confirming, setConfirming] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)

  if (!withdrawableStates.has(request.state)) {
    return null
  }

  async function withdraw(): Promise<void> {
    setSubmitting(true)
    setFailure(null)
    try {
      await client.withdrawRequest(
        request.request_id,
        {expected_revision: request.revision, active_role: "requester"},
        {csrfToken: session.csrf_token, idempotencyKey: idempotencyKeyFactory()},
      )
      onWithdrawn()
    } catch {
      setFailure("The request could not be withdrawn. Reload it and try again.")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="request-summary__withdrawal">
      {confirming ? (
        <>
          <p>Withdrawing closes this request. Heinzel will not prepare or deliver an answer.</p>
          <button disabled={submitting} onClick={() => void withdraw()} type="button">
            {submitting ? "Withdrawing…" : "Confirm withdrawal"}
          </button>
          <button disabled={submitting} onClick={() => setConfirming(false)} type="button">
            Keep request
          </button>
        </>
      ) : (
        <button onClick={() => setConfirming(true)} type="button">
          Withdraw request
        </button>
      )}
      {failure === null ? null : <p role="alert">{failure}</p>}
    </div>
  )
}

function deliveryReferenceLabel(kind: string, artifactId: string): string {
  if (kind === "Lineage") return "Governed lineage"
  if (kind === "Quality limitation") return "Governed quality observation"
  const withoutTechnicalPrefix = artifactId.replace(/^(product|metric)-/, "")
  return withoutTechnicalPrefix
    .replaceAll(/[-_]+/g, " ")
    .replace(/^./, (first) => first.toUpperCase())
}

export function MyRequests({
  client,
  dataAccessAvailable,
  dataProvenance,
  digestText = sha256Text,
  idempotencyKeyFactory = defaultIdempotencyKey,
  requestedRequestRef,
  session,
}: MyRequestsProps) {
  const [requests, setRequests] = useState<readonly RequesterRequestView[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [reloadToken, setReloadToken] = useState(0)
  const [revisingRequest, setRevisingRequest] = useState(false)
  const reload = useCallback(() => setReloadToken((token) => token + 1), [])

  useEffect(() => {
    let active = true
    void client
      .getRequesterRequests()
      .then((envelope) => {
        if (!active) {
          return
        }
        if (envelope.meta.data_provenance !== dataProvenance) {
          setFailed(true)
          return
        }
        setFailed(false)
        setRequests(envelope.data)
      })
      .catch(() => {
        if (active) {
          setFailed(true)
        }
      })
    return () => {
      active = false
    }
  }, [client, dataProvenance, reloadToken])

  if (failed) {
    // A whole page that cannot be shown is a recovery boundary, not a sentence. It used to
    // render as one line of unheaded prose with no way off the page.
    return <RecoveryPage detail="Your requests could not be displayed safely." kind="view" />
  }
  if (requests === null) {
    return <p role="status">Loading your requests…</p>
  }

  if (requestedRequestRef !== undefined) {
    const selected = requests.find((request) => request.request_id === requestedRequestRef)
    // A request owned by somebody else and a request that never existed are the same denial, so
    // nothing here can confirm that another request exists.
    if (selected === undefined) {
      return <p role="alert">The requested resource is unavailable.</p>
    }
    const revisedRequestDraft =
      selected.state === "no_valid_plan" &&
      selected.kind === "stakeholder_question" &&
      selected.question !== null &&
      selected.question !== undefined &&
      selected.no_valid_plan_explanation !== null &&
      selected.no_valid_plan_explanation !== undefined
        ? {
            kind: "stakeholder_question" as const,
            title: selected.title,
            purpose: selected.requested_outcome,
            question: selected.question,
          }
        : null
    const terminal = isTerminal(selected)
    const unexplainedClosure =
      terminal &&
      (selected.denial_explanation === null || selected.denial_explanation === undefined) &&
      (selected.no_valid_plan_explanation === null ||
        selected.no_valid_plan_explanation === undefined) &&
      (selected.delivered_answer === null || selected.delivered_answer === undefined) &&
      (selected.delivered_access === null || selected.delivered_access === undefined) &&
      (selected.access_lifecycle === null || selected.access_lifecycle === undefined)
    return (
      <div className="requester-surface">
        <section aria-labelledby="request-title" className="request-summary">
          <p className="eyebrow">Your request</p>
          <h1 id="request-title">{selected.title}</h1>
          <p className="request-summary__outcome">{selected.requested_outcome}</p>
          {selected.question === null || selected.question === undefined ? null : (
            <div className="request-summary__question">
              <p className="eyebrow">Original question</p>
              <p>{selected.question}</p>
            </div>
          )}
          <p className="request-summary__state">
            Lifecycle state: <strong>{stateLabel(selected)}</strong> · updated {selected.updated_at}
          </p>
          <details>
            <summary>Technical details</summary>
            <dl>
              <dt>Request reference</dt>
              <dd>{selected.request_id}</dd>
              <dt>Revision</dt>
              <dd>Revision {selected.revision}</dd>
            </dl>
          </details>
          {selected.denial_explanation === null ||
          selected.denial_explanation === undefined ? null : (
            <p className="request-summary__denial">{selected.denial_explanation}</p>
          )}
          {selected.no_valid_plan_explanation === null ||
          selected.no_valid_plan_explanation === undefined ? null : (
            <p className="request-summary__no-valid-plan">
              {selected.no_valid_plan_explanation}
            </p>
          )}
          {unexplainedClosure ? (
            <p className="request-summary__closed">
              This request is closed. No further action is needed from you.
            </p>
          ) : null}
          <OwnDecisions request={selected} />
          <WithdrawRequest
            client={client}
            idempotencyKeyFactory={idempotencyKeyFactory}
            onWithdrawn={reload}
            request={selected}
            session={session}
          />
          {revisedRequestDraft !== null && !revisingRequest ? (
            <button
              className="request-summary__recovery"
              onClick={() => setRevisingRequest(true)}
              type="button"
            >
              Start revised request
            </button>
          ) : null}
        </section>
        <AccessLifecycle
          client={client}
          idempotencyKeyFactory={idempotencyKeyFactory}
          onRevoked={reload}
          request={selected}
          session={session}
        />
        <DeliveredAnswer request={selected} />
        <DeliveredAccess request={selected} />
        {selected.result_page_available ? (
          <p>
            <a href={`/requests/${encodeURIComponent(selected.request_id)}/result`}>View results</a>
          </p>
        ) : null}
        {revisedRequestDraft !== null && revisingRequest ? (
          <RequestIntake
            client={client}
            dataAccessAvailable={dataAccessAvailable}
            digestText={digestText}
            idempotencyKeyFactory={idempotencyKeyFactory}
            initialDraft={revisedRequestDraft}
            onCreated={reload}
            session={session}
          />
        ) : null}
        {terminal ? null : (
          <>
            {selected.clarified_outcome === null || selected.clarified_outcome === undefined ? (
              // The list projection already says no outcome exists, so asking for one would only
              // produce an expected not-found response.
              <p>No clarified outcome has been prepared for this request yet.</p>
            ) : (
              <ClarifiedOutcome
                key={`${selected.request_id}:${selected.revision}`}
                client={client}
                dataProvenance={dataProvenance}
                idempotencyKeyFactory={idempotencyKeyFactory}
                onDecided={reload}
                requestId={selected.request_id}
                session={session}
              />
            )}
            <RequestConversation
              onReplied={reload}
              client={client}
              dataProvenance={dataProvenance}
              idempotencyKeyFactory={idempotencyKeyFactory}
              requestId={selected.request_id}
              session={session}
            />
          </>
        )}
      </div>
    )
  }

  return (
    <div className="requester-surface">
      <section aria-labelledby="my-requests-title" className="request-follow">
        <p className="eyebrow">Follow</p>
        <h1 id="my-requests-title">My requests</h1>
        {requests.length === 0 ? (
          <p>You have not submitted a request yet.</p>
        ) : (
          <ul aria-label="My requests" className="request-list">
            {requests.map((request) => (
              <li className="request-list__item" key={request.request_id}>
                <h2>{request.title}</h2>
                <p>{request.requested_outcome}</p>
                <p className="request-list__state">
                  Lifecycle state: <strong>{stateLabel(request)}</strong> · updated{" "}
                  {request.updated_at}
                </p>
                {isTerminal(request) ||
                request.clarified_outcome === null ||
                request.clarified_outcome === undefined ? null : (
                  <p className="request-list__acceptance">
                    {request.clarified_outcome.accepted
                      ? "You accepted the clarified outcome."
                      : "Your acceptance of the clarified outcome is outstanding."}
                  </p>
                )}
                {request.denial_explanation === null ||
                request.denial_explanation === undefined ? null : (
                  <p className="request-list__denial">{request.denial_explanation}</p>
                )}
                <OwnDecisions request={request} />
                <a
                  aria-label={`Open ${request.title}`}
                  href={`/requests/${request.request_id}`}
                >
                  Open request
                </a>
              </li>
            ))}
          </ul>
        )}
      </section>
      <RequestIntake
        client={client}
        dataAccessAvailable={dataAccessAvailable}
        digestText={digestText}
        idempotencyKeyFactory={idempotencyKeyFactory}
        onCreated={reload}
        session={session}
      />
    </div>
  )
}
