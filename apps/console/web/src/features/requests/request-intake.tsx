import {useState} from "react"

import {ConsoleApiError, ConsoleMutationOutcomeUnknown} from "../../api/client"
import type {MutationRequestContext} from "../../api/client"
import type {
  AccessMode,
  CreateRequestCommand,
  RequestInput,
  RequestKind,
  RequesterRequestView,
  SessionView,
} from "../../api/generated"
import type {DigestText, IdempotencyKeyFactory, RequesterClient} from "./my-requests"

const publicIdPattern = /^[a-z][a-z0-9_-]{2,127}$/
const utcMinutePattern = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/

const accessModes: readonly AccessMode[] = ["query", "dashboard", "export"]

interface IntakeAttempt {
  readonly command: CreateRequestCommand
  readonly context: MutationRequestContext
}

interface RequestIntakeProps {
  readonly client: RequesterClient
  readonly digestText: DigestText
  readonly idempotencyKeyFactory: IdempotencyKeyFactory
  readonly onCreated: () => void
  readonly session: SessionView
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
  digestText,
  idempotencyKeyFactory,
  onCreated,
  session,
}: RequestIntakeProps) {
  const [kind, setKind] = useState<RequestKind | null>(null)
  const [title, setTitle] = useState("")
  const [purpose, setPurpose] = useState("")
  const [question, setQuestion] = useState("")
  const [dataProductRef, setDataProductRef] = useState("")
  const [requestedFields, setRequestedFields] = useState("")
  const [accessMode, setAccessMode] = useState<AccessMode>("query")
  const [expiresAt, setExpiresAt] = useState("")
  const [fieldError, setFieldError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [created, setCreated] = useState<RequesterRequestView | null>(null)
  const [ambiguousAttempt, setAmbiguousAttempt] = useState<IntakeAttempt | null>(null)

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
      return {kind, purpose: purpose.trim(), question: question.trim()}
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
    const requestDigest = await digestText(JSON.stringify({title: title.trim(), request}))
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
      <h2 id="intake-title">Ask PillarMesh for something</h2>
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
            name="request-kind"
            onChange={() => setKind("data_access")}
            type="radio"
            value="data_access"
          />
          <span>Data access request</span>
        </label>
      </fieldset>

      {kind === null ? (
        <p className="request-intake__guidance">
          Choose a request type. PillarMesh asks only for the fields that type requires.
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
            <label>
              <span>Question</span>
              <textarea
                onChange={(event) => setQuestion(event.currentTarget.value)}
                value={question}
              />
            </label>
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
          Request {created.request_id} recorded at revision {created.revision} in state{" "}
          {created.state}.
        </p>
      )}
    </section>
  )
}
