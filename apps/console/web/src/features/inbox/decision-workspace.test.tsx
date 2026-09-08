import {validateConsoleResponse} from "../../api/schema"
import {render, screen, waitFor, within} from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import {expect, test, vi} from "vitest"

import type {
  ConsoleEnvelopeInboxView,
  ConsoleEnvelopeRequestDetailView,
  InboxItemView,
  RequestDetailView,
  SessionView,
} from "../../api/generated"
import {ConsoleApiError, ConsoleMutationOutcomeUnknown} from "../../api/client"
import {DecisionWorkspace, type InboxClient} from "./decision-workspace"

const session: SessionView = {
  actor: {display_name: "Dana Architect"},
  roles: ["data_architect"],
  active_role: "data_architect",
  tenant: {ref: "tenant-demo", display_name: "Northwind Demo"},
  workspace: {ref: "workspace-revenue", display_name: "Revenue to cash"},
  csrf_token: "csrf-token-with-at-least-thirty-two-characters",
}

const items: InboxItemView[] = [
  {
    request_id: "request-access",
    kind: "data_access",
    state: "awaiting_approval",
    title: "Invoice recognition access",
    purpose: "Investigate delayed invoice recognition.",
    risk: "critical",
    deadline: "2026-01-05T00:00:00Z",
    blocked_reason: null,
  },
  {
    request_id: "request-answer",
    kind: "stakeholder_question",
    state: "proposed",
    title: "Weekly revenue movement",
    purpose: "Explain the weekly net revenue movement.",
    risk: "low",
    deadline: null,
    blocked_reason: null,
  },
  {
    request_id: "request-blocked-acceptance",
    kind: "stakeholder_question",
    state: "clarifying",
    title: "Disputed invoice treatment",
    purpose: "Decide whether disputed invoices are collectible.",
    risk: "medium",
    deadline: null,
    blocked_reason: "Requester acceptance is required before architect action.",
  },
]

const inboxEnvelope: ConsoleEnvelopeInboxView = {
  meta: {correlation_id: "correlation-inbox", data_provenance: "demo_fixture"},
  data: {items, selected_request_id: null},
}

const answerDetail: RequestDetailView = {
  request_id: "request-answer",
  kind: "stakeholder_question",
  state: "proposed",
  title: "Weekly revenue movement",
  purpose: "Explain the weekly net revenue movement.",
  revision: 2,
  proposal_digest: "a".repeat(64),
  proposal: {
    kind: "stakeholder_answer",
    purpose: "Explain the weekly net revenue movement.",
    candidate: "Synthetic net revenue increased after delayed invoices were recognized.",
    metric_version: "net-revenue-v1",
    as_of: "2026-01-01T00:00:00Z",
    freshness: "current",
    quality_limitations: ["Fixture values are synthetic."],
    datasets: [{dataset_ref: "dataset-orders", display_name: "Synthetic orders"}],
    lineage_summary: "Synthetic orders to net revenue.",
    authorization_summary: "Data owner approval remains required.",
    required_authorities: [
      {role: "data_owner", reason: "Approve the stakeholder answer scope.", satisfied: true},
    ],
  },
  conversation: {
    request_id: "request-answer",
    conversation_digest: "c".repeat(64),
    revision: 2,
    awaiting_role: "requester",
    messages: [],
  },
  lifecycle: [
    {
      event_id: "event-proposed",
      state: "proposed",
      summary: "Candidate answer proposed for review.",
      occurred_at: "2026-01-01T01:00:00Z",
    },
  ],
  evidence: {
    datasets: [{dataset_ref: "dataset-orders", display_name: "Synthetic orders"}],
    metric_versions: ["net-revenue-v1"],
    as_of: "2026-01-01T00:00:00Z",
    freshness: "current",
    quality_summary: "Synthetic evidence context only.",
    lineage_summary: "Synthetic orders to the revenue data product.",
    authorization_summary: "Fixture projection only.",
    evidence_refs: ["evidence-openmetadata-roundtrip"],
  },
  available_actions: ["approve", "reject", "request_changes"],
}

const blockedDetail: RequestDetailView = {
  ...answerDetail,
  request_id: "request-blocked-acceptance",
  state: "clarifying",
  title: "Disputed invoice treatment",
  revision: 2,
  proposal_digest: "c".repeat(64),
  proposal: {
    ...(answerDetail.proposal as Extract<
      NonNullable<RequestDetailView["proposal"]>,
      {kind: "stakeholder_answer"}
    >),
    required_authorities: [
      {
        role: "requester",
        reason: "Accept the clarified outcome before the architect decides.",
        satisfied: false,
      },
      {role: "data_owner", reason: "Approve the stakeholder answer scope.", satisfied: true},
    ],
  },
  conversation: {
    request_id: "request-blocked-acceptance",
    conversation_digest: "c".repeat(64),
    revision: 2,
    awaiting_role: "requester",
    messages: [],
  },
  available_actions: ["approve", "reject", "request_changes"],
}

const accessDetail: RequestDetailView = {
  ...answerDetail,
  request_id: "request-access",
  kind: "data_access",
  state: "awaiting_approval",
  title: "Invoice recognition access",
  revision: 4,
  proposal_digest: "b".repeat(64),
  proposal: {
    kind: "access_preview",
    purpose: "Investigate delayed invoice recognition.",
    data_product_ref: "product-revenue",
    access_mode: "query",
    requested_fields: ["invoice_id"],
    effective_scope: ["invoice_id"],
    exclusions: [],
    expires_at: "2026-01-08T00:00:00Z",
    intended_checks: ["Can query synthetic invoice identifiers."],
    denied_checks: [],
    authority_summary: "Policy approval remains required.",
    required_authorities: [
      {role: "policy_approver", reason: "Approve the effective access scope.", satisfied: true},
    ],
  },
  conversation: {
    request_id: "request-access",
    conversation_digest: "c".repeat(64),
    revision: 4,
    awaiting_role: "requester",
    messages: [],
  },
  available_actions: ["approve", "reject", "request_changes"],
}

const details: Readonly<Record<string, RequestDetailView>> = {
  "request-answer": answerDetail,
  "request-access": accessDetail,
  "request-blocked-acceptance": blockedDetail,
}

function detailEnvelope(detail: RequestDetailView): ConsoleEnvelopeRequestDetailView {
  return {
    meta: {correlation_id: "correlation-detail", data_provenance: "demo_fixture"},
    data: detail,
  }
}

function createClient(overrides: Partial<InboxClient> = {}): InboxClient {
  return {
    getInbox: vi.fn(async () => inboxEnvelope),
    getRequestDetail: vi.fn(async (requestId: string) => {
      const detail = details[requestId]
      if (detail === undefined) {
        throw new Error(`unknown request ${requestId}`)
      }
      return detailEnvelope(detail)
    }),
    decideRequest: vi.fn(),
    appendConversationMessage: vi.fn(),
    getCatalogAsset: vi.fn(async () => {
      throw new Error("no catalog record")
    }),
    getDashboard: vi.fn(async () => {
      throw new Error("no dashboard")
    }),
    ...overrides,
  } as InboxClient
}

function requireOption(option: HTMLElement | undefined): HTMLElement {
  if (option === undefined) {
    throw new Error("the queue did not render the expected option")
  }
  return option
}

async function findQueueOptions(): Promise<HTMLElement[]> {
  const queue = await screen.findByRole("listbox", {name: "Prioritized requests"})
  return within(queue).getAllByRole("option")
}

function queueOptions(): HTMLElement[] {
  return within(screen.getByRole("listbox", {name: "Prioritized requests"})).getAllByRole("option")
}

function renderWorkspace(client: InboxClient, requestedRequestId?: string) {
  return render(
    <DecisionWorkspace
      client={client}
      dataProvenance="demo_fixture"
      idempotencyKeyFactory={() => "idempotency-decision"}
      layout="wide"
      requestedRequestId={requestedRequestId}
      session={session}
    />,
  )
}

test("the queue preserves the server-issued order and never re-sorts it in the browser", async () => {
  renderWorkspace(createClient())

  const options = await findQueueOptions()
  expect(options.map((option) => option.textContent)).toEqual([
    expect.stringContaining("Invoice recognition access"),
    expect.stringContaining("Weekly revenue movement"),
    expect.stringContaining("Disputed invoice treatment"),
  ])
})

test("request-type and lifecycle-state filters narrow the queue without reordering it", async () => {
  const user = userEvent.setup()
  renderWorkspace(createClient())
  await findQueueOptions()

  await user.selectOptions(
    screen.getByRole("combobox", {name: "Request type"}),
    "stakeholder_question",
  )
  expect(queueOptions().map((option) => option.textContent)).toEqual([
    expect.stringContaining("Weekly revenue movement"),
    expect.stringContaining("Disputed invoice treatment"),
  ])

  await user.selectOptions(screen.getByRole("combobox", {name: "Lifecycle state"}), "clarifying")
  const remaining = queueOptions()
  expect(remaining).toHaveLength(1)
  expect(remaining[0]).toHaveTextContent("Disputed invoice treatment")
})

test("roving keyboard selection moves through the queue and Enter transfers focus to the detail", async () => {
  const user = userEvent.setup()
  renderWorkspace(createClient())

  const options = await findQueueOptions()
  await user.click(requireOption(options[0]))
  await screen.findByRole("region", {name: "Request detail"})

  await user.keyboard("{ArrowDown}")
  const selected = queueOptions()[1]
  expect(selected).toHaveFocus()
  expect(selected).toHaveAttribute("aria-selected", "true")
  expect(queueOptions()[0]).toHaveAttribute("tabindex", "-1")

  await user.keyboard("{Enter}")
  await waitFor(() =>
    expect(screen.getByRole("region", {name: "Request detail"})).toHaveFocus(),
  )
})

test("a request whose requester acceptance is missing is marked blocked and admits no decision", async () => {
  const user = userEvent.setup()
  renderWorkspace(createClient())

  const options = await findQueueOptions()
  expect(options[2]).toHaveTextContent("Blocked on requester")

  await user.click(requireOption(options[2]))
  const detail = await screen.findByRole("region", {name: "Request detail"})

  expect(detail).toHaveTextContent("Blocked on the requester")
  expect(detail).toHaveTextContent("Accept the clarified outcome before the architect decides.")
  expect(within(detail).queryByRole("button", {name: /Approve/})).toBeNull()
  expect(within(detail).queryByRole("button", {name: /Reject/})).toBeNull()
})

test("a decision is displayed as applied only after the server returns the new projection", async () => {
  const user = userEvent.setup()
  let release: (envelope: ConsoleEnvelopeRequestDetailView) => void = () => {}
  const pending = new Promise<ConsoleEnvelopeRequestDetailView>((resolve) => {
    release = resolve
  })
  const client = createClient({decideRequest: vi.fn(async () => pending)})
  renderWorkspace(client, "request-answer")

  const detail = await screen.findByRole("region", {name: "Request detail"})
  await user.click(within(detail).getByRole("checkbox"))
  await user.click(within(detail).getByRole("button", {name: "Approve"}))

  expect(await within(detail).findByRole("button", {name: "Submitting decision…"})).toBeDisabled()
  expect(detail).toHaveTextContent("proposed")
  expect(detail).not.toHaveTextContent("execution ready")

  release(detailEnvelope({...answerDetail, revision: 3, state: "execution_ready"}))

  await waitFor(() => expect(detail).toHaveTextContent("execution ready"))
  expect(client.decideRequest).toHaveBeenCalledWith(
    "request-answer",
    {
      active_role: "data_architect",
      decision: "approve",
      expected_revision: 2,
      reviewed_digest: "a".repeat(64),
    },
    {
      csrfToken: "csrf-token-with-at-least-thirty-two-characters",
      idempotencyKey: "idempotency-decision",
    },
  )
})

test("a stale decision reloads the exact new revision and preserves the unsubmitted comment", async () => {
  const user = userEvent.setup()
  const staleError = new ConsoleApiError(409, {
    meta: {correlation_id: "correlation-stale", data_provenance: "demo_fixture"},
    error: {
      code: "stale-revision",
      safe_message: "The proposal changed while it was being reviewed.",
      recovery_action: "reload",
      field: null,
    },
  })
  const refreshed = detailEnvelope({
    ...answerDetail,
    revision: 5,
    proposal_digest: "d".repeat(64),
  })
  const client = createClient({
    decideRequest: vi.fn(async () => {
      throw staleError
    }),
    getRequestDetail: vi
      .fn()
      .mockResolvedValueOnce(detailEnvelope(answerDetail))
      .mockResolvedValue(refreshed),
  })
  renderWorkspace(client, "request-answer")

  const detail = await screen.findByRole("region", {name: "Request detail"})
  await user.type(within(detail).getByRole("textbox", {name: "Review comment"}), "Needs owner sign-off")
  await user.click(within(detail).getByRole("checkbox"))
  await user.click(within(detail).getByRole("button", {name: "Approve"}))

  await waitFor(() =>
    expect(detail).toHaveTextContent("Review refreshed at revision 5; your comment was preserved."),
  )
  expect(within(detail).getByRole("textbox", {name: "Review comment"})).toHaveValue(
    "Needs owner sign-off",
  )
  expect(within(detail).getByRole("checkbox")).not.toBeChecked()
  expect(detail).toHaveTextContent("d".repeat(64))
})

test("an ambiguous outcome reconciles under the original idempotency key", async () => {
  const user = userEvent.setup()
  const keys: string[] = []
  const client = createClient({
    decideRequest: vi.fn(async (_requestId, _command, context) => {
      keys.push(context.idempotencyKey)
      throw new ConsoleMutationOutcomeUnknown(
        "request_detail_response",
        context.idempotencyKey,
        null,
        null,
      )
    }),
  })
  renderWorkspace(client, "request-answer")

  const detail = await screen.findByRole("region", {name: "Request detail"})
  await user.click(within(detail).getByRole("checkbox"))
  await user.click(within(detail).getByRole("button", {name: "Approve"}))

  expect(await within(detail).findByRole("alert")).toHaveTextContent("Outcome unknown; reconciling")

  await user.click(within(detail).getByRole("button", {name: "Reconcile the submitted decision"}))

  await waitFor(() => expect(keys).toHaveLength(2))
  expect(keys[1]).toBe(keys[0])
})

test("a queue that cannot be validated fails closed instead of rendering partial decisions", async () => {
  const client = createClient({
    getInbox: vi.fn(async () => {
      throw new Error("malformed")
    }),
  })
  renderWorkspace(client)

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "The decision queue could not be displayed safely.",
  )
  expect(screen.queryByRole("listbox")).toBeNull()
})

test("a detail projection from another provenance never reaches the decision surface", async () => {
  const client = createClient({
    getRequestDetail: vi.fn(async () => ({
      meta: {correlation_id: "correlation-detail", data_provenance: "governed_local" as const},
      data: answerDetail,
    })),
  })
  renderWorkspace(client, "request-answer")

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "The request detail could not be displayed safely.",
  )
  expect(screen.queryByRole("button", {name: "Approve"})).toBeNull()
})

test("the architect can reply in the clarification conversation", async () => {
  // `POST /api/v1/requests/{id}/conversation` is wired, and the detail projection
  // carries the digest the command needs, but the panel was rendered without the
  // client or the digest. Both props are optional, so it silently refused every
  // reply and told the architect the server had not supplied a digest it had.
  const user = userEvent.setup()
  const appendConversationMessage = vi.fn(async () => ({
    meta: {correlation_id: "correlation-conversation", data_provenance: "demo_fixture" as const},
    data: {
      ...answerDetail.conversation,
      revision: answerDetail.conversation.revision + 1,
      conversation_digest: "d".repeat(64),
      messages: [
        ...(answerDetail.conversation.messages ?? []),
        {
          message_id: "con-architect-reply",
          author_label: "architect-a",
          author_role: "data_architect" as const,
          body: "Using the governed definition.",
          created_at: "2026-09-01T16:05:00Z",
        },
      ],
    },
  }))
  const client = createClient({appendConversationMessage})
  renderWorkspace(client, answerDetail.request_id)

  const compose = await screen.findByRole("textbox", {name: "Architect message"})
  await user.type(compose, "Using the governed definition.")
  await user.click(screen.getByRole("button", {name: "Send architect message"}))

  await waitFor(() => expect(appendConversationMessage).toHaveBeenCalled())
  expect(appendConversationMessage).toHaveBeenCalledWith(
    answerDetail.request_id,
    expect.objectContaining({
      active_role: "data_architect",
      body: "Using the governed definition.",
      conversation_digest: answerDetail.conversation.conversation_digest,
      expected_revision: answerDetail.conversation.revision,
    }),
    expect.objectContaining({idempotencyKey: expect.any(String)}),
  )
  expect(
    screen.queryByText(
      "Intervention is unavailable until the server supplies the conversation digest.",
    ),
  ).toBeNull()
})

test("an admission whose outcome is unknown is reconciled, not reported as unchanged", async () => {
  // The console cannot say the proposal is unchanged when it does not know whether
  // the admission was recorded. Every other command offers reconciliation under the
  // same command identity; admission did not, and asserted the stronger claim.
  const user = userEvent.setup()
  const admitRequest = vi.fn(async (_requestId, _command, context) => {
    throw new ConsoleMutationOutcomeUnknown(
      "request_detail_response",
      context.idempotencyKey,
      null,
      null,
    )
  })
  const client = createClient({
    admitRequest,
    getRequestDetail: vi.fn(async () => detailEnvelope({...answerDetail, admission: {available: true, blocking_reason: null}})),
  })
  renderWorkspace(client, answerDetail.request_id)

  await user.click(await screen.findByRole("button", {name: "Admit to execution"}))

  expect(
    await screen.findByText(/Outcome unknown; reconciling\./),
  ).toBeVisible()
  expect(screen.queryByText(/The proposal is unchanged\./)).toBeNull()

  await user.click(screen.getByRole("button", {name: "Reconcile the submitted admission"}))

  expect(admitRequest).toHaveBeenCalledTimes(2)
  const first = admitRequest.mock.calls[0] as unknown as [string, object, {idempotencyKey: string}]
  const second = admitRequest.mock.calls[1] as unknown as [string, object, {idempotencyKey: string}]
  expect(second[2].idempotencyKey).toBe(first[2].idempotencyKey)
})

test("artifact display keys never become catalog or dashboard lookups", async () => {
  if (accessDetail.proposal?.kind !== "access_preview") throw new Error("Expected access fixture")
  const reference = {artifact_id: "urn:product/Revenue", version: 2, digest: "a".repeat(64)}
  const getCatalogAsset = vi.fn()
  const getDashboard = vi.fn()
  const detail: RequestDetailView = {...accessDetail,
    proposal: {...accessDetail.proposal!, kind: "access_preview", purpose: "Approved scope",
      data_product_ref: "artifact-display-key", data_product_reference: reference,
      access_mode: "dashboard", requested_fields: ["total"], expires_at: "2026-01-08T00:00:00Z",
      authority_summary: "Awaiting review",
    },
    evidence: {...accessDetail.evidence, evidence_refs: [], datasets: [{dataset_ref: "artifact-display-key",
      display_name: "Revenue", artifact_reference: reference}]},
  }
  const envelope = validateConsoleResponse("request_detail_response", detailEnvelope(detail))
  const client = createClient({getRequestDetail: vi.fn(async () => envelope), getCatalogAsset, getDashboard})

  renderWorkspace(client, "request-access")

  expect(await screen.findByRole("region", {name: "Effective access preview"})).toHaveTextContent(reference.artifact_id)
  expect(getCatalogAsset).not.toHaveBeenCalled()
  expect(getDashboard).not.toHaveBeenCalled()
})
