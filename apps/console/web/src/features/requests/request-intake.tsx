import {useEffect, useState} from "react"

import {ConsoleApiError, ConsoleMutationOutcomeUnknown} from "../../api/client"
import type {MutationRequestContext} from "../../api/client"
import type {
  AccessMode,
  CreateRequestCommand,
  QuestionTermSelectionInput,
  RequestInput,
  RequestKind,
  RequesterRequestView,
  SelectableAnswerTermView,
  SessionView,
} from "../../api/generated"
import {canonicalRequestIntakeContent} from "./request-intake-content"
import type {DigestText, IdempotencyKeyFactory, RequesterClient} from "./my-requests"

const publicIdPattern = /^[a-z][a-z0-9_-]{2,127}$/
const utcMinutePattern = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/

const accessModes: readonly AccessMode[] = ["query", "dashboard", "export"]

/*
 * A question is composed from the governed terms the workspace publishes, never typed.
 *
 * The terms are read from `/api/v1/answer-terms`, which projects the approved metric and
 * dimension references the semantic layer will resolve the question against. Listing terms here
 * would let the form drift from the publication and offer a question the answer validation then
 * refuses as an unknown reference.
 *
 * `not_delivered` is a workspace with no publication behind the builder. The question is then
 * submitted with its text alone, exactly as it was before the builder existed -- the console says
 * so rather than offering terms it does not have.
 */
type AnswerTermsState =
  | {readonly kind: "loading"}
  | {readonly kind: "ready"; readonly terms: readonly SelectableAnswerTermView[]}
  | {readonly kind: "not_delivered"}
  | {readonly kind: "failed"}

function termsOfKind(
  terms: readonly SelectableAnswerTermView[],
  kind: SelectableAnswerTermView["kind"],
): readonly SelectableAnswerTermView[] {
  return terms.filter((term) => term.kind === kind)
}

// The approved revision is shown beside the term, because a term's meaning is part of its
// identity: the same name at another version is another term.
function termLabel(term: SelectableAnswerTermView): string {
  return `${term.term_ref} (approved version ${term.approved_version.version})`
}

interface IntakeAttempt {
  readonly command: CreateRequestCommand
  readonly context: MutationRequestContext
}

export interface StakeholderQuestionDraft {
  readonly kind: "stakeholder_question"
  readonly title: string
  readonly purpose: string
  readonly question: string
}

interface RequestIntakeProps {
  readonly client: RequesterClient
  readonly dataAccessAvailable: boolean
  readonly digestText: DigestText
  readonly idempotencyKeyFactory: IdempotencyKeyFactory
  readonly initialDraft?: StakeholderQuestionDraft
  readonly onCreated: () => void
  readonly session: SessionView
}

interface TermBuilderProps {
  readonly dimensionRefs: readonly string[]
  readonly metricRef: string
  readonly onDimensionToggled: (termRef: string) => void
  readonly onMetricSelected: (termRef: string) => void
  readonly terms: readonly SelectableAnswerTermView[]
}

function TermBuilder({
  dimensionRefs,
  metricRef,
  onDimensionToggled,
  onMetricSelected,
  terms,
}: TermBuilderProps) {
  const metrics = termsOfKind(terms, "metric")
  const dimensions = termsOfKind(terms, "dimension")
  return (
    <div className="request-intake__terms">
      <label>
        <span>Measure</span>
        <select onChange={(event) => onMetricSelected(event.currentTarget.value)} value={metricRef}>
          <option value="">Choose a governed measure</option>
          {metrics.map((term) => (
            <option key={term.term_ref} value={term.term_ref}>
              {termLabel(term)}
            </option>
          ))}
        </select>
      </label>
      <fieldset>
        <legend>Break down by</legend>
        {dimensions.map((term) => (
          <label key={term.term_ref}>
            <input
              checked={dimensionRefs.includes(term.term_ref)}
              name="question-dimension"
              onChange={() => onDimensionToggled(term.term_ref)}
              type="checkbox"
              value={term.term_ref}
            />
            <span>{termLabel(term)}</span>
          </label>
        ))}
      </fieldset>
      <p className="request-intake__guidance">
        These are the approved terms this workspace publishes. Heinzel answers the question these
        terms compose, not the wording above.
      </p>
    </div>
  )
}

function utcInstant(localMinute: string): string {
  return localMinute.length === 16 ? `${localMinute}:00Z` : `${localMinute}Z`
}

function splitFields(value: string): string[] {
  return value
    .split(",")
    .map((field) => field.trim())
    .filter((field) => field.length > 0)
}

export function RequestIntake({
  client,
  dataAccessAvailable,
  digestText,
  idempotencyKeyFactory,
  initialDraft,
  onCreated,
  session,
}: RequestIntakeProps) {
  const [kind, setKind] = useState<RequestKind | null>(initialDraft?.kind ?? null)
  const [title, setTitle] = useState(initialDraft?.title ?? "")
  const [purpose, setPurpose] = useState(initialDraft?.purpose ?? "")
  const [question, setQuestion] = useState(initialDraft?.question ?? "")
  const [dataProductRef, setDataProductRef] = useState("")
  const [requestedFields, setRequestedFields] = useState("")
  const [accessMode, setAccessMode] = useState<AccessMode>("query")
  const [expiresAt, setExpiresAt] = useState("")
  const [metricRef, setMetricRef] = useState("")
  const [dimensionRefs, setDimensionRefs] = useState<readonly string[]>([])
  const [answerTerms, setAnswerTerms] = useState<AnswerTermsState>({kind: "loading"})
  const [fieldError, setFieldError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [created, setCreated] = useState<RequesterRequestView | null>(null)
  const [ambiguousAttempt, setAmbiguousAttempt] = useState<IntakeAttempt | null>(null)

  useEffect(() => {
    let active = true
    void client
      .getSelectableAnswerTerms()
      .then((envelope) => {
        if (active) {
          setAnswerTerms({kind: "ready", terms: envelope.data.terms ?? []})
        }
      })
      .catch((error: unknown) => {
        if (!active) {
          return
        }
        // A workspace with no publication behind the builder says so; anything else is a read
        // that failed, and a failed read must not be displayed as an absent capability.
        setAnswerTerms(
          error instanceof ConsoleApiError && error.code === "capability_not_delivered"
            ? {kind: "not_delivered"}
            : {kind: "failed"},
        )
      })
    return () => {
      active = false
    }
  }, [client])

  function toggleDimension(termRef: string): void {
    setDimensionRefs((current) =>
      current.includes(termRef)
        ? current.filter((reference) => reference !== termRef)
        : [...current, termRef],
    )
  }

  /**
   * The selection to submit, or `null` when the terms cannot be offered at all.
   *
   * `undefined` is a selection the requester still has to make, which `buildRequest` reports as
   * the missing field it is. A workspace whose publication carries no terms yields `null`: the
   * question goes with its text alone, which is what a request carried before the builder.
   */
  function buildSelection(): QuestionTermSelectionInput | null | undefined {
    if (answerTerms.kind !== "ready" || answerTerms.terms.length === 0) {
      return null
    }
    const metrics = termsOfKind(answerTerms.terms, "metric")
    const dimensions = termsOfKind(answerTerms.terms, "dimension")
    if (metrics.length === 0 || dimensions.length === 0) {
      return null
    }
    if (!metrics.some((term) => term.term_ref === metricRef)) {
      setFieldError("Choose the governed measure the question asks for.")
      return undefined
    }
    const chosen = dimensions
      .filter((term) => dimensionRefs.includes(term.term_ref))
      .map((term) => term.term_ref)
    if (chosen.length === 0) {
      setFieldError("Choose at least one governed term to break the measure down by.")
      return undefined
    }
    return {metric_ref: metricRef, dimension_refs: [chosen[0]!, ...chosen.slice(1)]}
  }

  function buildRequest(): RequestInput | null {
    if (title.trim().length === 0) {
      setFieldError("Request title is required.")
      return null
    }
    if (purpose.trim().length === 0) {
      setFieldError("Purpose is required.")
      return null
    }
    if (kind === "stakeholder_question") {
      if (question.trim().length === 0) {
        setFieldError("Question is required.")
        return null
      }
      const selection = buildSelection()
      if (selection === undefined) {
        return null
      }
      return selection === null
        ? {kind, purpose: purpose.trim(), question: question.trim()}
        : {kind, purpose: purpose.trim(), question: question.trim(), selection}
    }
    if (!publicIdPattern.test(dataProductRef.trim())) {
      setFieldError("Data product reference is required and must be a governed reference.")
      return null
    }
    const fields = splitFields(requestedFields)
    if (fields.length === 0) {
      setFieldError("At least one requested field is required.")
      return null
    }
    if (!utcMinutePattern.test(expiresAt)) {
      setFieldError("Access expires at (UTC) is required.")
      return null
    }
    return {
      kind: "data_access",
      purpose: purpose.trim(),
      data_product_ref: dataProductRef.trim(),
      requested_fields: [fields[0]!, ...fields.slice(1)],
      access_mode: accessMode,
      expires_at: utcInstant(expiresAt),
    }
  }

  async function submitAttempt(attempt: IntakeAttempt): Promise<void> {
    setSubmitting(true)
    try {
      const envelope = await client.createRequest(attempt.command, attempt.context)
      setAmbiguousAttempt(null)
      setFieldError(null)
      setCreated(envelope.data)
      onCreated()
    } catch (error: unknown) {
      if (error instanceof ConsoleMutationOutcomeUnknown) {
        setAmbiguousAttempt(attempt)
        setFieldError("The submission outcome is unknown. Reconcile it before retrying.")
      } else if (error instanceof ConsoleApiError) {
        setFieldError(error.message)
      } else {
        setFieldError("The request could not be submitted safely.")
      }
    } finally {
      setSubmitting(false)
    }
  }

  async function submitRequest(): Promise<void> {
    if (kind === null) {
      return
    }
    setCreated(null)
    setAmbiguousAttempt(null)
    const request = buildRequest()
    if (request === null) {
      return
    }
    setFieldError(null)
    // The digest binds the exact submitted content. Requester identity is never sent; the server
    // derives it from trusted context.
    const requestDigest = await digestText(canonicalRequestIntakeContent(title.trim(), request))
    await submitAttempt({
      command: {
        expected_revision: 1,
        request_digest: requestDigest,
        active_role: "requester",
        title: title.trim(),
        request,
      },
      context: {
        csrfToken: session.csrf_token,
        idempotencyKey: idempotencyKeyFactory(),
      },
    })
  }

  return (
    <section aria-labelledby="intake-title" className="request-intake">
      <p className="eyebrow">Submit</p>
      <h2 id="intake-title">Ask Heinzel for something</h2>
      <fieldset className="request-intake__kind">
        <legend>Request type</legend>
        <label>
          <input
            checked={kind === "stakeholder_question"}
            name="request-kind"
            onChange={() => setKind("stakeholder_question")}
            type="radio"
            value="stakeholder_question"
          />
          <span>Stakeholder question</span>
        </label>
        <label>
          <input
            checked={kind === "data_access"}
            disabled={!dataAccessAvailable}
            name="request-kind"
            onChange={() => setKind("data_access")}
            type="radio"
            value="data_access"
          />
          <span>Data access request</span>
        </label>
      </fieldset>
      {dataAccessAvailable ? null : (
        <p className="request-intake__guidance">
          Data access requests are not available in this workspace yet.
        </p>
      )}

      {kind === null ? (
        <p className="request-intake__guidance">
          Choose a request type. Heinzel asks only for the fields that type requires.
        </p>
      ) : (
        <div className="request-intake__fields">
          <label>
            <span>Request title</span>
            <input
              onChange={(event) => setTitle(event.currentTarget.value)}
              type="text"
              value={title}
            />
          </label>
          <label>
            <span>Purpose</span>
            <textarea onChange={(event) => setPurpose(event.currentTarget.value)} value={purpose} />
          </label>
          {kind === "stakeholder_question" ? (
            <>
              <label>
                <span>Question</span>
                <textarea
                  onChange={(event) => setQuestion(event.currentTarget.value)}
                  value={question}
                />
              </label>
              {answerTerms.kind === "loading" ? (
                <p role="status">Loading the governed terms you can ask about…</p>
              ) : null}
              {answerTerms.kind === "failed" ? (
                <p role="alert">The governed terms could not be displayed safely.</p>
              ) : null}
              {answerTerms.kind === "not_delivered" ? (
                <p className="request-intake__guidance">
                  This workspace publishes no governed terms yet, so the question is submitted as
                  text for an architect to clarify.
                </p>
              ) : null}
              {answerTerms.kind === "ready" && answerTerms.terms.length === 0 ? (
                <p className="request-intake__guidance">
                  This workspace's publication carries no approved terms yet, so the question is
                  submitted as text for an architect to clarify.
                </p>
              ) : null}
              {answerTerms.kind === "ready" && answerTerms.terms.length > 0 ? (
                <TermBuilder
                  dimensionRefs={dimensionRefs}
                  metricRef={metricRef}
                  onDimensionToggled={toggleDimension}
                  onMetricSelected={setMetricRef}
                  terms={answerTerms.terms}
                />
              ) : null}
            </>
          ) : (
            <>
              <label>
                <span>Data product reference</span>
                <input
                  onChange={(event) => setDataProductRef(event.currentTarget.value)}
                  type="text"
                  value={dataProductRef}
                />
              </label>
              <label>
                <span>Requested fields</span>
                <input
                  onChange={(event) => setRequestedFields(event.currentTarget.value)}
                  type="text"
                  value={requestedFields}
                />
              </label>
              <label>
                <span>Access mode</span>
                <select
                  onChange={(event) => setAccessMode(event.currentTarget.value as AccessMode)}
                  value={accessMode}
                >
                  {accessModes.map((mode) => (
                    <option key={mode} value={mode}>
                      {mode}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>Access expires at (UTC)</span>
                <input
                  onChange={(event) => setExpiresAt(event.currentTarget.value)}
                  type="datetime-local"
                  value={expiresAt}
                />
              </label>
            </>
          )}
        </div>
      )}

      {fieldError === null ? null : <p role="alert">{fieldError}</p>}
      {ambiguousAttempt === null ? null : (
        <button
          disabled={submitting}
          onClick={() => void submitAttempt(ambiguousAttempt)}
          type="button"
        >
          Reconcile submission
        </button>
      )}
      <button
        className="primary-action"
        disabled={kind === null || submitting}
        onClick={() => void submitRequest()}
        type="button"
      >
        {submitting ? "Submitting…" : "Submit request"}
      </button>
      {created === null ? null : (
        <p role="status">
          {initialDraft === undefined ? "Request submitted." : "Revised request submitted."}{" "}
          <a href={`/requests/${created.request_id}`}>View request</a>.
        </p>
      )}
    </section>
  )
}
