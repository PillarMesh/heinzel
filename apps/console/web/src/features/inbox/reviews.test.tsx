import {fireEvent, render, screen, waitFor} from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import {expect, test, vi} from "vitest"

import type {
  AccessPreviewProposalView,
  ConsoleEnvelopeCatalogAssetView,
  ConsoleEnvelopeConversationView,
  ConsoleEnvelopeDashboardView,
  ConversationView,
  EvidenceContextView,
  LifecycleEventView,
  SessionView,
  StakeholderAnswerProposalView,
} from "../../api/generated"
import {ConsoleApiError} from "../../api/client"
import {AccessPreviewReview} from "./access-preview-review"
import {CatalogEvidence} from "./catalog-evidence"
import {ConversationPanel} from "./conversation-panel"
import {DashboardPreview} from "./dashboard-preview"
import {EvidenceDrawer} from "./evidence-drawer"
import {LifecycleTimeline} from "./lifecycle-timeline"
import {StakeholderAnswerReview} from "./stakeholder-answer-review"

const session: SessionView = {
  actor: {display_name: "Dana Architect"},
  roles: ["data_architect"],
  active_role: "data_architect",
  tenant: {ref: "tenant-demo", display_name: "Northwind Demo"},
  workspace: {ref: "workspace-revenue", display_name: "Revenue to cash"},
  csrf_token: "csrf-token-with-at-least-thirty-two-characters",
}

const answerProposal: StakeholderAnswerProposalView = {
  kind: "stakeholder_answer",
  purpose: "Explain the weekly net revenue movement.",
  candidate: "Synthetic net revenue increased after delayed invoices were recognized.",
  metric_version: "net-revenue-v1",
  as_of: "2026-01-01T00:00:00Z",
  freshness: "current",
  quality_limitations: ["Fixture values are synthetic and cannot support a real decision."],
  datasets: [{dataset_ref: "dataset-orders", display_name: "Synthetic orders"}],
  lineage_summary: "Synthetic orders to net revenue.",
  authorization_summary: "Data owner approval remains required.",
  required_authorities: [
    {role: "data_owner", reason: "Approve the stakeholder answer scope.", satisfied: false},
  ],
}

const accessProposal: AccessPreviewProposalView = {
  kind: "access_preview",
  purpose: "Investigate delayed invoice recognition.",
  data_product_ref: "product-revenue",
  access_mode: "query",
  requested_fields: ["invoice_id", "recognized_at"],
  effective_scope: ["invoice_id"],
  exclusions: ["recognized_at"],
  expires_at: "2026-01-08T00:00:00Z",
  intended_checks: ["Can query synthetic invoice identifiers."],
  denied_checks: ["Cannot query excluded recognition timestamps."],
  authority_summary: "Policy approval remains required.",
  required_authorities: [
    {role: "policy_approver", reason: "Approve the effective access scope.", satisfied: false},
  ],
}

const evidence: EvidenceContextView = {
  datasets: [{dataset_ref: "dataset-orders", display_name: "Synthetic orders"}],
  metric_versions: ["net-revenue-v1"],
  as_of: "2026-01-01T00:00:00Z",
  freshness: "current",
  quality_summary: "Synthetic evidence context only.",
  lineage_summary: "Synthetic orders to the revenue data product.",
  authorization_summary: "Fixture projection only; no governed evidence exists.",
  evidence_refs: ["evidence-openmetadata-roundtrip"],
}

const lifecycle: readonly LifecycleEventView[] = [
  {
    event_id: "event-submitted",
    state: "submitted",
    summary: "Request submitted by the requester.",
    occurred_at: "2026-01-01T00:00:00Z",
  },
  {
    event_id: "event-proposed",
    state: "proposed",
    summary: "Candidate answer proposed for review.",
    occurred_at: "2026-01-01T01:00:00Z",
  },
]

const conversation: ConversationView = {
  request_id: "request-answer",
  revision: 2,
  conversation_digest: "c".repeat(64),
  awaiting_role: "requester",
  messages: [
    {
      message_id: "message-question",
      author_role: "heinzel",
      author_label: "Heinzel",
      body: "Which invoices count as recognized?",
      created_at: "2026-01-01T00:10:00Z",
    },
    {
      message_id: "message-reply",
      author_role: "requester",
      author_label: "Robin Requester",
      body: "<img src=x onerror=alert(1)>",
      created_at: "2026-01-01T00:20:00Z",
    },
  ],
}

test("the stakeholder answer review states every grounding fact the architect must judge", () => {
  render(<StakeholderAnswerReview proposal={answerProposal} />)

  const review = screen.getByRole("region", {name: "Stakeholder answer proposal"})
  expect(review).toHaveTextContent("Explain the weekly net revenue movement.")
  expect(review).toHaveTextContent(
    "Synthetic net revenue increased after delayed invoices were recognized.",
  )
  expect(review).toHaveTextContent("net-revenue-v1")
  // Shown as a person reads it, with the exact stored instant kept on the element for anything
  // that parses the page rather than looks at it.
  expect(review).toHaveTextContent("Jan 1, 2026, 00:00:00 UTC")
  expect(review.querySelector("time")).toHaveAttribute("datetime", "2026-01-01T00:00:00Z")
  expect(review).toHaveTextContent("Current")
  expect(review).toHaveTextContent(
    "Fixture values are synthetic and cannot support a real decision.",
  )
  expect(review).toHaveTextContent("Synthetic orders")
  expect(review).toHaveTextContent("Synthetic orders to net revenue.")
  expect(review).toHaveTextContent("Data owner approval remains required.")
  expect(screen.getByRole("list", {name: "Required authorities"})).toHaveTextContent("Data owner")
})

test("the access preview review states the effective scope, exclusions, checks, and authority", () => {
  render(<AccessPreviewReview proposal={accessProposal} />)

  const review = screen.getByRole("region", {name: "Effective access preview"})
  expect(review).toHaveTextContent("Investigate delayed invoice recognition.")
  expect(screen.getByRole("list", {name: "Effective scope"})).toHaveTextContent("invoice_id")
  expect(screen.getByRole("list", {name: "Exclusions"})).toHaveTextContent("recognized_at")
  // Shown as a person reads it, with the stored instant kept on the element.
  expect(review).toHaveTextContent("Jan 8, 2026, 00:00:00 UTC")
  expect(review.querySelector("time")).toHaveAttribute("datetime", "2026-01-08T00:00:00Z")
  expect(screen.getByRole("list", {name: "Intended checks"})).toHaveTextContent(
    "Can query synthetic invoice identifiers.",
  )
  expect(screen.getByRole("list", {name: "Denied checks"})).toHaveTextContent(
    "Cannot query excluded recognition timestamps.",
  )
  expect(review).toHaveTextContent("Policy approval remains required.")
})

test("the lifecycle timeline lists server events in the order the server supplied them", () => {
  render(<LifecycleTimeline events={lifecycle} />)

  const entries = screen.getAllByRole("listitem")
  expect(entries[0]).toHaveTextContent("Request submitted by the requester.")
  expect(entries[1]).toHaveTextContent("Candidate answer proposed for review.")
})

test("the evidence drawer is a static region at wide width", () => {
  render(<EvidenceDrawer evidence={evidence} layout="wide" />)

  expect(screen.getByRole("region", {name: "Decision evidence"})).toHaveTextContent(
    "Fixture projection only; no governed evidence exists.",
  )
  expect(screen.queryByRole("button", {name: "Show evidence"})).toBeNull()
})

test("the medium-width evidence drawer takes focus and restores it to its trigger", async () => {
  const user = userEvent.setup()
  render(<EvidenceDrawer evidence={evidence} layout="medium" />)

  const trigger = screen.getByRole("button", {name: "Show evidence"})
  await user.click(trigger)

  const drawer = await screen.findByRole("dialog", {name: "Decision evidence"})
  await waitFor(() => expect(drawer).toHaveFocus())

  await user.keyboard("{Escape}")

  expect(screen.queryByRole("dialog", {name: "Decision evidence"})).toBeNull()
  expect(trigger).toHaveFocus()
})

test("catalog evidence renders the server-issued opaque link and never a provider URL", async () => {
  const envelope: ConsoleEnvelopeCatalogAssetView = {
    meta: {correlation_id: "correlation-catalog", data_provenance: "demo_fixture"},
    data: {
      asset_ref: "asset-revenue",
      display_name: "Synthetic net revenue",
      definition: "Recognized invoice value in the fixture scenario.",
      owner: "Synthetic Finance Data",
      classifications: ["synthetic"],
      lineage_summary: "Synthetic orders to net revenue.",
      link_ref: "link-catalog-revenue",
    },
  }
  const client = {getCatalogAsset: vi.fn(async () => envelope)}

  render(
    <CatalogEvidence assetRef="asset-revenue" client={client} dataProvenance="demo_fixture" />,
  )

  const link = await screen.findByRole("link", {name: "Open Synthetic net revenue in the catalog"})
  expect(link).toHaveAttribute("href", "/api/v1/links/link-catalog-revenue")
  expect(screen.getByText("Recognized invoice value in the fixture scenario.")).toBeInTheDocument()
})

test("catalog evidence fails closed when the catalog record cannot be displayed safely", async () => {
  const client = {
    getCatalogAsset: vi.fn(async () => {
      throw new Error("transport")
    }),
  }

  render(<CatalogEvidence assetRef="asset-revenue" client={client} dataProvenance="demo_fixture" />)

  expect(await screen.findByRole("status")).toHaveTextContent("Catalog record unavailable")
})

test("the dashboard preview loads a same-origin opaque image route only", async () => {
  const envelope: ConsoleEnvelopeDashboardView = {
    meta: {correlation_id: "correlation-dashboard", data_provenance: "demo_fixture"},
    data: {
      dashboard_ref: "dashboard-revenue",
      display_name: "Synthetic revenue overview",
      version: 1,
      lifecycle_state: "active",
      as_of: "2026-09-01T15:55:00Z",
      freshness: "current",
      access_state: "workspace_role",
      published_at: "2026-09-01T16:00:00Z",
      summary: "The downstream dashboard is intentionally unavailable.",
      state: "not_delivered",
      preview_ref: "preview-dashboard-revenue",
      link_ref: "link-dashboard-revenue",
    },
  }
  const client = {getDashboard: vi.fn(async () => envelope)}

  render(
    <DashboardPreview
      client={client}
      dashboardRef="dashboard-revenue"
      dataProvenance="demo_fixture"
    />,
  )

  const image = await screen.findByRole("img", {
    name: "Rendered preview of Synthetic revenue overview",
  })
  expect(image).toHaveAttribute("src", "/api/v1/previews/preview-dashboard-revenue")
  expect(screen.getByRole("link", {name: "Open Synthetic revenue overview in dashboards"})).toHaveAttribute(
    "href",
    "/api/v1/links/link-dashboard-revenue",
  )
})

test("a failed dashboard render shows its own unavailable state and reports unavailability", async () => {
  const envelope: ConsoleEnvelopeDashboardView = {
    meta: {correlation_id: "correlation-dashboard", data_provenance: "demo_fixture"},
    data: {
      dashboard_ref: "dashboard-revenue",
      display_name: "Synthetic revenue overview",
      version: 1,
      lifecycle_state: "active",
      as_of: "2026-09-01T15:55:00Z",
      freshness: "current",
      access_state: "workspace_role",
      published_at: "2026-09-01T16:00:00Z",
      summary: "The downstream dashboard is intentionally unavailable.",
      state: "not_delivered",
      preview_ref: "preview-dashboard-revenue",
      link_ref: null,
    },
  }
  const client = {getDashboard: vi.fn(async () => envelope)}
  const onAvailabilityChange = vi.fn()

  render(
    <DashboardPreview
      client={client}
      dashboardRef="dashboard-revenue"
      dataProvenance="demo_fixture"
      onAvailabilityChange={onAvailabilityChange}
    />,
  )

  fireEvent.error(
    await screen.findByRole("img", {name: "Rendered preview of Synthetic revenue overview"}),
  )

  expect(await screen.findByRole("status")).toHaveTextContent("Dashboard preview unavailable")
  await waitFor(() => expect(onAvailabilityChange).toHaveBeenLastCalledWith(false))
})

test("the conversation panel renders requester text as text and labels every author role", () => {
  render(<ConversationPanel conversation={conversation} session={session} />)

  const panel = screen.getByRole("region", {name: "Clarification conversation"})
  expect(panel).toHaveTextContent("<img src=x onerror=alert(1)>")
  expect(panel.querySelector("img")).toBeNull()
  expect(panel).toHaveTextContent("Heinzel question")
  expect(panel).toHaveTextContent("Requester reply")
})

test("a read-only conversation projection offers no intervention and says so", () => {
  render(<ConversationPanel conversation={conversation} session={session} />)

  expect(screen.getByRole("button", {name: "Send architect message"})).toBeDisabled()
  expect(screen.getByRole("status")).toHaveTextContent(
    "This projection is read-only, so the conversation cannot be joined from here.",
  )
})

test("an architect intervention replaces the conversation with the authoritative projection", async () => {
  const user = userEvent.setup()
  const updated: ConsoleEnvelopeConversationView = {
    meta: {correlation_id: "correlation-conversation", data_provenance: "demo_fixture"},
    data: {
      ...conversation,
      revision: 3,
      awaiting_role: "requester",
      messages: [
        ...(conversation.messages ?? []),
        {
          message_id: "message-intervention",
          author_role: "data_architect",
          author_label: "Dana Architect",
          body: "Recognized means invoiced and accepted.",
          created_at: "2026-01-01T00:30:00Z",
        },
      ],
    },
  }
  const client = {appendConversationMessage: vi.fn(async () => updated)}

  render(
    <ConversationPanel
      client={client}
      conversation={conversation}
      idempotencyKeyFactory={() => "idempotency-conversation"}
      session={session}
    />,
  )

  await user.type(
    screen.getByRole("textbox", {name: "Architect message"}),
    "Recognized means invoiced and accepted.",
  )
  await user.click(screen.getByRole("button", {name: "Send architect message"}))

  expect(await screen.findByText("Recognized means invoiced and accepted.")).toBeInTheDocument()
  expect(client.appendConversationMessage).toHaveBeenCalledWith(
    "request-answer",
    {
      active_role: "data_architect",
      body: "Recognized means invoiced and accepted.",
      conversation_digest: conversation.conversation_digest,
      expected_revision: 2,
    },
    {
      csrfToken: "csrf-token-with-at-least-thirty-two-characters",
      idempotencyKey: "idempotency-conversation",
    },
  )
})

test("a second architect reply carries the digest the first reply returned", async () => {
  // The panel took the revision from the conversation a send returned but the digest
  // from a prop nothing refreshed, so after one reply the two disagreed and the
  // server refused everything after it with `stale_revision`.
  const user = userEvent.setup()
  const replies = [
    {...conversation, revision: 3, conversation_digest: "d".repeat(64)},
    {...conversation, revision: 4, conversation_digest: "e".repeat(64)},
  ]
  const appendConversationMessage = vi.fn(async () => ({
    meta: {correlation_id: "correlation-conversation", data_provenance: "demo_fixture" as const},
    data: replies.shift() ?? conversation,
  }))

  render(
    <ConversationPanel
      client={{appendConversationMessage}}
      conversation={conversation}
      session={session}
    />,
  )

  const compose = screen.getByRole("textbox", {name: "Architect message"})
  await user.type(compose, "First.")
  await user.click(screen.getByRole("button", {name: "Send architect message"}))
  await waitFor(() => expect(appendConversationMessage).toHaveBeenCalledTimes(1))
  await user.type(compose, "Second.")
  await user.click(screen.getByRole("button", {name: "Send architect message"}))
  await waitFor(() => expect(appendConversationMessage).toHaveBeenCalledTimes(2))

  expect(appendConversationMessage).toHaveBeenNthCalledWith(
    1,
    conversation.request_id,
    expect.objectContaining({
      conversation_digest: conversation.conversation_digest,
      expected_revision: conversation.revision,
    }),
    expect.anything(),
  )
  expect(appendConversationMessage).toHaveBeenNthCalledWith(
    2,
    conversation.request_id,
    expect.objectContaining({conversation_digest: "d".repeat(64), expected_revision: 3}),
    expect.anything(),
  )
})

test("a refused reply reports the server's own reason and keeps the message", async () => {
  // The bare `catch` discarded a typed error carrying the cause and the recovery
  // action, so the architect was told only that something failed.
  const user = userEvent.setup()
  const appendConversationMessage = vi.fn(async () => {
    throw new ConsoleApiError(409, {
      meta: {correlation_id: "correlation-stale", data_provenance: "demo_fixture"},
      error: {
        code: "stale_revision",
        safe_message: "The conversation changed. Reload it before posting a message.",
        recovery_action: "reload",
        field: null,
      },
    })
  })

  render(
    <ConversationPanel
      client={{appendConversationMessage}}
      conversation={conversation}
      session={session}
    />,
  )

  const compose = screen.getByRole("textbox", {name: "Architect message"})
  await user.type(compose, "Following up.")
  await user.click(screen.getByRole("button", {name: "Send architect message"}))

  expect(
    await screen.findByText("The conversation changed. Reload it before posting a message."),
  ).toBeVisible()
  expect(compose).toHaveValue("Following up.")
})

test("legacy conversation entries explicitly say their role was not recorded", () => {
  render(<ConversationPanel conversation={{...conversation, messages: [{
    message_id: "message-legacy", author_role: null, author_label: "Legacy actor",
    body: "Historical contribution", created_at: "2026-01-01T00:10:00Z",
  }]}} session={session} />)

  const panel = screen.getByRole("region", {name: "Clarification conversation"})
  expect(panel).toHaveTextContent("Role not recorded")
  expect(panel).not.toHaveTextContent("Architect intervention")
})

test("governed review shows exact artifact versions without inventing catalog links", () => {
  const reference = {artifact_id: "urn:catalog/Revenue <Q1>", version: 2, digest: "a".repeat(64)}
  render(<StakeholderAnswerReview proposal={{...answerProposal,
    datasets: [{dataset_ref: "artifact-reference", display_name: "Revenue", artifact_reference: reference}],
    metric_references: [reference], quality_limitations: [], quality_references: [reference],
    required_approvals: [{authority_ref: "role:data_owner", reason: "ownership_review", satisfied: false}],
  }} />)

  expect(screen.getAllByText(reference.artifact_id).length).toBeGreaterThan(0)
  expect(screen.getAllByText(reference.digest).length).toBeGreaterThan(0)
  expect(screen.getByRole("region", {name: "Required approvals"})).toHaveTextContent("Not recorded")
  expect(screen.queryByText("No quality limitation was recorded.")).not.toBeInTheDocument()
  expect(screen.queryByRole("link")).not.toBeInTheDocument()
})

test("required approvals name the approving authority rather than its internal reference", () => {
  render(<StakeholderAnswerReview proposal={{...answerProposal,
    required_approvals: [
      {authority_ref: "principal:requester-a", authority_label: "Requester", reason: "clarified_outcome_acceptance", satisfied: true},
      {authority_ref: "role:data_engineering_architect", authority_label: "Data engineering architect", reason: "architect_review", satisfied: false},
    ],
  }} />)

  const approvals = screen.getByRole("region", {name: "Required approvals"})
  expect(approvals).toHaveTextContent("Requester · clarified outcome acceptance · Recorded")
  expect(approvals).toHaveTextContent("Data engineering architect · architect review · Not recorded")
  expect(approvals).not.toHaveTextContent("principal:requester-a")
  expect(approvals).not.toHaveTextContent("role:data_engineering_architect")
})
