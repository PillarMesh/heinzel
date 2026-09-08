import {render, screen, waitFor} from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import {expect, test, vi} from "vitest"

import {validateConsoleResponse} from "../../api/schema"
import type {RequestDetailView, SessionView} from "../../api/generated"
import {RequestPreparation} from "./request-preparation"

const session: SessionView = {
  actor: {display_name: "Architect"}, roles: ["data_architect"], active_role: "data_architect",
  tenant: {ref: "tenant-a", display_name: "Tenant"},
  workspace: {ref: "workspace-a", display_name: "Workspace"},
  csrf_token: "csrf-token-with-at-least-thirty-two-characters",
}
const detail: RequestDetailView = {
  request_id: "request-new", kind: "stakeholder_question", state: "submitted",
  title: "Net revenue definition", purpose: "semantic definition", revision: 1,
  preparation_actions: ["clarify"],
  conversation: {request_id: "request-new", revision: 1, conversation_digest: "a".repeat(64), messages: []},
  evidence: {freshness: "unknown", quality_summary: "No proposal yet", lineage_summary: "No proposal yet", authorization_summary: "No proposal yet"},
}
function envelope(value: RequestDetailView) {
  return validateConsoleResponse("request_detail_response", {
    meta: {correlation_id: "correlation-test", data_provenance: "governed_local"}, data: value,
  })
}
function setup(value = detail) {
  const client = {
    clarifyRequest: vi.fn(async () => envelope({...detail, state: "investigating", revision: 2, preparation_actions: ["prepare_answer"]})),
    prepareRequestProposal: vi.fn(async () => envelope({...detail, state: "proposed", revision: 3, preparation_actions: ["submit_proposal"]})),
    submitRequestProposal: vi.fn(async () => envelope({...detail, state: "awaiting_approval", revision: 4, preparation_actions: []})),
    getRequestDetail: vi.fn(async () => envelope(value)),
  }
  const onAuthoritativeDetail = vi.fn()
  render(<RequestPreparation client={client} detail={envelope(value).data} session={session} dataProvenance="governed_local" idempotencyKeyFactory={() => "prepare-test-key"} onAuthoritativeDetail={onAuthoritativeDetail} />)
  return {client, onAuthoritativeDetail}
}

test("records the architect clarification with the displayed revision and scoped text", async () => {
  const user = userEvent.setup()
  const {client, onAuthoritativeDetail} = setup()
  expect(screen.getByRole("button", {name: "Record clarification"})).toBeDisabled()
  await user.type(screen.getByLabelText("Clarified request"), "Explain net revenue")
  await user.type(screen.getByLabelText("In scope"), "Governed definition")
  await user.type(screen.getByLabelText("Out of scope"), "Raw rows")
  await user.click(screen.getByRole("button", {name: "Record clarification"}))
  await waitFor(() => expect(onAuthoritativeDetail).toHaveBeenCalledWith(expect.objectContaining({revision: 2})))
  expect(client.clarifyRequest).toHaveBeenCalledWith("request-new", {
    expected_revision: 1, active_role: "data_architect", restated_request: "Explain net revenue",
    in_scope_summary: "Governed definition", out_of_scope_summary: "Raw rows",
  }, {csrfToken: session.csrf_token, idempotencyKey: "prepare-test-key"})
})

test.each([
  ["prepare_answer", "Prepare answer proposal", "prepareRequestProposal", "investigating"],
  ["submit_proposal", "Submit proposal for approval", "submitRequestProposal", "proposed"],
] as const)("offers only the server-issued %s action", async (action, label, method, state) => {
  const {client} = setup({...detail, state, preparation_actions: [action]})
  expect(screen.queryByLabelText("Clarified request")).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole("button", {name: label}))
  expect(client[method]).toHaveBeenCalledWith("request-new", {expected_revision: 1, active_role: "data_architect"}, expect.objectContaining({csrfToken: session.csrf_token}))
})

test("shows no preparation controls when the server offers no action", () => {
  setup({...detail, preparation_actions: []})
  expect(screen.queryByRole("button")).not.toBeInTheDocument()
})

test("requires a reload after a lost preparation response", async () => {
  const {client, onAuthoritativeDetail} = setup({...detail, state: "investigating", preparation_actions: ["prepare_answer"]})
  client.prepareRequestProposal.mockRejectedValueOnce(new Error("connection lost"))
  await userEvent.click(screen.getByRole("button", {name: "Prepare answer proposal"}))
  expect(await screen.findByRole("alert")).toHaveTextContent("Reload the request")
  expect(onAuthoritativeDetail).not.toHaveBeenCalled()
  expect(screen.getByRole("button", {name: "Prepare answer proposal"})).toBeDisabled()
  await userEvent.click(screen.getByRole("button", {name: "Reload request"}))
  await waitFor(() => expect(client.getRequestDetail).toHaveBeenCalledWith("request-new"))
  expect(client.prepareRequestProposal).toHaveBeenCalledTimes(1)
})

test("shows the owning dependency without offering another preparation", () => {
  setup({...detail, preparation_actions: [], preparation_notes: ["Blocked on data_product_change: missing_data."]})
  expect(screen.getByRole("status")).toHaveTextContent("Blocked on data_product_change")
  expect(screen.queryByRole("button")).not.toBeInTheDocument()
})

test("refuses a command response for another request", async () => {
  const {client, onAuthoritativeDetail} = setup({...detail, state: "investigating", preparation_actions: ["prepare_answer"]})
  client.prepareRequestProposal.mockResolvedValueOnce(envelope({...detail, request_id: "request-other"}))
  await userEvent.click(screen.getByRole("button", {name: "Prepare answer proposal"}))
  expect(await screen.findByRole("alert")).toHaveTextContent("could not be reconciled")
  expect(onAuthoritativeDetail).not.toHaveBeenCalled()
})
