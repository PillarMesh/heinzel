import {useCallback, useEffect, useState} from "react"

import type {MutationRequestContext} from "../../api/client"
import type {
  ClarifiedOutcomeAcceptanceCommand,
  ConsoleEnvelopeClarifiedOutcomeView,
  ConsoleEnvelopeConversationView,
  ConsoleEnvelopeJsonTuplePillarmeshConsoleContractsRequesterRequestView,
  ConsoleEnvelopeRequesterRequestView,
  ConversationMessageCommand,
  CreateRequestCommand,
  DataProvenance,
  RequesterRequestView,
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
  getRequesterRequests(): Promise<ConsoleEnvelopeJsonTuplePillarmeshConsoleContractsRequesterRequestView>
}

export type IdempotencyKeyFactory = () => string
export type DigestText = (value: string) => Promise<string>

interface MyRequestsProps {
  readonly client: RequesterClient
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

export function MyRequests({
  client,
  dataProvenance,
  digestText = sha256Text,
  idempotencyKeyFactory = defaultIdempotencyKey,
  requestedRequestRef,
  session,
}: MyRequestsProps) {
  const [requests, setRequests] = useState<readonly RequesterRequestView[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [reloadToken, setReloadToken] = useState(0)
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
    return <p role="alert">Your requests could not be displayed safely.</p>
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
    return (
      <div className="requester-surface">
        <section aria-labelledby="request-title" className="request-summary">
          <p className="eyebrow">Your request</p>
          <h1 id="request-title">{selected.title}</h1>
          <p className="request-summary__outcome">{selected.requested_outcome}</p>
          <p className="request-summary__state">
            Lifecycle state: <strong>{stateLabel(selected)}</strong> · revision{" "}
            {selected.revision} · updated {selected.updated_at}
          </p>
          {selected.denial_explanation === null ||
          selected.denial_explanation === undefined ? null : (
            <p className="request-summary__denial">{selected.denial_explanation}</p>
          )}
          <OwnDecisions request={selected} />
        </section>
        <ClarifiedOutcome
          client={client}
          dataProvenance={dataProvenance}
          idempotencyKeyFactory={idempotencyKeyFactory}
          onDecided={reload}
          requestId={selected.request_id}
          session={session}
        />
        <RequestConversation
          client={client}
          dataProvenance={dataProvenance}
          digestText={digestText}
          idempotencyKeyFactory={idempotencyKeyFactory}
          requestId={selected.request_id}
          session={session}
        />
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
                {request.clarified_outcome === null ||
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
                <a href={`/requests/${request.request_id}`}>Open request</a>
              </li>
            ))}
          </ul>
        )}
      </section>
      <RequestIntake
        client={client}
        digestText={digestText}
        idempotencyKeyFactory={idempotencyKeyFactory}
        onCreated={reload}
        session={session}
      />
    </div>
  )
}
