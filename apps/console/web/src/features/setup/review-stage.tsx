import {useEffect, useState} from "react"

import {ConsoleApiError} from "../../api/client"
import type {
  ActorRole,
  DataProvenance,
  Decision,
  ReviewKind,
  ReviewView,
  SessionView,
} from "../../api/generated"
import type {IdempotencyKeyFactory, SetupClient} from "./setup-workbench"

interface ReviewStageProps {
  readonly client: SetupClient
  readonly dataProvenance: DataProvenance
  readonly idempotencyKeyFactory: IdempotencyKeyFactory
  readonly review: ReviewView
  readonly session: SessionView
}

interface ReviewProjectionProps {
  readonly client: SetupClient
  readonly dataProvenance: DataProvenance
  readonly idempotencyKeyFactory: IdempotencyKeyFactory
  readonly kind: ReviewKind
  readonly reviewRefs: readonly string[]
  readonly session: SessionView
}

export function ReviewProjection({
  client,
  dataProvenance,
  idempotencyKeyFactory,
  kind,
  reviewRefs,
  session,
}: ReviewProjectionProps) {
  const requestKey = `${kind}:${reviewRefs.join(":")}`
  const [result, setResult] = useState<{
    readonly failed: boolean
    readonly key: string
    readonly review: ReviewView | null
  }>({failed: false, key: requestKey, review: null})

  useEffect(() => {
    let active = true
    void Promise.all(reviewRefs.map((reviewRef) => client.getReview(reviewRef)))
      .then((envelopes) => {
        if (!active) {
          return
        }
        const matchingEnvelope = envelopes.find(
          (envelope) =>
            envelope.meta.data_provenance === dataProvenance && envelope.data.kind === kind,
        )
        if (matchingEnvelope === undefined) {
          setResult({failed: true, key: requestKey, review: null})
          return
        }
        setResult({failed: false, key: requestKey, review: matchingEnvelope.data})
      })
      .catch(() => {
        if (active) {
          setResult({failed: true, key: requestKey, review: null})
        }
      })
    return () => {
      active = false
    }
  }, [client, dataProvenance, kind, requestKey, reviewRefs])

  if (result.key === requestKey && result.failed) {
    return <p role="alert">The review projection could not be displayed safely.</p>
  }
  if (result.key !== requestKey || result.review === null) {
    return <p role="status">Loading review projection…</p>
  }
  return (
    <ReviewStage
      client={client}
      dataProvenance={dataProvenance}
      idempotencyKeyFactory={idempotencyKeyFactory}
      review={result.review}
      session={session}
    />
  )
}

function roleLabel(role: ActorRole): string {
  return role
    .split("_")
    .map((part, index) =>
      index === 0 ? `${part.charAt(0).toUpperCase()}${part.slice(1)}` : part,
    )
    .join(" ")
}

function gateLabel(review: ReviewView): string {
  if (review.kind === "meaning") {
    return "Stage 5 · Meaning review"
  }
  if (review.kind === "data_product") {
    return "Stage 6 · Data-product review"
  }
  return "Stage 7 · Activation"
}

function uniqueRoles(roles: readonly ActorRole[]): ActorRole[] {
  return [...new Set(roles)]
}

export function ReviewStage({
  client,
  dataProvenance,
  idempotencyKeyFactory,
  review,
  session,
}: ReviewStageProps) {
  const [currentReview, setCurrentReview] = useState(review)
  const [digestConfirmed, setDigestConfirmed] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [decisionError, setDecisionError] = useState<string | null>(null)
  const [comment, setComment] = useState("")
  const [refreshNotice, setRefreshNotice] = useState<string | null>(null)
  const requirements = currentReview.required_authorities ?? []
  const pendingRequirements = requirements.filter((authority) => !authority.satisfied)
  const holdsEveryRequiredRole = requirements.every((authority) =>
    session.roles.includes(authority.role),
  )
  const decisionRoles = uniqueRoles(
    (holdsEveryRequiredRole
      ? pendingRequirements
      : pendingRequirements.filter((authority) => authority.role === session.active_role)
    ).map((authority) => authority.role),
  )

  async function recordDecision(decision: Decision): Promise<void> {
    if (!digestConfirmed || decisionRoles.length === 0 || !currentReview.can_decide) {
      return
    }
    setSubmitting(true)
    setDecisionError(null)
    setRefreshNotice(null)
    let authoritativeReview = currentReview
    try {
      for (const role of decisionRoles) {
        const response = await client.decideReview(
          authoritativeReview.review_id,
          {
            expected_revision: authoritativeReview.revision,
            reviewed_digest: authoritativeReview.reviewed_digest,
            active_role: role,
            decision,
          },
          {
            csrfToken: session.csrf_token,
            idempotencyKey: idempotencyKeyFactory(),
          },
        )
        if (
          response.meta.data_provenance !== dataProvenance ||
          response.data.review_id !== authoritativeReview.review_id ||
          response.data.reviewed_digest !== authoritativeReview.reviewed_digest
        ) {
          throw new Error("review projection mismatch")
        }
        authoritativeReview = response.data
        setCurrentReview(response.data)
      }
    } catch (error: unknown) {
      if (
        error instanceof ConsoleApiError &&
        error.status === 409 &&
        error.recoveryAction === "reload"
      ) {
        try {
          const response = await client.getReview(authoritativeReview.review_id)
          if (
            response.meta.data_provenance !== dataProvenance ||
            response.data.review_id !== authoritativeReview.review_id
          ) {
            setDecisionError("The refreshed review projection could not be reconciled safely.")
            return
          }
          setCurrentReview(response.data)
          setDigestConfirmed(false)
          setRefreshNotice("Review refreshed; your unsubmitted comment was preserved.")
        } catch {
          setDecisionError("The refreshed review projection could not be reconciled safely.")
        }
      } else {
        setDecisionError("The review decision could not be reconciled safely.")
      }
    } finally {
      setSubmitting(false)
    }
  }

  const actionLabel =
    holdsEveryRequiredRole && decisionRoles.length > 1
      ? "Approve required roles"
      : `Approve as ${roleLabel(session.active_role)}`

  return (
    <section aria-labelledby="review-title" className="setup-stage review-stage">
      <p className="eyebrow">{gateLabel(currentReview)}</p>
      <h1 id="review-title">{currentReview.title}</h1>
      <p className="setup-stage__lead">{currentReview.summary}</p>

      <div className="review-sections">
        {currentReview.sections.map((section) => (
          <article className="review-section" key={section.section_id}>
            <h2>{section.title}</h2>
            {section.summary === null || section.summary === undefined ? null : (
              <p>{section.summary}</p>
            )}
            <dl>
              {(section.items ?? []).map((item) => (
                <div key={`${section.section_id}:${item.label}`}>
                  <dt>{item.label}</dt>
                  <dd>{item.value}</dd>
                  {item.material_change === true ? (
                    <dd className="review-item__warning">
                      Changing {item.label} invalidates downstream approvals.
                    </dd>
                  ) : null}
                </div>
              ))}
            </dl>
          </article>
        ))}
      </div>

      <section aria-labelledby="authorities-title" className="review-ledger">
        <h2 id="authorities-title">Required authorities</h2>
        <ul aria-label="Required authorities">
          {(currentReview.required_authorities ?? []).map((authority) => (
            <li key={`${authority.role}:${authority.subject_digest}`}>
              <strong>{roleLabel(authority.role)}</strong>
              <span>{authority.reason}</span>
              <code>{authority.subject_digest}</code>
              <span>{authority.satisfied ? "Recorded" : "Not recorded"}</span>
            </li>
          ))}
        </ul>
      </section>

      {(currentReview.decisions ?? []).length === 0 ? null : (
        <section aria-labelledby="decisions-title" className="review-ledger">
          <h2 id="decisions-title">Recorded decisions</h2>
          <ul>
            {(currentReview.decisions ?? []).map((decision) => (
              <li key={`${decision.role}:${decision.decided_at}`}>
                {roleLabel(decision.role)} · {decision.decision} · {decision.decided_at}
              </li>
            ))}
          </ul>
        </section>
      )}

      {(currentReview.constraints ?? []).length === 0 ? null : (
        <section aria-labelledby="constraints-title" className="review-constraints">
          <h2 id="constraints-title">No Valid Plan</h2>
          <ul>
            {(currentReview.constraints ?? []).map((constraint) => (
              <li key={constraint.code}>
                <code>{constraint.code}</code>
                <strong>{constraint.summary}</strong>
                <span>Responsible role: {roleLabel(constraint.responsible_role)}</span>
                <span>Permitted next action: {constraint.permitted_next_action}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {(currentReview.evidence_refs ?? []).length === 0 ? null : (
        <section aria-labelledby="evidence-title" className="review-ledger">
          <h2 id="evidence-title">Evidence references</h2>
          <ul>
            {(currentReview.evidence_refs ?? []).map((reference) => (
              <li key={reference}>{reference}</li>
            ))}
          </ul>
        </section>
      )}

      <label className="review-comment">
        <span>Unsubmitted review comment</span>
        <textarea
          onChange={(event) => setComment(event.currentTarget.value)}
          value={comment}
        />
      </label>
      {refreshNotice === null ? null : <p className="setup-notice">{refreshNotice}</p>}

      <label className="digest-confirmation">
        <input
          checked={digestConfirmed}
          onChange={(event) => setDigestConfirmed(event.currentTarget.checked)}
          type="checkbox"
        />
        <span>I confirm the exact reviewed digest {currentReview.reviewed_digest}.</span>
      </label>
      <div className="review-actions">
        <button
          className="primary-action"
          disabled={
            !digestConfirmed ||
            submitting ||
            decisionRoles.length === 0 ||
            !currentReview.can_decide
          }
          onClick={() => void recordDecision("approve")}
          type="button"
        >
          {submitting ? "Recording decision…" : actionLabel}
        </button>
        <button
          disabled={
            !digestConfirmed ||
            submitting ||
            decisionRoles.length === 0 ||
            !currentReview.can_decide
          }
          onClick={() => void recordDecision("request_changes")}
          type="button"
        >
          Request changes
        </button>
      </div>
      {decisionError === null ? null : <p role="alert">{decisionError}</p>}
    </section>
  )
}
