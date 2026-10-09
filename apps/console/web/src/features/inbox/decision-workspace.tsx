import {RequestPreparation, type RequestPreparationClient} from "./request-preparation"
import {ArtifactDigest, ProposalApprovals} from "./artifact-reference"
import {useCallback, useEffect, useRef, useState} from "react"
import {useParams} from "react-router-dom"

import {ConsoleApiError, ConsoleMutationOutcomeUnknown} from "../../api/client"
import type {MutationRequestContext} from "../../api/client"
import type {
  AccessRevocationCommand,
  AdmissionCommand,
  ActorRole,
  ConsoleEnvelopeInboxView,
  ConsoleEnvelopeAccessLifecycleView,
  ConsoleEnvelopeProductIntentApprovalView,
  ConsoleEnvelopeRequestDetailView,
  DataProvenance,
  Decision1,
  DecisionCommand,
  InboxView,
  ProductIntentApprovalCommand,
  ProductIntentReviewView,
  RequestDetailView,
  SessionView,
} from "../../api/generated"
import {AccessPreviewReview} from "./access-preview-review"
import {Nothing, Panel} from "../../components/panel"
import {Tabs} from "../../components/tabs"
import {CatalogEvidence, type CatalogEvidenceClient} from "./catalog-evidence"
import {ConversationPanel, type ConversationPanelClient} from "./conversation-panel"
import {DashboardPreview, type DashboardPreviewClient} from "./dashboard-preview"
import {
  DashboardPublication,
  type DashboardPublicationClient,
} from "./dashboard-publication"
import {ImpactPanel, type ImpactClient} from "../impact/impact-panel"
import {DecisionQueue} from "./decision-queue"
import {EvidencePane} from "./evidence-pane"
import {ProvenancePane, type ProvenanceClient} from "./provenance-pane"
import {StakeholderAnswerReview} from "./stakeholder-answer-review"
import "./inbox.css"

/** A lifecycle state as a person reads it, rather than as the identifier it travels under. */
function stateLabel(state: string): string {
  const words = state.replaceAll("_", " ")
  return `${words.charAt(0).toUpperCase()}${words.slice(1)}`
}

const decisionLabels = {
  approve: "Approve",
  reject: "Reject",
  request_changes: "Request changes",
} satisfies Record<Decision1, string>

/**
 * Which of the three is the one to press.
 *
 * They were drawn identically, so the surface gave no help with the only question it exists
 * to ask. Approving carries the work forward and is the filled one; rejecting ends it and is
 * outlined in the danger colour rather than filled, because the loudest thing on a review
 * page should be the action that continues it.
 */
const decisionRank = {
  approve: "action--primary",
  reject: "action--danger",
  request_changes: "",
} satisfies Record<Decision1, string>

export interface InboxClient
  extends RequestPreparationClient,
    CatalogEvidenceClient,
    ConversationPanelClient,
    DashboardPreviewClient,
    DashboardPublicationClient,
    ImpactClient,
    ProvenanceClient {
  approveProductIntent(
    requestId: string,
    command: ProductIntentApprovalCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeProductIntentApprovalView>
  admitRequest(
    requestId: string,
    command: AdmissionCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeRequestDetailView>
  decideRequest(
    requestId: string,
    command: DecisionCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeRequestDetailView>
  getInbox(): Promise<ConsoleEnvelopeInboxView>
  getRequestDetail(requestId: string): Promise<ConsoleEnvelopeRequestDetailView>
  revokeAccess(
    requestId: string,
    command: AccessRevocationCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeAccessLifecycleView>
}

interface ProductIntentReviewProps {
  readonly client: InboxClient
  readonly idempotencyKeyFactory: IdempotencyKeyFactory
  readonly onApproved: (reviewedDigest: string) => void
  readonly requestId: string
  readonly requestRevision: number
  readonly review: ProductIntentReviewView
  readonly session: SessionView
}

function ProductIntentReview({
  client,
  idempotencyKeyFactory,
  onApproved,
  requestId,
  requestRevision,
  review,
  session,
}: ProductIntentReviewProps) {
  const [approved, setApproved] = useState(review.approved)
  const [approvedRevision, setApprovedRevision] = useState(review.approved_intent_revision)
  const [submitting, setSubmitting] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  const sourceCoverage = review.source_coverage
  const unresolvedConstraints = review.unresolved_constraints ?? []
  const activeRole = session.active_role === "data_architect" ? session.active_role : null
  const blocked =
    activeRole === null ||
    unresolvedConstraints.length > 0 ||
    sourceCoverage.some((source) => !source.authorized)

  async function approve() {
    if (activeRole === null) {
      return
    }
    setSubmitting(true)
    setFailure(null)
    try {
      const response = await client.approveProductIntent(
        requestId,
        {
          active_role: activeRole,
          expected_revision: requestRevision,
          reviewed_digest: review.reviewed_digest,
        },
        {
          csrfToken: session.csrf_token,
          idempotencyKey: idempotencyKeyFactory(),
        },
      )
      setApproved(true)
      setApprovedRevision(response.data.intent_revision)
      onApproved(review.reviewed_digest)
    } catch {
      setFailure("The typed product intent could not be approved. Reload and review it again.")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section aria-label="Typed product intent" className="product-intent-review">
      <div className="product-intent-review__heading">
        <div>
          <h3>{review.title}</h3>
          <p>{review.business_outcome}</p>
        </div>
        <span className={approved ? "product-intent-review__approved" : undefined}>
          {approved ? `Approved revision ${approvedRevision}` : "Awaiting approval"}
        </span>
      </div>

      <div className="product-intent-review__section">
        <h4>Source coverage</h4>
        <ul className="product-intent-review__sources">
          {sourceCoverage.map((source) => (
            <li key={source.source_ref}>
              <strong>{source.source_ref}</strong>
              <span>{(source.covered_fields ?? []).join(", ") || "No fields covered"}</span>
              <span className={source.authorized ? "source-authorized" : "source-unresolved"}>
                {source.authorized ? "Authorized" : "Authorization required"}
              </span>
            </li>
          ))}
        </ul>
      </div>

      <dl className="product-intent-review__facts">
        <div><dt>Grain</dt><dd>{review.grain.join(", ")}</dd></div>
        <div>
          <dt>Measures</dt>
          <dd>{review.measures.map((measure) => `${measure.metric_ref} (${measure.aggregation})`).join(", ")}</dd>
        </div>
        <div><dt>Dimensions</dt><dd>{(review.dimensions ?? []).join(", ") || "None"}</dd></div>
        <div>
          <dt>Filters</dt>
          <dd>{(review.filters ?? []).map((filter) => `${filter.dimension_ref} ${filter.operator.replaceAll("_", " ")} ${filter.value}`).join(", ") || "None"}</dd>
        </div>
        <div><dt>Freshness</dt><dd>{review.freshness_seconds.toLocaleString()} seconds</dd></div>
        <div><dt>Outputs</dt><dd>{review.outputs.join(", ")}</dd></div>
      </dl>

      {unresolvedConstraints.length === 0 ? null : (
        <div className="product-intent-review__unresolved">
          <h4>Unresolved constraints</h4>
          <ul>{unresolvedConstraints.map((constraint) => <li key={constraint}>{constraint}</li>)}</ul>
        </div>
      )}
      {failure === null ? null : <p role="alert">{failure}</p>}
      <button
        className="primary-action"
        disabled={approved || blocked || submitting}
        onClick={() => void approve()}
        type="button"
      >
        {approved ? "Typed intent approved" : submitting ? "Approving typed intent…" : "Approve typed intent"}
      </button>
    </section>
  )
}

export type IdempotencyKeyFactory = () => string

function defaultIdempotencyKey(): string {
  return `decision-${globalThis.crypto.randomUUID()}`
}

function mediaMatches(query: string): boolean {
  return typeof globalThis.matchMedia === "function" && globalThis.matchMedia(query).matches
}

/**
 * How much room the workspace has: both columns, the queue folded away, or one at a time.
 *
 * It was named for the evidence rail, which is gone -- the rail repeated the proposal beside
 * it in the narrowest column on the page, and what was its own is a section now.
 */
export type WorkspaceLayout = "wide" | "medium" | "narrow"

function currentLayout(): WorkspaceLayout {
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

interface AccessRevocationPanelProps {
  readonly client: InboxClient
  readonly dataProvenance: DataProvenance
  readonly detail: RequestDetailView
  readonly idempotencyKeyFactory: IdempotencyKeyFactory
  readonly onAuthoritativeDetail: (detail: RequestDetailView) => void
  readonly session: SessionView
}

function AccessRevocationPanel({
  client,
  dataProvenance,
  detail,
  idempotencyKeyFactory,
  onAuthoritativeDetail,
  session,
}: AccessRevocationPanelProps) {
  const lifecycle = detail.access_lifecycle
  const [confirming, setConfirming] = useState(false)
  const [reason, setReason] = useState("")
  const [submitting, setSubmitting] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  if (lifecycle === null || lifecycle === undefined) {
    return null
  }
  const expectedRevision = lifecycle.revision

  async function refresh(): Promise<void> {
    const response = await client.getRequestDetail(detail.request_id)
    if (
      response.meta.data_provenance !== dataProvenance ||
      response.data.request_id !== detail.request_id
    ) {
      throw new Error("request detail scope changed")
    }
    onAuthoritativeDetail(response.data)
  }

  async function revoke(): Promise<void> {
    const explanatoryReason = reason.trim()
    if (explanatoryReason.length === 0 || session.active_role !== "data_architect") {
      setFailure("Explain why access should be removed.")
      return
    }
    setSubmitting(true)
    setFailure(null)
    try {
      const response = await client.revokeAccess(
        detail.request_id,
        {
          active_role: "data_architect",
          expected_revision: expectedRevision,
          reason: explanatoryReason,
        },
        {csrfToken: session.csrf_token, idempotencyKey: idempotencyKeyFactory()},
      )
      if (response.meta.data_provenance !== dataProvenance) {
        throw new Error("access lifecycle provenance changed")
      }
      await refresh()
    } catch {
      setFailure("Access removal could not be confirmed. Reload the request before trying again.")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section aria-label="Access status" className="access-lifecycle">
      <p className="eyebrow">Access status</p>
      <h3>{lifecycle.title}</h3>
      <p>{lifecycle.summary}</p>
      {lifecycle.can_revoke && session.active_role === "data_architect" ? (
        confirming ? (
          <div className="access-lifecycle__revocation">
            <label htmlFor="architect-access-revocation-reason">Reason for removing access</label>
            <textarea
              disabled={submitting}
              id="architect-access-revocation-reason"
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

interface RequestDetailPanelProps {
  readonly client: InboxClient
  readonly dataProvenance: DataProvenance
  readonly detail: RequestDetailView
  readonly idempotencyKeyFactory: IdempotencyKeyFactory
  readonly onAuthoritativeDetail: (detail: RequestDetailView) => void
  /** The dashboard this request would open up, when it is a dashboard it names rather than one
   * the catalog already describes. `null` for every other kind of request. */
  readonly dashboardCandidateRef: string | null
  readonly onDashboardAvailability: (available: boolean) => void
  readonly requiredEvidenceUnavailable: boolean
  readonly session: SessionView
}

function RequestDetailPanel({
  client,
  dashboardCandidateRef,
  dataProvenance,
  detail,
  idempotencyKeyFactory,
  onAuthoritativeDetail,
  onDashboardAvailability,
  requiredEvidenceUnavailable,
  session,
}: RequestDetailPanelProps) {
  const [comment, setComment] = useState("")
  const [digestConfirmed, setDigestConfirmed] = useState(false)
  const [submitting, setSubmitting] = useState<Decision1 | null>(null)
  const [admitting, setAdmitting] = useState(false)
  const [admissionReconciliation, setAdmissionReconciliation] = useState<{
    readonly idempotencyKey: string
  } | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [refreshNotice, setRefreshNotice] = useState<string | null>(null)
  const [reconciliation, setReconciliation] = useState<{
    readonly decision: Decision1
    readonly idempotencyKey: string
  } | null>(null)
  const [locallyApprovedIntentDigest, setLocallyApprovedIntentDigest] = useState<string | null>(null)
  const approvedIntentDigest =
    detail.product_intent?.approved === true
      ? detail.product_intent.reviewed_digest
      : locallyApprovedIntentDigest

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

  async function admitProposal(reusedKey?: string): Promise<void> {
    if (proposalDigest === null || proposalDigest === undefined) {
      return
    }
    const idempotencyKey = reusedKey ?? idempotencyKeyFactory()
    setAdmitting(true)
    setFailure(null)
    setRefreshNotice(null)
    try {
      const response = await client.admitRequest(
        detail.request_id,
        {
          active_role: session.active_role,
          expected_revision: detail.revision,
          reviewed_digest: proposalDigest,
        },
        {csrfToken: session.csrf_token, idempotencyKey},
      )
      setAdmissionReconciliation(null)
      onAuthoritativeDetail(response.data)
    } catch (error: unknown) {
      if (error instanceof ConsoleMutationOutcomeUnknown) {
        // The admission may well have been recorded. Saying the proposal is
        // unchanged would assert something the console does not know, so it offers
        // the same command identity again instead.
        setAdmissionReconciliation({idempotencyKey})
        return
      }
      setFailure(
        error instanceof ConsoleApiError
          ? error.message
          : "The admission could not be recorded safely.",
      )
    } finally {
      setAdmitting(false)
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
  const productIntentApprovalBlocked =
    detail.product_intent !== null &&
    detail.product_intent !== undefined &&
    approvedIntentDigest !== detail.product_intent.reviewed_digest
  /**
   * What the product is waiting for, in the reviewer's words.
   *
   * Ordered so the first thing they can do something about is the thing they are told. A
   * submission in flight is not a blocker worth naming -- the button already says so.
   */
  const blockingReason =
    requiredEvidenceUnavailable || submitting !== null
      ? null
      : !digestConfirmed
        ? "Confirm the reviewed digest to record a decision."
        : productIntentApprovalBlocked
          ? "Approving also needs the product intent approved against the digest under review."
          : null

  const conversationCount = (detail.conversation?.messages ?? []).length
  const preparationCount = (detail.preparation_actions ?? []).length
  const admissionOffered = detail.admission?.available === true
  const decisionsWaiting = availableActions.length > 0 || admissionOffered
  /*
    The tab the architect lands on is the one with something to do on it. Opened on the first
    tab every time, the product made them hunt for the control that was the whole reason the
    request is in front of them.
  */
  const openOn = preparationCount > 0
    ? "request"
    : decisionsWaiting
      ? "decision"
      : detail.proposal === null || detail.proposal === undefined
        ? "request"
        : "proposal"

  const requestTab = () => (
    <div className="panel-stack">
      {detail.question === null || detail.question === undefined ? null : (
        <Panel title="Original question">
          <p>{detail.question}</p>
        </Panel>
      )}
      <RequestPreparation
        key={`${detail.request_id}:${detail.revision}`}
        client={client}
        dataProvenance={dataProvenance}
        detail={detail}
        idempotencyKeyFactory={idempotencyKeyFactory}
        onAuthoritativeDetail={onAuthoritativeDetail}
        session={session}
      />
      <ImpactPanel
        client={client}
        dataProvenance={dataProvenance}
        key={detail.request_id}
        requestId={detail.request_id}
      />
      <AccessRevocationPanel
        client={client}
        dataProvenance={dataProvenance}
        detail={detail}
        idempotencyKeyFactory={idempotencyKeyFactory}
        onAuthoritativeDetail={onAuthoritativeDetail}
        session={session}
      />
    </div>
  )

  const proposalTab = () => (
    <div className="panel-stack">
      {detail.proposal === null || detail.proposal === undefined ? (
        <Panel title="Proposal">
          <Nothing>
            No proposal has been issued for this request. One is prepared from the Request tab.
          </Nothing>
        </Panel>
      ) : detail.proposal.kind === "stakeholder_answer" ? (
        <StakeholderAnswerReview proposal={detail.proposal} />
      ) : detail.proposal.kind === "access_preview" ? (
        <AccessPreviewReview proposal={detail.proposal} />
      ) : (
        <Panel ariaLabel="Disclosure denial proposal" title="Proposed disclosure denial">
          <p>{detail.proposal.explanation}</p>
          <dl className="record">
            <dt>Reason</dt>
            <dd>{detail.proposal.reason_code}</dd>
          </dl>
          <ProposalApprovals approvals={detail.proposal.required_approvals ?? []} />
        </Panel>
      )}
      {detail.product_intent === null || detail.product_intent === undefined ? null : (
        <ProductIntentReview
          client={client}
          idempotencyKeyFactory={idempotencyKeyFactory}
          key={`${detail.request_id}:${detail.revision}:${detail.product_intent.reviewed_digest}`}
          onApproved={setLocallyApprovedIntentDigest}
          requestId={detail.request_id}
          requestRevision={detail.revision}
          review={detail.product_intent}
          session={session}
        />
      )}
    </div>
  )

  const conversationTab = () => (
    /* The client is what makes a reply possible at all; without it the panel refuses every
       message. The digest comes from the conversation itself. */
    <ConversationPanel
      client={client}
      conversation={detail.conversation}
      idempotencyKeyFactory={idempotencyKeyFactory}
      session={session}
    />
  )

  // Read on demand. Seven receipts joined across six stores is the console's most expensive
  // read, and a reviewer who only needs to approve should never pay for it.
  const lineageTab = () => (
    <ProvenancePane
      client={client}
      dataProvenance={dataProvenance}
      key={detail.request_id}
      requestId={detail.request_id}
    />
  )

  // What was cited and what has happened, at the page's width instead of in a rail beside it.
  const evidenceTab = () => (
    <EvidencePane
      evidence={detail.evidence}
      lifecycle={detail.lifecycle ?? []}
      proposal={detail.proposal ?? null}
    >
      {(detail.evidence.datasets ?? [])
        .filter((dataset) => !dataset.artifact_reference)
        .map((dataset) => (
          <CatalogEvidence
            assetRef={dataset.dataset_ref}
            client={client}
            dataProvenance={dataProvenance}
            key={dataset.dataset_ref}
          />
        ))}
      {dashboardCandidateRef === null ? null : (
        <DashboardPreview
          client={client}
          dashboardRef={dashboardCandidateRef}
          dataProvenance={dataProvenance}
          onAvailabilityChange={onDashboardAvailability}
        />
      )}
    </EvidencePane>
  )

  const decisionTab = () => (
    <div className="panel-stack">
      {availableActions.length === 0 ? (
        <Panel title="Record a decision">
          <Nothing>
            This request is not waiting on a decision from you in its current state.
          </Nothing>
        </Panel>
      ) : (
        <Panel
          description="A decision is recorded against the exact proposal digest shown here."
          title="Record a decision"
        >
          <label className="decision-record__digest field--inline">
            <input
              checked={digestConfirmed}
              onChange={(event) => setDigestConfirmed(event.currentTarget.checked)}
              type="checkbox"
            />
            <span>
              I confirm the exact reviewed digest
              {proposalDigest === null || proposalDigest === undefined ? null : (
                // Sixty-four characters wrapped across two lines beside a checkbox, which
                // nobody read and nobody could have compared. Its ends, with the whole
                // value a disclosure away.
                <ArtifactDigest digest={proposalDigest} />
              )}
            </span>
          </label>
          <label className="decision-detail__comment">
            <span>Review comment</span>
            <textarea onChange={(event) => setComment(event.currentTarget.value)} value={comment} />
          </label>
          {!requiredEvidenceUnavailable ? null : (
            <p className="inbox-unavailable">
              Required evidence is unavailable, so no decision can be recorded.
            </p>
          )}
          {refreshNotice === null ? null : <p className="inbox-notice">{refreshNotice}</p>}
          <div className="decision-record__actions">
            {availableActions.map((action) => (
              <button
                className={decisionRank[action]}
                disabled={actionsDisabled || (action === "approve" && productIntentApprovalBlocked)}
                key={action}
                onClick={() => void recordDecision(action)}
                type="button"
              >
                {submitting === action ? "Submitting decision…" : decisionLabels[action]}
              </button>
            ))}
            {/*
              Why, beside the controls it is about. A row of disabled buttons with the
              reason somewhere else is a reader guessing at what the product wants.
            */}
            {blockingReason === null ? null : (
              <p className="decision-record__blocked" role="status">
                {blockingReason}
              </p>
            )}
          </div>
        </Panel>
      )}

      {detail.admission === null || detail.admission === undefined ? null : (
        <Panel title="Admission to execution">
          {detail.admission.available && detail.admission.pending_delivery === true ? (
            <>
              <p>
                This answer was admitted, but its delivery has not completed. Retry delivery once
                the cause is resolved; the admission is not recorded a second time.
              </p>
              <div className="decision-record__actions">
                <button
                  className="primary-action"
                  disabled={admitting || proposalDigest === null || proposalDigest === undefined}
                  onClick={() => void admitProposal()}
                  type="button"
                >
                  {admitting ? "Retrying delivery…" : "Retry delivery"}
                </button>
              </div>
            </>
          ) : detail.admission.available ? (
            <>
              <p>
                Every required approval is recorded against this proposal. Admission is the
                separate transaction that carries it into execution.
              </p>
              <div className="decision-record__actions">
                <button
                  className="primary-action"
                  disabled={admitting || proposalDigest === null || proposalDigest === undefined}
                  onClick={() => void admitProposal()}
                  type="button"
                >
                  {admitting ? "Admitting…" : "Admit to execution"}
                </button>
              </div>
            </>
          ) : (
            <Nothing>
              {detail.admission.blocking_reason ?? "This proposal cannot be admitted yet."}
            </Nothing>
          )}
          {admissionReconciliation === null ? null : (
            <div className="decision-detail__reconciliation">
              <p role="alert">
                Outcome unknown; reconciling. The same command identity is reused; no second
                admission is created.
              </p>
              <button
                disabled={admitting}
                onClick={() => void admitProposal(admissionReconciliation.idempotencyKey)}
                type="button"
              >
                Reconcile the submitted admission
              </button>
            </div>
          )}
        </Panel>
      )}

      {/*
        Publishing sits with the actions rather than in the evidence rail. It is the last thing
        the architect does to a delivered answer, and it was the one control on the page filed
        under evidence -- in a hundred-and-sixty-pixel column that broke its identifiers across
        three lines each.
      */}
      <Panel
        description="A certified dashboard may be published from a delivered answer."
        title="Publish a dashboard"
      >
        <DashboardPublication
          client={client}
          key={`${detail.request_id}:${detail.revision}`}
          mutationContext={() => ({
            csrfToken: session.csrf_token,
            idempotencyKey: idempotencyKeyFactory(),
          })}
          requestId={detail.request_id}
        />
      </Panel>

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

  return (
    <div className="decision-detail__body">
      {/*
        A page header, then one job at a time. These five jobs -- read the question, read the
        impact, read the proposal, hold a conversation, decide -- used to be a single column
        two and a half screens tall, with nothing grouped and nothing deferred.
      */}
      <header className="work-header">
        <p className="eyebrow">{detail.kind.replaceAll("_", " ")}</p>
        <h1>{detail.title}</h1>
        <dl className="record work-header__facts">
          <dt>Lifecycle state</dt>
          <dd>{stateLabel(detail.state)}</dd>
          <dt>Requester purpose</dt>
          <dd>{detail.purpose}</dd>
        </dl>
      </header>
      {blockingAuthority === undefined ? null : (
        <p className="decision-detail__blocked" role="status">
          <strong>Blocked on the requester.</strong> {blockingAuthority.reason} Until{" "}
          {roleLabel(blockingAuthority.role)} acceptance is recorded, no architect decision can
          be admitted.
        </p>
      )}
      <Tabs
        ariaLabel="Request sections"
        initial={openOn}
        tabs={[
          {content: requestTab, id: "request", label: "Request", badge: preparationCount > 0 ? preparationCount : undefined},
          {content: proposalTab, id: "proposal", label: "Proposal"},
          {content: conversationTab, id: "conversation", label: "Conversation", badge: conversationCount > 0 ? conversationCount : undefined},
          {content: lineageTab, id: "lineage", label: "Lineage"},
          {content: evidenceTab, id: "evidence", label: "Evidence"},
          {content: decisionTab, id: "decision", label: "Decision", badge: decisionsWaiting ? "•" : undefined},
        ]}
      />
    </div>
  )
}

interface DecisionWorkspaceProps {
  readonly client: InboxClient
  readonly dataProvenance: DataProvenance
  readonly idempotencyKeyFactory?: IdempotencyKeyFactory
  readonly layout?: WorkspaceLayout
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
  const [measuredLayout, setMeasuredLayout] = useState<WorkspaceLayout>(currentLayout)
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
  const [inboxRefresh, setInboxRefresh] = useState(0)
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
  }, [client, dataProvenance, requestedRequestId, inboxRefresh])

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
    setInboxRefresh(value => value + 1)
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
    currentDetail.proposal.access_mode === "dashboard" &&
    !currentDetail.proposal.data_product_reference
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
          isPageHeading={!showDetail}
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
            <div className="inbox-empty">
              <h1>Inbox</h1>
              <p>Select a request from the decision queue to review its proposal.</p>
            </div>
          ) : detailFailed ? (
            <p role="alert">The request detail could not be displayed safely.</p>
          ) : currentDetail === null ? (
            <p role="status">Loading the request detail…</p>
          ) : (
            <RequestDetailPanel
              client={client}
              dashboardCandidateRef={dashboardRef}
              dataProvenance={dataProvenance}
              detail={currentDetail}
              idempotencyKeyFactory={idempotencyKeyFactory}
              key={currentDetail.request_id}
              onAuthoritativeDetail={onAuthoritativeDetail}
              onDashboardAvailability={onDashboardAvailability}
              requiredEvidenceUnavailable={requiredEvidenceUnavailable}
              session={session}
            />
          )}
        </section>
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
