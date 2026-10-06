import {render, screen, waitFor} from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import {beforeEach, expect, test, vi} from "vitest"

import type {
  ConsoleEnvelopeReviewView,
  ConsoleEnvelopeSetupView,
  ReviewView,
  SessionView,
  SetupView,
} from "../../api/generated"
import {ConsoleApiError} from "../../api/client"
import {SetupWorkbench} from "./setup-workbench"

const session: SessionView = {
  actor: {display_name: "Dana Architect"},
  roles: ["data_architect", "data_owner", "budget_approver"],
  active_role: "data_architect",
  tenant: {ref: "tenant-demo", display_name: "Northwind Demo"},
  workspace: {ref: "workspace-revenue", display_name: "Revenue to cash"},
  csrf_token: "csrf-token-with-at-least-thirty-two-characters",
}

const setup: SetupView = {
  workspace_ref: "workspace-revenue",
  revision: 8,
  setup_digest: "a".repeat(64),
  reset_token: "reset_token_fixture_sequence-0008",
  active_stage: "meaning",
  stages: [
    {stage: "foundation", label: "Foundation", state: "complete", detail: null},
    {stage: "managed_services", label: "Managed services", state: "complete", detail: null},
    {stage: "sources", label: "Sources", state: "complete", detail: null},
    {stage: "business_process", label: "Business process", state: "complete", detail: null},
    {stage: "meaning", label: "Meaning review", state: "current", detail: null},
    {stage: "data_product", label: "Data-product review", state: "not_started", detail: null},
    {stage: "activation", label: "Activation", state: "not_started", detail: null},
  ],
  warehouse_options: [
    {
      engine: "postgresql",
      label: "PostgreSQL",
      supported_region: "us-west-2",
      fixed_capacity: "fixed-small",
    },
  ],
  warehouse_binding: null,
  managed_services: [],
  sources: [],
  process_package: null,
  pending_review_refs: ["review-meaning"],
}

const setupEnvelope: ConsoleEnvelopeSetupView = {
  meta: {correlation_id: "correlation-setup", data_provenance: "demo_fixture"},
  data: setup,
}

const meaningReview: ReviewView = {
  review_id: "review-meaning",
  kind: "meaning",
  title: "Meaning approval",
  summary: "Approve the business meaning used to design the product.",
  revision: 3,
  reviewed_digest: "b".repeat(64),
  sections: [
    {
      section_id: "process-model",
      title: "Process model",
      summary: "Revenue-to-cash meaning extracted from the package.",
      items: [
        {label: "Owner", value: "Revenue operations", material_change: false},
        {label: "Customer identity", value: "Billing account", material_change: true},
      ],
    },
    {
      section_id: "open-questions",
      title: "Open questions",
      summary: null,
      items: [
        {
          label: "Unresolved semantic question",
          value: "Does a disputed invoice count as collectible revenue?",
          material_change: false,
        },
      ],
    },
  ],
  required_authorities: [
    {
      role: "data_architect",
      reason: "Confirm the process model.",
      subject_digest: "c".repeat(64),
      satisfied: false,
    },
    {
      role: "data_owner",
      reason: "Confirm ownership and identity semantics.",
      subject_digest: "d".repeat(64),
      satisfied: false,
    },
  ],
  decisions: [],
  constraints: [],
  evidence_refs: ["evidence-openmetadata-roundtrip"],
  can_decide: true,
}

const meaningEnvelope: ConsoleEnvelopeReviewView = {
  meta: {correlation_id: "correlation-meaning", data_provenance: "demo_fixture"},
  data: meaningReview,
}

const setupClient = {
  getSetup: vi.fn(async () => setupEnvelope),
  getReview: vi.fn(async () => meaningEnvelope),
  getOperation: vi.fn(),
  confirmWarehouseBinding: vi.fn(),
  registerSource: vi.fn(),
  submitProcessPackage: vi.fn(),
  decideReview: vi.fn(),
}

beforeEach(() => {
  vi.clearAllMocks()
  setupClient.getReview.mockResolvedValue(meaningEnvelope)
})

test("renders typed meaning sections, unresolved questions, authorities, evidence, and exact digest", async () => {
  render(<SetupWorkbench client={setupClient} session={session} setupEnvelope={setupEnvelope} />)

  expect(await screen.findByRole("heading", {name: "Meaning approval"})).toBeVisible()
  expect(screen.getByRole("heading", {name: "Process model"})).toBeVisible()
  expect(screen.getByText("Does a disputed invoice count as collectible revenue?")).toBeVisible()
  expect(screen.getByText("Changing Customer identity invalidates downstream approvals.")).toBeVisible()
  const authorities = screen.getByRole("list", {name: "Required authorities"})
  expect(authorities).toHaveTextContent("Data architect")
  expect(authorities).toHaveTextContent("Data owner")
  expect(authorities).toHaveTextContent("c".repeat(64))
  expect(screen.getByText("evidence-openmetadata-roundtrip")).toBeVisible()
  expect(
    screen.getByRole("checkbox", {
      name: `I confirm the exact reviewed digest ${"b".repeat(64)}.`,
    }),
  ).toBeVisible()
})

test("waits for authoritative responses while recording every role held by the session", async () => {
  const user = userEvent.setup()
  const firstDecision = Promise.withResolvers<ConsoleEnvelopeReviewView>()
  const afterArchitect: ConsoleEnvelopeReviewView = {
    ...meaningEnvelope,
    data: {
      ...meaningReview,
      revision: 4,
      required_authorities: [
        {...meaningReview.required_authorities![0]!, satisfied: true},
        meaningReview.required_authorities![1]!,
      ],
      decisions: [
        {
          role: "data_architect",
          decision: "approve",
          decided_at: "2026-09-01T19:00:00Z",
        },
      ],
    },
  }
  const afterOwner: ConsoleEnvelopeReviewView = {
    ...meaningEnvelope,
    data: {
      ...afterArchitect.data,
      revision: 5,
      required_authorities: afterArchitect.data.required_authorities!.map((authority) => ({
        ...authority,
        satisfied: true,
      })),
      decisions: [
        ...afterArchitect.data.decisions!,
        {role: "data_owner", decision: "approve", decided_at: "2026-09-01T19:01:00Z"},
      ],
    },
  }
  setupClient.decideReview
    .mockImplementationOnce(() => firstDecision.promise)
    .mockResolvedValueOnce(afterOwner)
  const idempotencyKeyFactory = vi
    .fn<() => string>()
    .mockReturnValueOnce("idempotency-review-architect")
    .mockReturnValueOnce("idempotency-review-owner")

  render(
    <SetupWorkbench
      client={setupClient}
      idempotencyKeyFactory={idempotencyKeyFactory}
      session={session}
      setupEnvelope={setupEnvelope}
    />,
  )
  await user.click(
    await screen.findByRole("checkbox", {
      name: `I confirm the exact reviewed digest ${"b".repeat(64)}.`,
    }),
  )
  await user.click(screen.getByRole("button", {name: "Approve required roles"}))

  expect(setupClient.decideReview).toHaveBeenCalledTimes(1)
  expect(screen.getAllByText("Not recorded")).toHaveLength(2)
  expect(screen.queryByRole("heading", {name: "Recorded decisions"})).not.toBeInTheDocument()
  firstDecision.resolve(afterArchitect)

  await waitFor(() => expect(setupClient.decideReview).toHaveBeenCalledTimes(2))
  expect(setupClient.decideReview.mock.calls[0]).toEqual([
    "review-meaning",
    {
      expected_revision: 3,
      reviewed_digest: "b".repeat(64),
      active_role: "data_architect",
      decision: "approve",
    },
    {
      csrfToken: "csrf-token-with-at-least-thirty-two-characters",
      idempotencyKey: "idempotency-review-architect",
    },
  ])
  expect(setupClient.decideReview.mock.calls[1]).toEqual([
    "review-meaning",
    {
      expected_revision: 4,
      reviewed_digest: "b".repeat(64),
      active_role: "data_owner",
      decision: "approve",
    },
    {
      csrfToken: "csrf-token-with-at-least-thirty-two-characters",
      idempotencyKey: "idempotency-review-owner",
    },
  ])
  expect(await screen.findAllByText("Recorded")).toHaveLength(2)
})

test("refreshes a stale review while preserving only an unsubmitted local comment", async () => {
  const user = userEvent.setup()
  const refreshedEnvelope: ConsoleEnvelopeReviewView = {
    ...meaningEnvelope,
    data: {
      ...meaningReview,
      revision: 9,
      sections: [
        {
          section_id: "process-model",
          title: "Process model",
          summary: "Refreshed server projection.",
          items: [{label: "Owner", value: "Finance operations", material_change: false}],
        },
      ],
    },
  }
  setupClient.getReview
    .mockResolvedValueOnce(meaningEnvelope)
    .mockResolvedValueOnce(refreshedEnvelope)
  setupClient.decideReview.mockRejectedValueOnce(
    new ConsoleApiError(409, {
      meta: {correlation_id: "correlation-stale", data_provenance: "demo_fixture"},
      error: {
        code: "stale_revision",
        safe_message: "The review changed; reload before deciding.",
        recovery_action: "reload",
        field: "expected_revision",
      },
    }),
  )

  render(
    <SetupWorkbench
      client={setupClient}
      idempotencyKeyFactory={() => "idempotency-review-stale"}
      session={{...session, roles: ["data_architect"], active_role: "data_architect"}}
      setupEnvelope={setupEnvelope}
    />,
  )
  const comment = await screen.findByRole("textbox", {name: "Unsubmitted review comment"})
  await user.type(comment, "Keep this local note")
  await user.click(
    screen.getByRole("checkbox", {
      name: `I confirm the exact reviewed digest ${"b".repeat(64)}.`,
    }),
  )
  await user.click(screen.getByRole("button", {name: "Approve as Data architect"}))

  expect(await screen.findByText("Review refreshed; your unsubmitted comment was preserved.")).toBeVisible()
  expect(screen.getByRole("textbox", {name: "Unsubmitted review comment"})).toHaveValue(
    "Keep this local note",
  )
  expect(screen.getByText("Finance operations")).toBeVisible()
  expect(screen.getAllByText("Not recorded")).toHaveLength(2)
  expect(setupClient.getReview).toHaveBeenCalledTimes(2)
})

test("fails closed when a stale review cannot be refreshed", async () => {
  const user = userEvent.setup()
  setupClient.getReview
    .mockResolvedValueOnce(meaningEnvelope)
    .mockRejectedValueOnce(new TypeError("refresh transport unavailable"))
  setupClient.decideReview.mockRejectedValueOnce(
    new ConsoleApiError(409, {
      meta: {correlation_id: "correlation-stale-failed", data_provenance: "demo_fixture"},
      error: {
        code: "stale_revision",
        safe_message: "The review changed; reload before deciding.",
        recovery_action: "reload",
        field: "expected_revision",
      },
    }),
  )

  render(
    <SetupWorkbench
      client={setupClient}
      idempotencyKeyFactory={() => "idempotency-review-stale-failed"}
      session={{...session, roles: ["data_architect"], active_role: "data_architect"}}
      setupEnvelope={setupEnvelope}
    />,
  )
  await user.click(
    await screen.findByRole("checkbox", {
      name: `I confirm the exact reviewed digest ${"b".repeat(64)}.`,
    }),
  )
  await user.click(screen.getByRole("button", {name: "Approve as Data architect"}))

  expect(
    await screen.findByText("The refreshed review projection could not be reconciled safely."),
  ).toBeVisible()
})

test("attributes every No Valid Plan constraint and shows its permitted next action", async () => {
  const constrainedReview: ConsoleEnvelopeReviewView = {
    ...meaningEnvelope,
    data: {
      ...meaningReview,
      kind: "data_product",
      title: "Data-product approval",
      constraints: [
        {
          code: "no-valid-plan",
          summary: "No admitted provider pair can preserve the requested identity semantics.",
          responsible_role: "data_architect",
          permitted_next_action: "Revise the identity requirement and request a new plan.",
        },
      ],
    },
  }
  setupClient.getReview.mockResolvedValueOnce(constrainedReview)

  render(
    <SetupWorkbench
      client={setupClient}
      session={session}
      setupEnvelope={{
        ...setupEnvelope,
        data: {...setup, active_stage: "data_product"},
      }}
    />,
  )

  expect(await screen.findByRole("heading", {name: "No Valid Plan"})).toBeVisible()
  expect(screen.getByText("no-valid-plan")).toBeVisible()
  expect(screen.getByText("Responsible role: Data architect")).toBeVisible()
  expect(
    screen.getByText(
      "Permitted next action: Revise the identity requirement and request a new plan.",
    ),
  ).toBeVisible()
})
