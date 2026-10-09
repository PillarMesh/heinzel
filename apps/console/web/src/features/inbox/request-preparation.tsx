import {useState} from "react"

import {Panel} from "../../components/panel"

import {ConsoleApiError, ConsoleMutationOutcomeUnknown} from "../../api/client"
import type {MutationRequestContext} from "../../api/client"
import type {
  ConsoleEnvelopeRequestDetailView,
  DataProvenance,
  PreparationAction,
  ProposalPreparationCommand,
  RequestClarificationCommand,
  RequestDetailView,
  SessionView,
} from "../../api/generated"

export interface RequestPreparationClient {
  clarifyRequest(requestId: string, command: RequestClarificationCommand, context: MutationRequestContext): Promise<ConsoleEnvelopeRequestDetailView>
  prepareRequestProposal(requestId: string, command: ProposalPreparationCommand, context: MutationRequestContext): Promise<ConsoleEnvelopeRequestDetailView>
  submitRequestProposal(requestId: string, command: ProposalPreparationCommand, context: MutationRequestContext): Promise<ConsoleEnvelopeRequestDetailView>
  getRequestDetail(requestId: string): Promise<ConsoleEnvelopeRequestDetailView>
}

interface Props {
  readonly client: RequestPreparationClient
  readonly dataProvenance: DataProvenance
  readonly detail: RequestDetailView
  readonly idempotencyKeyFactory: () => string
  readonly onAuthoritativeDetail: (detail: RequestDetailView) => void
  readonly session: SessionView
}

const labels: Record<PreparationAction, string> = {
  clarify: "Record clarification",
  prepare_access: "Prepare access proposal",
  prepare_answer: "Prepare answer proposal",
  submit_proposal: "Submit proposal for approval",
}

export function RequestPreparation({client, dataProvenance, detail, idempotencyKeyFactory, onAuthoritativeDetail, session}: Props) {
  const [restatedRequest, setRestatedRequest] = useState("")
  const [inScope, setInScope] = useState("")
  const [outOfScope, setOutOfScope] = useState("")
  const [busy, setBusy] = useState(false)
  const [reloadRequired, setReloadRequired] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  const actions = detail.preparation_actions ?? []
  const notes = detail.preparation_notes ?? []
  if (session.active_role !== "data_architect" || (actions.length === 0 && notes.length === 0)) return null

  function acceptResponse(response: ConsoleEnvelopeRequestDetailView) {
    if (response.meta.data_provenance !== dataProvenance || response.data.request_id !== detail.request_id || response.data.revision < detail.revision) {
      throw new Error("The response does not match the displayed request.")
    }
    onAuthoritativeDetail(response.data)
  }

  async function reload() {
    setBusy(true)
    try {
      acceptResponse(await client.getRequestDetail(detail.request_id))
      setReloadRequired(false)
      setFailure(null)
    } catch {
      setFailure("The request could not be refreshed safely. Reload it before continuing.")
    } finally {
      setBusy(false)
    }
  }

  async function submit(action: PreparationAction) {
    if (busy || reloadRequired) return
    setBusy(true)
    setFailure(null)
    const command = {expected_revision: detail.revision, active_role: session.active_role}
    const context = {csrfToken: session.csrf_token, idempotencyKey: idempotencyKeyFactory()}
    try {
      const response = action === "clarify"
        ? await client.clarifyRequest(detail.request_id, {...command, restated_request: restatedRequest.trim(), in_scope_summary: inScope.trim(), out_of_scope_summary: outOfScope.trim()}, context)
        : action === "prepare_answer" || action === "prepare_access"
          ? await client.prepareRequestProposal(detail.request_id, command, context)
          : await client.submitRequestProposal(detail.request_id, command, context)
      acceptResponse(response)
    } catch (error: unknown) {
      setReloadRequired(true)
      setFailure(error instanceof ConsoleMutationOutcomeUnknown
        ? "The outcome is unknown. Reload the request to see whether the action was recorded."
        : error instanceof ConsoleApiError
          ? `${error.message} Reload the request before continuing.`
          : "The action could not be reconciled safely. Reload the request before continuing.")
    } finally {
      setBusy(false)
    }
  }

  const clarificationInvalid = [restatedRequest, inScope, outOfScope].some(value => !value.trim() || value.length > 4000)
  const heading = actions.includes("clarify")
    ? "Clarify this request"
    : actions.includes("prepare_answer")
      ? "Prepare an answer"
      : actions.includes("prepare_access")
        ? "Prepare an access scope"
        : "Submit for approval"
  return (
    <Panel
      ariaLabel="Request preparation"
      className="proposal-review"
      description={
        actions.includes("clarify")
          ? "Record the intended outcome and its boundaries for the requester to review."
          : actions.includes("prepare_answer")
            ? "Composed from the governed semantic scope and policy. Review the result before submitting it."
            : actions.includes("prepare_access")
              ? "The least-privilege scope from the governed product and current policy. Review it before submitting."
              : "Submit the displayed proposal so its required authorities can review it."
      }
      title={heading}
    >
      {notes.map(note => <p className="panel__nothing" key={note} role="status">{note}</p>)}
      {!actions.includes("clarify") ? null : (
        <div className="field-grid">
          <label className="decision-detail__comment"><span>Clarified request</span><textarea maxLength={4000} placeholder="What this request will answer, in one sentence." rows={2} value={restatedRequest} onChange={event => setRestatedRequest(event.currentTarget.value)} disabled={busy || reloadRequired} /></label>
          <label className="decision-detail__comment"><span>In scope</span><textarea maxLength={4000} placeholder="What the answer will cover." rows={2} value={inScope} onChange={event => setInScope(event.currentTarget.value)} disabled={busy || reloadRequired} /></label>
          <label className="decision-detail__comment"><span>Out of scope</span><textarea maxLength={4000} placeholder="What it deliberately will not cover." rows={2} value={outOfScope} onChange={event => setOutOfScope(event.currentTarget.value)} disabled={busy || reloadRequired} /></label>
        </div>
      )}
      {actions.length === 0 ? null : (
        <div className="decision-record__actions">
          {actions.map(action => <button key={action} className="primary-action" type="button" disabled={busy || reloadRequired || (action === "clarify" && clarificationInvalid)} onClick={() => void submit(action)}>{busy ? "Working…" : labels[action]}</button>)}
        </div>
      )}
      {failure === null ? null : <p role="alert" className="inbox-unavailable">{failure}</p>}
      {reloadRequired ? <button type="button" disabled={busy} onClick={() => void reload()}>Reload request</button> : null}
    </Panel>
  )
}
