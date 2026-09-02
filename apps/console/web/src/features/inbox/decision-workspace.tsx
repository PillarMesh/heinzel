import {useCallback, useEffect, useRef, useState} from "react"
import {useParams} from "react-router-dom"

import {ConsoleApiError, ConsoleMutationOutcomeUnknown} from "../../api/client"
import type {MutationRequestContext} from "../../api/client"
import type {
  ActorRole,
  ConsoleEnvelopeInboxView,
  ConsoleEnvelopeRequestDetailView,
  DataProvenance,
  Decision1,
  DecisionCommand,
  InboxView,
  RequestDetailView,
  SessionView,
} from "../../api/generated"
import {AccessPreviewReview} from "./access-preview-review"
import {CatalogEvidence, type CatalogEvidenceClient} from "./catalog-evidence"
import {ConversationPanel, type ConversationPanelClient} from "./conversation-panel"
import {DashboardPreview, type DashboardPreviewClient} from "./dashboard-preview"
import {DecisionQueue} from "./decision-queue"
import {EvidenceDrawer, type EvidenceLayout} from "./evidence-drawer"
import {LifecycleTimeline} from "./lifecycle-timeline"
import {StakeholderAnswerReview} from "./stakeholder-answer-review"
import "./inbox.css"

const decisionLabels = {
  approve: "Approve",
  reject: "Reject",
  request_changes: "Request changes",
} satisfies Record<Decision1, string>

export interface InboxClient
  extends CatalogEvidenceClient,
    ConversationPanelClient,
    DashboardPreviewClient {
  decideRequest(
    requestId: string,
    command: DecisionCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeRequestDetailView>
  getInbox(): Promise<ConsoleEnvelopeInboxView>
  getRequestDetail(requestId: string): Promise<ConsoleEnvelopeRequestDetailView>
}

export type IdempotencyKeyFactory = () => string

function defaultIdempotencyKey(): string {
  return `decision-${globalThis.crypto.randomUUID()}`
}

function mediaMatches(query: string): boolean {
  return typeof globalThis.matchMedia === "function" && globalThis.matchMedia(query).matches
}

function currentLayout(): EvidenceLayout {
  if (mediaMatches("(max-width: 759px)")) {
    return "narrow"
  }
  return mediaMatches("(max-width: 1119px)") ? "medium" : "wide"
}

function roleLabel(role: ActorRole): string {
  const words = role.replaceAll("_", " ")
  return `${words.charAt(0).toUpperCase()}${words.slice(1)}`
}

// The contract carries no explicit requester-acceptance field, so the outstanding requester
// authority on the proposal is the only evidence that the clarified outcome is unaccepted.
function outstandingRequesterAuthority(detail: RequestDetailView) {
  return (detail.proposal?.required_authorities ?? []).find(
    (authority) => authority.role === "requester" && !authority.satisfied,
  )
}

interface RequestDetailPanelProps {
  readonly client: InboxClient
  readonly dataProvenance: DataProvenance
  readonly detail: RequestDetailView
  readonly idempotencyKeyFactory: IdempotencyKeyFactory
  readonly onAuthoritativeDetail: (detail: RequestDetailView) => void
  readonly requiredEvidenceUnavailable: boolean
  readonly session: SessionView
}

function RequestDetailPanel({
  client,
  dataProvenance,
  detail,
  idempotencyKeyFactory,
  onAuthoritativeDetail,
  requiredEvidenceUnavailable,
  session,
}: RequestDetailPanelProps) {
  const [comment, setComment] = useState("")
  const [digestConfirmed, setDigestConfirmed] = useState(false)
  const [submitting, setSubmitting] = useState<Decision1 | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [refreshNotice, setRefreshNotice] = useState<string | null>(null)
  const [reconciliation, setReconciliation] = useState<{
    readonly decision: Decision1
    readonly idempotencyKey: string
  } | null>(null)

  const blockingAuthority = outstandingRequesterAuthority(detail)
  const proposalDigest = detail.proposal_digest
  const availableActions =
    blockingAuthority !== undefined || proposalDigest === null || proposalDigest === undefined
      ? []
      : (detail.available_actions ?? [])

  async function refreshAuthoritativeDetail(): Promise<void> {
    try {
      const response = await client.getRequestDetail(detail.request_id)
      if (
        response.meta.data_provenance !== dataProvenance ||
        response.data.request_id !== detail.request_id
      ) {
        setFailure("The refreshed request detail could not be reconciled safely.")
        return
      }
      onAuthoritativeDetail(response.data)
      setDigestConfirmed(false)
      setRefreshNotice(
        `Review refreshed at revision ${response.data.revision}; your comment was preserved.`,
      )
    } catch {
      setFailure("The refreshed request detail could not be reconciled safely.")
    }
  }

  async function recordDecision(decision: Decision1, reusedKey?: string): Promise<void> {
    if (proposalDigest === null || proposalDigest === undefined) {
      return
    }
    const idempotencyKey = reusedKey ?? idempotencyKeyFactory()
    setSubmitting(decision)
    setFailure(null)
    setRefreshNotice(null)
    try {
      const response = await client.decideRequest(
        detail.request_id,
        {
          active_role: session.active_role,
          decision,
          expected_revision: detail.revision,
          reviewed_digest: proposalDigest,
        },
        {csrfToken: session.csrf_token, idempotencyKey},
      )
      if (
        response.meta.data_provenance !== dataProvenance ||
        response.data.request_id !== detail.request_id
      ) {
        setFailure("The recorded decision could not be displayed safely.")
        return
      }
      setReconciliation(null)
      setDigestConfirmed(false)
      onAuthoritativeDetail(response.data)
    } catch (error: unknown) {
      if (error instanceof ConsoleMutationOutcomeUnknown) {
        setReconciliation({decision, idempotencyKey})
        return
      }
      if (
        error instanceof ConsoleApiError &&
        error.status === 409 &&
        error.recoveryAction === "reload"
      ) {
        await refreshAuthoritativeDetail()
        return
      }
      setFailure("The decision could not be recorded. The prior authoritative state is unchanged.")
    } finally {
      setSubmitting(null)
    }
  }

  const actionsDisabled =
    !digestConfirmed || submitting !== null || requiredEvidenceUnavailable || availableActions.length === 0

  return (
    <div className="decision-detail__body">
      <p className="eyebrow">{detail.kind.replaceAll("_", " ")}</p>
      <h2>{detail.title}</h2>
      <dl className="decision-detail__facts">
        <div>
          <dt>Lifecycle state</dt>
          <dd>{detail.state.replaceAll("_", " ")}</dd>
        </div>
        <div>
          <dt>Requester purpose</dt>
          <dd>{detail.purpose}</dd>
        </div>
        <div>
          <dt>Server revision</dt>
          <dd>{detail.revision}</dd>
        </div>
      </dl>

      {blockingAuthority === undefined ? null : (
        <p className="decision-detail__blocked" role="status">
          Blocked on the requester. {blockingAuthority.reason} Until{" "}
          {roleLabel(blockingAuthority.role)} acceptance is recorded, no architect decision can be
          admitted.
        </p>
      )}

      {detail.proposal === null || detail.proposal === undefined ? (
        <p className="inbox-empty">No proposal has been issued for this request.</p>
      ) : detail.proposal.kind === "stakeholder_answer" ? (
        <StakeholderAnswerReview proposal={detail.proposal} />
      ) : (
        <AccessPreviewReview proposal={detail.proposal} />
      )}

      <ConversationPanel conversation={detail.conversation} session={session} />

      <label className="decision-detail__comment">
        <span>Review comment</span>
        <textarea onChange={(event) => setComment(event.currentTarget.value)} value={comment} />
      </label>
      {refreshNotice === null ? null : <p className="inbox-notice">{refreshNotice}</p>}

      {availableActions.length === 0 ? (
        <p className="inbox-empty">No decision is admissible from this projection.</p>
      ) : (
        <>
          <label className="decision-detail__digest">
            <input
              checked={digestConfirmed}
              onChange={(event) => setDigestConfirmed(event.currentTarget.checked)}
              type="checkbox"
            />
            <span>I confirm the exact reviewed digest {proposalDigest}.</span>
          </label>
          {!requiredEvidenceUnavailable ? null : (
            <p className="inbox-unavailable">
              Required evidence is unavailable, so no decision can be recorded.
            </p>
          )}
          <div className="decision-detail__actions">
            {availableActions.map((action) => (
              <button
                className={action === "approve" ? "primary-action" : undefined}
                disabled={actionsDisabled}
                key={action}
                onClick={() => void recordDecision(action)}
                type="button"
              >
                {submitting === action ? "Submitting decision…" : decisionLabels[action]}
              </button>
            ))}
          </div>
        </>
      )}

      {reconciliation === null ? null : (
        <div className="decision-detail__reconciliation">
          <p role="alert">
            Outcome unknown; reconciling. The same command identity is reused; no new decision is
            created.
          </p>
          <button
            disabled={submitting !== null}
            onClick={() =>
              void recordDecision(reconciliation.decision, reconciliation.idempotencyKey)
            }
            type="button"
          >
            Reconcile the submitted decision
          </button>
        </div>
      )}
      {failure === null ? null : <p role="alert">{failure}</p>}
    </div>
  )
}

interface DecisionWorkspaceProps {
  readonly client: InboxClient
  readonly dataProvenance: DataProvenance
  readonly idempotencyKeyFactory?: IdempotencyKeyFactory
  readonly layout?: EvidenceLayout
  readonly requestedRequestId?: string | undefined
  readonly session: SessionView
}

export function DecisionWorkspace({
  client,
  dataProvenance,
  idempotencyKeyFactory = defaultIdempotencyKey,
  layout,
  requestedRequestId,
  session,
}: DecisionWorkspaceProps) {
  const [measuredLayout, setMeasuredLayout] = useState<EvidenceLayout>(currentLayout)
  const [inbox, setInbox] = useState<{readonly failed: boolean; readonly view: InboxView | null}>({
    failed: false,
    view: null,
  })
  const [selectedRequestId, setSelectedRequestId] = useState<string | null>(
    requestedRequestId ?? null,
  )
  const [detail, setDetail] = useState<{
    readonly failed: boolean
    readonly requestId: string | null
    readonly value: RequestDetailView | null
  }>({failed: false, requestId: null, value: null})
  const [detailFocusToken, setDetailFocusToken] = useState(0)
  const [queueFocus, setQueueFocus] = useState<{
    readonly requestId: string | null
    readonly token: number
  }>({requestId: null, token: 0})
  const [dashboardAvailability, setDashboardAvailability] = useState<{
    readonly available: boolean
    readonly requestId: string
  } | null>(null)
  const detailRef = useRef<HTMLElement>(null)

  const effectiveLayout = layout ?? measuredLayout

  useEffect(() => {
    if (layout !== undefined || typeof globalThis.matchMedia !== "function") {
      return undefined
    }
    const medium = globalThis.matchMedia("(max-width: 1119px)")
    const narrow = globalThis.matchMedia("(max-width: 759px)")
    const update = () => setMeasuredLayout(currentLayout())
    medium.addEventListener("change", update)
    narrow.addEventListener("change", update)
    return () => {
      medium.removeEventListener("change", update)
      narrow.removeEventListener("change", update)
    }
  }, [layout])

  useEffect(() => {
    let active = true
    void client
      .getInbox()
      .then((envelope) => {
        if (!active) {
          return
        }
        if (envelope.meta.data_provenance !== dataProvenance) {
          setInbox({failed: true, view: null})
          return
        }
        setInbox({failed: false, view: envelope.data})
        const serverSelection = envelope.data.selected_request_id
        if (
          requestedRequestId === undefined &&
          serverSelection !== null &&
          serverSelection !== undefined
        ) {
          setSelectedRequestId((current) => current ?? serverSelection)
        }
      })
      .catch(() => {
        if (active) {
          setInbox({failed: true, view: null})
        }
      })
    return () => {
      active = false
    }
  }, [client, dataProvenance, requestedRequestId])

  useEffect(() => {
    if (selectedRequestId === null) {
      return undefined
    }
    let active = true
    void client
      .getRequestDetail(selectedRequestId)
      .then((envelope) => {
        if (!active) {
          return
        }
        if (
          envelope.meta.data_provenance !== dataProvenance ||
          envelope.data.request_id !== selectedRequestId
        ) {
          setDetail({failed: true, requestId: selectedRequestId, value: null})
          return
        }
        setDetail({failed: false, requestId: selectedRequestId, value: envelope.data})
      })
      .catch(() => {
        if (active) {
          setDetail({failed: true, requestId: selectedRequestId, value: null})
        }
      })
    return () => {
      active = false
    }
  }, [client, dataProvenance, selectedRequestId])

  useEffect(() => {
    if (detailFocusToken === 0 || detail.value === null) {
      return
    }
    detailRef.current?.focus()
  }, [detail.value, detailFocusToken])

  const onAuthoritativeDetail = useCallback((next: RequestDetailView) => {
    setDetail({failed: false, requestId: next.request_id, value: next})
  }, [])

  const onDashboardAvailability = useCallback(
    (available: boolean) => {
      if (selectedRequestId !== null) {
        setDashboardAvailability({available, requestId: selectedRequestId})
      }
    },
    [selectedRequestId],
  )

  const items = inbox.view?.items ?? []
  const showQueue = effectiveLayout !== "narrow" || selectedRequestId === null
  const showDetail = effectiveLayout !== "narrow" || selectedRequestId !== null
  // A projection is only current for the selection that requested it, so a slower response for a
  // previous selection can never be shown against the request now on screen.
  const detailIsCurrent = detail.requestId === selectedRequestId
  const detailFailed = detailIsCurrent && detail.failed
  const currentDetail = detailIsCurrent && !detail.failed ? detail.value : null
  const dashboardRef =
    currentDetail?.proposal?.kind === "access_preview" &&
    currentDetail.proposal.access_mode === "dashboard"
      ? currentDetail.proposal.data_product_ref
      : null
  const requiredEvidenceUnavailable =
    dashboardRef !== null &&
    dashboardAvailability !== null &&
    dashboardAvailability.requestId === selectedRequestId &&
    !dashboardAvailability.available

  return (
    <div className={`decision-workspace decision-workspace--${effectiveLayout}`}>
      {!showQueue ? null : inbox.failed ? (
        <section aria-label="Decision queue" className="decision-queue">
          <h2>Decision queue</h2>
          <p role="alert">The decision queue could not be displayed safely.</p>
        </section>
      ) : inbox.view === null ? (
        <section aria-label="Decision queue" className="decision-queue">
          <h2>Decision queue</h2>
          <p role="status">Loading the decision queue…</p>
        </section>
      ) : (
        <DecisionQueue
          focusRequestId={queueFocus.requestId}
          focusToken={queueFocus.token}
          items={items}
          onActivate={() => setDetailFocusToken((token) => token + 1)}
          onSelect={(requestId) => setSelectedRequestId(requestId)}
          selectedRequestId={selectedRequestId}
        />
      )}

      {!showDetail ? null : (
        <section
          aria-label="Request detail"
          className="decision-detail"
          ref={detailRef}
          tabIndex={-1}
        >
          {effectiveLayout !== "narrow" ? null : (
            <button
              onClick={() => {
                const previous = selectedRequestId
                setSelectedRequestId(null)
                setQueueFocus((focus) => ({requestId: previous, token: focus.token + 1}))
              }}
              type="button"
            >
              Back to the decision queue
            </button>
          )}
          {selectedRequestId === null ? (
            <p className="inbox-empty">Select a request to review its proposal.</p>
          ) : detailFailed ? (
            <p role="alert">The request detail could not be displayed safely.</p>
          ) : currentDetail === null ? (
            <p role="status">Loading the request detail…</p>
          ) : (
            <RequestDetailPanel
              client={client}
              dataProvenance={dataProvenance}
              detail={currentDetail}
              idempotencyKeyFactory={idempotencyKeyFactory}
              key={currentDetail.request_id}
              onAuthoritativeDetail={onAuthoritativeDetail}
              requiredEvidenceUnavailable={requiredEvidenceUnavailable}
              session={session}
            />
          )}
        </section>
      )}

      {currentDetail === null ? null : (
        <EvidenceDrawer evidence={currentDetail.evidence} layout={effectiveLayout}>
          <h3>Catalog records</h3>
          {(currentDetail.evidence.datasets ?? []).map((dataset) => (
            <CatalogEvidence
              assetRef={dataset.dataset_ref}
              client={client}
              dataProvenance={dataProvenance}
              key={dataset.dataset_ref}
            />
          ))}
          {dashboardRef === null ? null : (
            <>
              <h3>Dashboard candidate</h3>
              <DashboardPreview
                client={client}
                dashboardRef={dashboardRef}
                dataProvenance={dataProvenance}
                onAvailabilityChange={onDashboardAvailability}
              />
            </>
          )}
          <h3>Lifecycle</h3>
          <LifecycleTimeline events={currentDetail.lifecycle ?? []} />
        </EvidenceDrawer>
      )}
    </div>
  )
}

interface DecisionWorkspaceRouteProps {
  readonly client: InboxClient
  readonly dataProvenance: DataProvenance
  readonly session: SessionView
}

export default function DecisionWorkspaceRoute({
  client,
  dataProvenance,
  session,
}: DecisionWorkspaceRouteProps) {
  const {requestId} = useParams()
  return (
    <DecisionWorkspace
      client={client}
      dataProvenance={dataProvenance}
      requestedRequestId={requestId}
      session={session}
    />
  )
}
