import {render, screen, waitFor, within} from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import {beforeEach, expect, test, vi} from "vitest"

import {ConsoleApiError} from "../../api/client"
import type {
  ClarifiedOutcomeView,
  ConsoleEnvelopeClarifiedOutcomeView,
  ConsoleEnvelopeConversationView,
  ConsoleEnvelopeJsonTuplePillarmeshConsoleContractsRequesterRequestView,
  ConsoleEnvelopeRequesterRequestView,
  ConversationView,
  RequesterRequestView,
  SessionView,
} from "../../api/generated"
import {MyRequests} from "./my-requests"

const conversationDigest = "8".repeat(64)
const outcomeDigest = "b".repeat(64)
const requestDigest = "c".repeat(64)

const session: SessionView = {
  actor: {display_name: "Riley Requester"},
  roles: ["requester"],
  active_role: "requester",
  tenant: {ref: "tenant-demo", display_name: "Northwind Demo"},
  workspace: {ref: "workspace-revenue", display_name: "Revenue to cash"},
  csrf_token: "csrf-token-with-at-least-thirty-two-characters",
}

const clarifiedOutcome: ClarifiedOutcomeView = {
  request_id: "request-blocked-acceptance",
  revision: 2,
  statement_digest: outcomeDigest,
  restated_request: "Explain the weekly net revenue movement.",
  purpose: "Support a synthetic stakeholder review.",
  in_scope_summary: "Synthetic aggregate revenue only.",
  out_of_scope_summary: "Customer and payment details.",
  accepted: false,
}

const ownRequest: RequesterRequestView = {
  request_id: "request-blocked-acceptance",
  kind: "stakeholder_question",
  state: "clarifying",
  title: "Weekly net revenue movement",
  requested_outcome: "Explain the weekly net revenue movement.",
  revision: 2,
  updated_at: "2026-09-01T09:00:00Z",
  own_decisions: [
    {
      decision: "request_changes",
      subject_label: "Clarified outcome statement",
      created_at: "2026-09-01T08:00:00Z",
    },
  ],
  clarified_outcome: clarifiedOutcome,
  denial_explanation: null,
}

const requestsEnvelope: ConsoleEnvelopeJsonTuplePillarmeshConsoleContractsRequesterRequestView = {
  meta: {correlation_id: "correlation-requests", data_provenance: "demo_fixture"},
  data: [ownRequest],
}

const conversation: ConversationView = {
  request_id: "request-blocked-acceptance",
  revision: 2,
  conversation_digest: "c".repeat(64),
  awaiting_role: "requester",
  messages: [
    {
      message_id: "message-question",
      author_label: "PillarMesh",
      author_role: "pillarmesh",
      body: "Does <b>weekly</b> mean the ISO week ending Sunday?",
      created_at: "2026-09-01T08:10:00Z",
    },
    {
      message_id: "message-reply",
      author_label: "Riley Requester",
      author_role: "requester",
      body: "Please use the governed revenue definition.",
      created_at: "2026-09-01T08:20:00Z",
    },
    {
      message_id: "message-intervention",
      author_label: "Dana Architect",
      author_role: "data_architect",
      body: "I am taking over this business-meaning question.",
      created_at: "2026-09-01T08:30:00Z",
    },
  ],
}

const conversationEnvelope: ConsoleEnvelopeConversationView = {
  meta: {correlation_id: "correlation-conversation", data_provenance: "demo_fixture"},
  data: conversation,
}

const outcomeEnvelope: ConsoleEnvelopeClarifiedOutcomeView = {
  meta: {correlation_id: "correlation-outcome", data_provenance: "demo_fixture"},
  data: clarifiedOutcome,
}

const createdEnvelope: ConsoleEnvelopeRequesterRequestView = {
  meta: {correlation_id: "correlation-created", data_provenance: "demo_fixture"},
  data: {
    request_id: "request-0001",
    kind: "stakeholder_question",
    state: "submitted",
    title: "Weekly net revenue movement",
    requested_outcome: "Prepare the weekly revenue review.",
    revision: 1,
    updated_at: "2026-09-01T09:05:00Z",
  },
}

const client = {
  acceptClarifiedOutcome: vi.fn(),
  appendConversationMessage: vi.fn(),
  createRequest: vi.fn(),
  getClarifiedOutcome: vi.fn(),
  getConversation: vi.fn(),
  getRequesterRequests: vi.fn(),
  // Reviewer projections this surface must never reach for.
  getEvidence: vi.fn(),
  getInbox: vi.fn(),
  getRequestDetail: vi.fn(),
}

const digestText = vi.fn(async (value: string) =>
  value.includes("conversation") ? conversationDigest : requestDigest,
)

function renderSurface(requestedRequestRef?: string) {
  return render(
    <MyRequests
      client={client}
      dataProvenance="demo_fixture"
      digestText={digestText}
      idempotencyKeyFactory={() => "idempotency-requester-fixed"}
      requestedRequestRef={requestedRequestRef}
      session={session}
    />,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  client.getRequesterRequests.mockResolvedValue(requestsEnvelope)
  client.getConversation.mockResolvedValue(conversationEnvelope)
  client.getClarifiedOutcome.mockResolvedValue(outcomeEnvelope)
})

test("requires an explicit request type before any typed payload field is offered", async () => {
  renderSurface()

  expect(await screen.findByRole("radio", {name: "Stakeholder question"})).not.toBeChecked()
  expect(screen.getByRole("radio", {name: "Data access request"})).not.toBeChecked()
  expect(screen.queryByRole("textbox", {name: "Question"})).not.toBeInTheDocument()
  expect(screen.queryByRole("textbox", {name: "Data product reference"})).not.toBeInTheDocument()
  expect(screen.getByRole("button", {name: "Submit request"})).toBeDisabled()
})

test("names the exact missing field, keeps the screen, and preserves the other input", async () => {
  const user = userEvent.setup()
  renderSurface()

  await user.click(await screen.findByRole("radio", {name: "Stakeholder question"}))
  await user.type(screen.getByRole("textbox", {name: "Request title"}), "Weekly revenue")
  await user.type(
    screen.getByRole("textbox", {name: "Question"}),
    "Why did net revenue move last week?",
  )
  await user.click(screen.getByRole("button", {name: "Submit request"}))

  expect(await screen.findByRole("alert")).toHaveTextContent("Purpose is required.")
  expect(screen.getByRole("textbox", {name: "Request title"})).toHaveValue("Weekly revenue")
  expect(screen.getByRole("textbox", {name: "Question"})).toHaveValue(
    "Why did net revenue move last week?",
  )
  expect(client.createRequest).not.toHaveBeenCalled()
})

test("submits a stakeholder question without any browser-supplied requester identity", async () => {
  const user = userEvent.setup()
  client.createRequest.mockResolvedValue(createdEnvelope)
  renderSurface()

  await user.click(await screen.findByRole("radio", {name: "Stakeholder question"}))
  await user.type(
    screen.getByRole("textbox", {name: "Request title"}),
    "Weekly net revenue movement",
  )
  await user.type(
    screen.getByRole("textbox", {name: "Purpose"}),
    "Prepare the weekly revenue review.",
  )
  await user.type(
    screen.getByRole("textbox", {name: "Question"}),
    "Why did net revenue move last week?",
  )
  await user.click(screen.getByRole("button", {name: "Submit request"}))

  await waitFor(() => expect(client.createRequest).toHaveBeenCalledTimes(1))
  const [command, context] = client.createRequest.mock.calls[0]!
  expect(command).toEqual({
    expected_revision: 1,
    request_digest: requestDigest,
    active_role: "requester",
    title: "Weekly net revenue movement",
    request: {
      kind: "stakeholder_question",
      purpose: "Prepare the weekly revenue review.",
      question: "Why did net revenue move last week?",
    },
  })
  expect(digestText).toHaveBeenCalledWith(
    '{"payload":{"purpose":"Prepare the weekly revenue review.","question":"Why did net revenue move last week?","request_type":"stakeholder_question"},"title":"Weekly net revenue movement"}',
  )
  expect(Object.keys(command)).not.toContain("actor_id")
  expect(Object.keys(command)).not.toContain("tenant_id")
  expect(Object.keys(command)).not.toContain("requester")
  expect(context).toEqual({
    csrfToken: session.csrf_token,
    idempotencyKey: "idempotency-requester-fixed",
  })
  expect(await screen.findByRole("status")).toHaveTextContent(
    "Request request-0001 recorded at revision 1 in state submitted.",
  )
})

test("submits a data access request carrying every field its payload model requires", async () => {
  const user = userEvent.setup()
  client.createRequest.mockResolvedValue(createdEnvelope)
  renderSurface()

  await user.click(await screen.findByRole("radio", {name: "Data access request"}))
  await user.type(screen.getByRole("textbox", {name: "Request title"}), "Invoice recognition")
  await user.type(
    screen.getByRole("textbox", {name: "Purpose"}),
    "Investigate delayed invoice recognition.",
  )
  await user.type(
    screen.getByRole("textbox", {name: "Data product reference"}),
    "product-revenue",
  )
  await user.type(
    screen.getByRole("textbox", {name: "Requested fields"}),
    "invoice_id, recognized_at",
  )
  await user.selectOptions(screen.getByRole("combobox", {name: "Access mode"}), "query")
  await user.type(screen.getByLabelText("Access expires at (UTC)"), "2026-09-08T09:00")
  await user.click(screen.getByRole("button", {name: "Submit request"}))

  await waitFor(() => expect(client.createRequest).toHaveBeenCalledTimes(1))
  expect(client.createRequest.mock.calls[0]![0].request).toEqual({
    kind: "data_access",
    purpose: "Investigate delayed invoice recognition.",
    data_product_ref: "product-revenue",
    requested_fields: ["invoice_id", "recognized_at"],
    access_mode: "query",
    expires_at: "2026-09-08T09:00:00Z",
  })
})

test("follows only the requester's own requests and no reviewer or evidence projection", async () => {
  renderSurface()

  const list = await screen.findByRole("list", {name: "My requests"})
  const items = within(list).getAllByRole("listitem")
  expect(items).toHaveLength(1)
  expect(items[0]).toHaveTextContent("Weekly net revenue movement")
  expect(items[0]).toHaveTextContent("Explain the weekly net revenue movement.")
  expect(items[0]).toHaveTextContent("clarifying")
  expect(items[0]).toHaveTextContent("Clarified outcome statement")

  expect(client.getInbox).not.toHaveBeenCalled()
  expect(client.getRequestDetail).not.toHaveBeenCalled()
  expect(client.getEvidence).not.toHaveBeenCalled()
  expect(screen.queryByText(/candidate/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/effective (access )?scope/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/approver/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/reviewer/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/evidence/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/other request/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/hidden|withheld from you|not shown/i)).not.toBeInTheDocument()
})

test("labels the PillarMesh question, the requester reply, and the architect intervention", async () => {
  renderSurface("request-blocked-acceptance")

  const thread = await screen.findByRole("list", {name: "Clarification conversation"})
  const messages = within(thread).getAllByRole("listitem")
  expect(messages[0]).toHaveTextContent("PillarMesh question")
  expect(messages[0]!.className).toContain("conversation-message--pillarmesh")
  expect(messages[1]).toHaveTextContent("Your reply")
  expect(messages[1]!.className).toContain("conversation-message--requester")
  expect(messages[2]).toHaveTextContent("Architect intervention")
  expect(messages[2]!.className).toContain("conversation-message--architect")

  expect(
    within(messages[0]!).getByText("Does <b>weekly</b> mean the ISO week ending Sunday?"),
  ).toBeVisible()
  expect(messages[0]!.querySelector("b")).toBeNull()
})

test("posts a requester reply bound to the conversation revision and its digest", async () => {
  const user = userEvent.setup()
  client.appendConversationMessage.mockResolvedValue({
    ...conversationEnvelope,
    data: {
      ...conversation,
      revision: 3,
      awaiting_role: null,
      messages: [
        ...conversation.messages!,
        {
          message_id: "message-new",
          author_label: "Riley Requester",
          author_role: "requester",
          body: "Yes, the ISO week ending Sunday.",
          created_at: "2026-09-01T09:10:00Z",
        },
      ],
    },
  })
  renderSurface("request-blocked-acceptance")

  await user.type(
    await screen.findByRole("textbox", {name: "Your reply"}),
    "Yes, the ISO week ending Sunday.",
  )
  await user.click(screen.getByRole("button", {name: "Send reply"}))

  await waitFor(() => expect(client.appendConversationMessage).toHaveBeenCalledTimes(1))
  expect(client.appendConversationMessage.mock.calls[0]).toEqual([
    "request-blocked-acceptance",
    {
      expected_revision: 2,
      conversation_digest: conversationDigest,
      active_role: "requester",
      body: "Yes, the ISO week ending Sunday.",
    },
    {csrfToken: session.csrf_token, idempotencyKey: "idempotency-requester-fixed"},
  ])
  expect(await screen.findByText("Yes, the ISO week ending Sunday.")).toBeVisible()
})

test("shows no proposal while the clarified outcome is unaccepted", async () => {
  renderSurface("request-blocked-acceptance")

  const outcome = await screen.findByRole("region", {name: "Clarified outcome"})
  expect(within(outcome).getByText("Explain the weekly net revenue movement.")).toBeVisible()
  expect(within(outcome).getByText("Synthetic aggregate revenue only.")).toBeVisible()
  expect(within(outcome).getByText("Customer and payment details.")).toBeVisible()
  expect(
    screen.getByRole("checkbox", {
      name: `I confirm the exact clarified-outcome digest ${outcomeDigest}.`,
    }),
  ).toBeVisible()
  expect(screen.getByRole("button", {name: "Accept clarified outcome"})).toBeDisabled()
  expect(
    screen.getByText("No proposal exists while the clarified outcome is unaccepted."),
  ).toBeVisible()
  expect(screen.queryByRole("region", {name: "Proposal"})).not.toBeInTheDocument()
  expect(screen.queryByText(/answer/i)).not.toBeInTheDocument()
})

test("records acceptance as the requester and still withholds the answer without a receipt", async () => {
  const user = userEvent.setup()
  client.acceptClarifiedOutcome.mockResolvedValue({
    ...outcomeEnvelope,
    data: {...clarifiedOutcome, revision: 3, accepted: true},
  })
  renderSurface("request-blocked-acceptance")

  await user.click(
    await screen.findByRole("checkbox", {
      name: `I confirm the exact clarified-outcome digest ${outcomeDigest}.`,
    }),
  )
  await user.click(screen.getByRole("button", {name: "Accept clarified outcome"}))

  await waitFor(() => expect(client.acceptClarifiedOutcome).toHaveBeenCalledTimes(1))
  expect(client.acceptClarifiedOutcome.mock.calls[0]).toEqual([
    "request-blocked-acceptance",
    {
      expected_revision: 2,
      clarified_outcome_digest: outcomeDigest,
      active_role: "requester",
      decision: "approve",
    },
    {csrfToken: session.csrf_token, idempotencyKey: "idempotency-requester-fixed"},
  ])
  expect(
    await screen.findByText("Acceptance recorded at revision 3. PillarMesh can now build a proposal."),
  ).toBeVisible()
  expect(
    screen.getByText("The answer stays withheld until a verified delivery receipt exists."),
  ).toBeVisible()
  expect(screen.queryByRole("region", {name: "Proposal"})).not.toBeInTheDocument()
})

test("does not apply the decision on a stale digest and reloads the exact new outcome", async () => {
  const user = userEvent.setup()
  client.acceptClarifiedOutcome.mockRejectedValue(
    new ConsoleApiError(409, {
      meta: {correlation_id: "correlation-stale", data_provenance: "demo_fixture"},
      error: {
        code: "stale_digest",
        safe_message: "The clarified outcome changed; reload before responding.",
        recovery_action: "reload",
      },
    }),
  )
  const refreshedDigest = "d".repeat(64)
  client.getClarifiedOutcome
    .mockResolvedValueOnce(outcomeEnvelope)
    .mockResolvedValueOnce({
      ...outcomeEnvelope,
      data: {...clarifiedOutcome, revision: 4, statement_digest: refreshedDigest},
    })
  renderSurface("request-blocked-acceptance")

  await user.click(
    await screen.findByRole("checkbox", {
      name: `I confirm the exact clarified-outcome digest ${outcomeDigest}.`,
    }),
  )
  await user.click(screen.getByRole("button", {name: "Accept clarified outcome"}))

  expect(
    await screen.findByRole("checkbox", {
      name: `I confirm the exact clarified-outcome digest ${refreshedDigest}.`,
    }),
  ).not.toBeChecked()
  expect(screen.getByRole("button", {name: "Accept clarified outcome"})).toBeDisabled()
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "The clarified outcome changed; review the new statement before responding.",
  )
})

test("treats a request that is not the requester's own as one that does not exist", async () => {
  renderSurface("request-someone-else")

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "The requested resource is unavailable.",
  )
  expect(client.getConversation).not.toHaveBeenCalled()
  expect(client.getClarifiedOutcome).not.toHaveBeenCalled()
  expect(screen.queryByText(/request-someone-else/)).not.toBeInTheDocument()
})

test.each([
  ["data_owner", "Data owner note"],
  ["policy_approver", "Policy approver note"],
  ["budget_approver", "Budget approver note"],
  [null, "Role not recorded"],
] as const)("displays the recorded %s role without calling it an architect", async (author_role, label) => {
  client.getConversation.mockResolvedValue({
    ...conversationEnvelope,
    data: {...conversation, messages: [{
      message_id: "message-role", author_label: "Recorded actor", author_role,
      body: "Recorded contribution", created_at: "2026-09-01T09:00:00Z",
    }]},
  } satisfies ConsoleEnvelopeConversationView)
  renderSurface("request-blocked-acceptance")

  const thread = await screen.findByRole("list", {name: "Clarification conversation"})
  expect(thread).toHaveTextContent(label)
  expect(thread).not.toHaveTextContent("Architect intervention")
})
