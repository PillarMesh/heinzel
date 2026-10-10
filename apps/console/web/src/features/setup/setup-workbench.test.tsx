import {act, fireEvent, render, screen, waitFor} from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import {MemoryRouter} from "react-router-dom"
import {beforeEach, expect, test, vi} from "vitest"
import {featureClientStubs} from "../../test/client-stubs"

import type {
  ConsoleEnvelopeSessionView,
  ConsoleEnvelopeSetupView,
  ConsoleEnvelopeWorkspaceView,
  OperationView,
  ReviewView,
  SetupView,
} from "../../api/generated"
import {ConsoleMutationOutcomeUnknown} from "../../api/client"
import {ConsoleRoutes} from "../../routes/router"
import {SetupWorkbench} from "./setup-workbench"

const sessionEnvelope: ConsoleEnvelopeSessionView = {
  meta: {correlation_id: "correlation-session", data_provenance: "demo_fixture"},
  data: {
    actor: {display_name: "Dana Architect"},
    roles: ["data_architect", "data_owner", "budget_approver"],
    active_role: "data_architect",
    tenant: {ref: "tenant-demo", display_name: "Northwind Demo"},
    workspace: {ref: "workspace-revenue", display_name: "Revenue to cash"},
    csrf_token: "csrf-token-with-at-least-thirty-two-characters",
  },
}

const setup: SetupView = {
  workspace_ref: "workspace-revenue",
  revision: 4,
  setup_digest: "a".repeat(64),
  reset_token: "reset_token_fixture_sequence-0004",
  active_stage: "foundation",
  stages: [
    {stage: "foundation", label: "Foundation", state: "current", detail: "Choose an engine."},
    {stage: "managed_services", label: "Managed services", state: "blocked", detail: "Warehouse required."},
    {stage: "sources", label: "Sources", state: "not_started", detail: null},
    {stage: "business_process", label: "Business process", state: "not_started", detail: null},
    {stage: "meaning", label: "Meaning review", state: "not_started", detail: null},
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
    {
      engine: "clickhouse",
      label: "ClickHouse",
      supported_region: "us-west-2",
      fixed_capacity: "fixed-small",
    },
  ],
  pending_review_refs: ["review-meaning", "review-data-product", "review-activation"],
  managed_services: [],
  sources: [],
  warehouse_binding: null,
  process_package: null,
}

const setupEnvelope: ConsoleEnvelopeSetupView = {
  meta: {correlation_id: "correlation-setup", data_provenance: "demo_fixture"},
  data: setup,
}

const workspaceEnvelope: ConsoleEnvelopeWorkspaceView = {
  meta: {correlation_id: "correlation-workspace", data_provenance: "demo_fixture"},
  data: {
    workspace: {ref: "workspace-revenue", display_name: "Revenue to cash"},
    state: "setup",
    capabilities: [],
    recovery_message: null,
  },
}

const terminalOperation: OperationView = {
  operation_id: "operation-fixture",
  revision: 1,
  state: "succeeded",
  phase: "fixture",
  summary: "Fixture operation completed.",
  recovery_actions: [],
  evidence_ref: null,
  failure: null,
  operation_digest: null,
  retry_token: null,
}

const review: ReviewView = {
  review_id: "review-meaning",
  kind: "meaning",
  title: "Meaning approval",
  summary: "Review the extracted meaning.",
  revision: 1,
  reviewed_digest: "b".repeat(64),
  sections: [{section_id: "meaning-summary", title: "Meaning", summary: null, items: []}],
  required_authorities: [],
  decisions: [],
  constraints: [],
  evidence_refs: [],
  can_decide: true,
}

const setupClient = {
  ...featureClientStubs(),
  getSetup: vi.fn(async () => setupEnvelope),
  getReview: vi.fn(async () => ({meta: setupEnvelope.meta, data: review})),
  getOperation: vi.fn(async () => ({meta: setupEnvelope.meta, data: terminalOperation})),
  confirmWarehouseBinding: vi.fn(),
  registerSource: vi.fn(),
  submitProcessPackage: vi.fn(),
  decideReview: vi.fn(),
}

beforeEach(() => {
  vi.clearAllMocks()
})

function renderSetup(currentSetup: SetupView = setup) {
  return render(
    <MemoryRouter initialEntries={["/setup"]}>
      <ConsoleRoutes
        sessionEnvelope={sessionEnvelope}
        setupClient={setupClient}
        setupEnvelope={{...setupEnvelope, data: currentSetup}}
        workspaceEnvelope={workspaceEnvelope}
      />
    </MemoryRouter>,
  )
}

test("renders server-issued seven-stage progress and warehouse choices", () => {
  renderSetup()

  const progress = screen.getByRole("list", {name: "Setup stages"})
  expect(progress).toHaveTextContent("Foundation")
  expect(progress).toHaveTextContent("Managed services")
  expect(progress).toHaveTextContent("Sources")
  expect(progress).toHaveTextContent("Business process")
  expect(progress).toHaveTextContent("Meaning review")
  expect(progress).toHaveTextContent("Data-product review")
  expect(progress).toHaveTextContent("Activation")
  expect(screen.getByText("Warehouse required.")).toBeVisible()
  expect(screen.getByRole("radio", {name: /PostgreSQL/})).toBeVisible()
  expect(screen.getByRole("radio", {name: /ClickHouse/})).toBeVisible()
})

test("confirms an immutable server option and presents acceptance without claiming success", async () => {
  const user = userEvent.setup()
  setupClient.confirmWarehouseBinding.mockResolvedValueOnce({
    envelope: {
      meta: setupEnvelope.meta,
      data: {
        ...terminalOperation,
        operation_id: "operation-warehouse",
        state: "accepted",
        phase: "warehouse_provisioning",
        summary: "Warehouse provisioning was accepted.",
      },
    },
    kind: "pending",
    state: "accepted",
    status: 202,
  })

  render(
    <SetupWorkbench
      client={setupClient}
      idempotencyKeyFactory={() => "idempotency-warehouse-0001"}
      session={sessionEnvelope.data}
      setupEnvelope={setupEnvelope}
    />,
  )

  await user.click(screen.getByRole("radio", {name: /ClickHouse/}))
  expect(screen.getByRole("button", {name: "Confirm warehouse binding"})).toBeDisabled()
  await user.click(
    screen.getByRole("checkbox", {
      name: "I understand that this warehouse binding is immutable after confirmation.",
    }),
  )
  await user.click(screen.getByRole("button", {name: "Confirm warehouse binding"}))

  expect(setupClient.confirmWarehouseBinding).toHaveBeenCalledWith(
    {
      expected_revision: 4,
      reviewed_digest: "a".repeat(64),
      active_role: "data_architect",
      engine: "clickhouse",
      region: "us-west-2",
      capacity: "fixed-small",
    },
    {
      csrfToken: "csrf-token-with-at-least-thirty-two-characters",
      idempotencyKey: "idempotency-warehouse-0001",
    },
  )
  expect(await screen.findByRole("status", {name: "Warehouse operation"})).toHaveTextContent(
    "Request accepted",
  )
  expect(screen.queryByText(/Provisioning succeeded/i)).not.toBeInTheDocument()
})

test("prevents an engine change after the server issues an immutable binding", () => {
  render(
    <SetupWorkbench
      client={setupClient}
      session={sessionEnvelope.data}
      setupEnvelope={{
        ...setupEnvelope,
        data: {
          ...setup,
          warehouse_binding: {
            binding_ref: "binding-warehouse",
            engine: "postgresql",
            region: "us-west-2",
            capacity: "fixed-small",
            state: "ready",
            immutable: true,
          },
        },
      }}
    />,
  )

  expect(screen.getByRole("radio", {name: /PostgreSQL/})).toBeChecked()
  expect(screen.getByRole("radio", {name: /ClickHouse/})).toBeDisabled()
  expect(
    screen.getByText("The PostgreSQL warehouse binding is immutable. Engine changes are unavailable."),
  ).toBeVisible()
  expect(screen.queryByRole("button", {name: "Confirm warehouse binding"})).not.toBeInTheDocument()
})

test("keeps a not-delivered Superset capability disabled with its supplied dependency", () => {
  render(
    <SetupWorkbench
      client={setupClient}
      session={sessionEnvelope.data}
      setupEnvelope={{
        ...setupEnvelope,
        data: {
          ...setup,
          active_stage: "managed_services",
          managed_services: [
            {
              service: "warehouse",
              label: "Managed warehouse",
              state: "ready",
              detail: "Warehouse validation completed.",
              validation_summary: "Backups and monitoring are enabled.",
            },
            {
              service: "openmetadata",
              label: "OpenMetadata",
              state: "ready",
              detail: "Catalog service is ready.",
              validation_summary: "Round-trip validation completed.",
            },
            {
              service: "superset",
              label: "Superset",
              state: "not_delivered",
              detail: "Depends on managed Superset delivery.",
              validation_summary: null,
            },
          ],
        },
      }}
    />,
  )

  expect(screen.getByRole("heading", {name: "Managed services"})).toBeVisible()
  expect(screen.getByText("Depends on managed Superset delivery.")).toBeVisible()
  expect(screen.getByRole("button", {name: "Open dashboards"})).toBeDisabled()
  expect(screen.queryByRole("link", {name: "Open dashboards"})).not.toBeInTheDocument()
})

test("renders typed source intents with intended and denial probes", () => {
  render(
    <SetupWorkbench
      client={setupClient}
      session={sessionEnvelope.data}
      setupEnvelope={{
        ...setupEnvelope,
        data: {
          ...setup,
          active_stage: "sources",
          sources: [
            {
              source_ref: "source-orders",
              source_type: "postgresql",
              display_name: "Orders PostgreSQL",
              state: "ready",
              intended_checks: ["Read approved order tables"],
              denied_checks: ["Cannot alter source schemas"],
            },
            {
              source_ref: "source-stripe",
              source_type: "stripe",
              display_name: "Stripe billing",
              state: "blocked",
              intended_checks: ["Read balance transactions"],
              denied_checks: ["Cannot create refunds"],
            },
          ],
        },
      }}
    />,
  )

  expect(screen.getByRole("heading", {name: "Registered sources"})).toBeVisible()
  expect(screen.getByRole("article", {name: "Orders PostgreSQL"})).toHaveTextContent(
    "Read approved order tables",
  )
  expect(screen.getByRole("article", {name: "Orders PostgreSQL"})).toHaveTextContent(
    "Cannot alter source schemas",
  )
  expect(screen.getByRole("article", {name: "Stripe billing"})).toHaveTextContent(
    "Cannot create refunds",
  )
})

test("polls a pending operation with the exact capped sequence until terminal success", async () => {
  const user = userEvent.setup()
  const scheduledCallbacks: Array<() => void> = []
  const pollTimer = {
    clear: vi.fn(),
    set: vi.fn((callback: () => void, delay: number) => {
      void delay
      scheduledCallbacks.push(callback)
      return scheduledCallbacks.length
    }),
  }
  setupClient.confirmWarehouseBinding.mockResolvedValueOnce({
    envelope: {
      meta: setupEnvelope.meta,
      data: {
        ...terminalOperation,
        operation_id: "operation-warehouse",
        state: "accepted",
      },
    },
    kind: "pending",
    state: "accepted",
    status: 202,
  })
  setupClient.getOperation
    .mockResolvedValueOnce({
      meta: setupEnvelope.meta,
      data: {...terminalOperation, operation_id: "operation-warehouse", revision: 2, state: "running"},
    })
    .mockResolvedValueOnce({
      meta: setupEnvelope.meta,
      data: {...terminalOperation, operation_id: "operation-warehouse", revision: 3, state: "running"},
    })
    .mockResolvedValueOnce({
      meta: setupEnvelope.meta,
      data: {...terminalOperation, operation_id: "operation-warehouse", revision: 4, state: "outcome_unknown"},
    })
    .mockResolvedValueOnce({
      meta: setupEnvelope.meta,
      data: {...terminalOperation, operation_id: "operation-warehouse", revision: 5, state: "running"},
    })
    .mockResolvedValueOnce({
      meta: setupEnvelope.meta,
      data: {...terminalOperation, operation_id: "operation-warehouse", revision: 6, state: "running"},
    })
    .mockResolvedValueOnce({
      meta: setupEnvelope.meta,
      data: {...terminalOperation, operation_id: "operation-warehouse", revision: 7, state: "succeeded"},
    })

  render(
    <SetupWorkbench
      client={setupClient}
      idempotencyKeyFactory={() => "idempotency-warehouse-poll"}
      pollTimer={pollTimer}
      session={sessionEnvelope.data}
      setupEnvelope={setupEnvelope}
    />,
  )
  await user.click(
    screen.getByRole("checkbox", {
      name: "I understand that this warehouse binding is immutable after confirmation.",
    }),
  )
  await user.click(screen.getByRole("button", {name: "Confirm warehouse binding"}))

  await waitFor(() => expect(pollTimer.set).toHaveBeenCalledTimes(1))
  for (let index = 0; index < 6; index += 1) {
    await act(async () => scheduledCallbacks[index]!())
    await waitFor(() => expect(setupClient.getOperation).toHaveBeenCalledTimes(index + 1))
  }

  expect(pollTimer.set.mock.calls.map((call) => call[1])).toEqual([
    1_000,
    2_000,
    4_000,
    8_000,
    15_000,
    15_000,
  ])
  expect(setupClient.getOperation).toHaveBeenCalledTimes(6)
  expect(setupClient.getOperation).toHaveBeenCalledWith("operation-warehouse")
  expect(screen.getByRole("status", {name: "Warehouse operation"})).toHaveTextContent(
    "Completed",
  )
  expect(screen.getByText("operation-warehouse")).not.toBeVisible()
  await user.click(screen.getByText("Technical details"))
  expect(screen.getByText("operation-warehouse")).toBeVisible()
})

test("cancels a pending operation poll when the workbench unmounts", async () => {
  const user = userEvent.setup()
  let scheduledCallback: (() => void) | undefined
  const pollTimer = {
    clear: vi.fn(),
    set: vi.fn((callback: () => void, delay: number) => {
      void delay
      scheduledCallback = callback
      return 41
    }),
  }
  setupClient.confirmWarehouseBinding.mockResolvedValueOnce({
    envelope: {
      meta: setupEnvelope.meta,
      data: {...terminalOperation, operation_id: "operation-unmount", state: "accepted"},
    },
    kind: "pending",
    state: "accepted",
    status: 202,
  })
  const view = render(
    <SetupWorkbench
      client={setupClient}
      idempotencyKeyFactory={() => "idempotency-warehouse-unmount"}
      pollTimer={pollTimer}
      session={sessionEnvelope.data}
      setupEnvelope={setupEnvelope}
    />,
  )
  await user.click(
    screen.getByRole("checkbox", {
      name: "I understand that this warehouse binding is immutable after confirmation.",
    }),
  )
  await user.click(screen.getByRole("button", {name: "Confirm warehouse binding"}))
  await waitFor(() => expect(pollTimer.set).toHaveBeenCalledTimes(1))

  view.unmount()
  await act(async () => scheduledCallback?.())

  expect(pollTimer.clear).toHaveBeenCalledWith(41)
  expect(setupClient.getOperation).not.toHaveBeenCalled()
})

test("reconciles an ambiguous confirmation with the original idempotency key", async () => {
  const user = userEvent.setup()
  const idempotencyKeyFactory = vi.fn(() => "idempotency-warehouse-ambiguous")
  setupClient.confirmWarehouseBinding
    .mockRejectedValueOnce(
      new ConsoleMutationOutcomeUnknown(
        "operation_response",
        "idempotency-warehouse-ambiguous",
        null,
        null,
      ),
    )
    .mockResolvedValueOnce({
      envelope: {
        meta: setupEnvelope.meta,
        data: {...terminalOperation, operation_id: "operation-reconciled", state: "accepted"},
      },
      kind: "pending",
      state: "accepted",
      status: 202,
    })

  render(
    <SetupWorkbench
      client={setupClient}
      idempotencyKeyFactory={idempotencyKeyFactory}
      session={sessionEnvelope.data}
      setupEnvelope={setupEnvelope}
    />,
  )
  await user.click(
    screen.getByRole("checkbox", {
      name: "I understand that this warehouse binding is immutable after confirmation.",
    }),
  )
  await user.click(screen.getByRole("button", {name: "Confirm warehouse binding"}))
  expect(await screen.findByText("The confirmation outcome is unknown.")).toBeVisible()

  await user.click(screen.getByRole("button", {name: "Reconcile confirmation"}))

  expect(setupClient.confirmWarehouseBinding).toHaveBeenCalledTimes(2)
  expect(setupClient.confirmWarehouseBinding.mock.calls[1]).toEqual(
    setupClient.confirmWarehouseBinding.mock.calls[0],
  )
  expect(idempotencyKeyFactory).toHaveBeenCalledTimes(1)
  expect(await screen.findByText("operation-reconciled")).not.toBeVisible()
})

test("accepts only nonempty Markdown narratives and explains the strict JSON manifest", async () => {
  const user = userEvent.setup({applyAccept: false})
  render(
    <SetupWorkbench
      client={setupClient}
      session={sessionEnvelope.data}
      setupEnvelope={{...setupEnvelope, data: {...setup, active_stage: "business_process"}}}
    />,
  )

  const input = screen.getByLabelText("Process package")
  expect(
    screen.getByText("Accepted narrative: UTF-8 Markdown (.md). Add the matching JSON manifest below."),
  ).toBeVisible()
  expect(screen.queryByText(/maximum|max file size/i)).not.toBeInTheDocument()

  await user.upload(input, new File(["plain text"], "process.txt", {type: "text/plain"}))
  expect(screen.getByRole("alert")).toHaveTextContent("Choose a Markdown (.md) file.")

  await user.upload(input, new File([], "empty.md", {type: "text/markdown"}))
  expect(screen.getByRole("alert")).toHaveTextContent("Choose a non-empty Markdown file.")

  await user.upload(input, new File(["# Revenue to cash"], "process.md", {type: "text/markdown"}))

  fireEvent.change(screen.getByLabelText("Business process manifest (JSON)"), {
    target: {
      value: JSON.stringify({
        process_name: "Revenue to cash",
        owner: "Finance operations",
        participants: [],
        outcomes: [],
        entities: [],
        events: [],
        states: [],
        rules: [],
        source_references: [],
        unresolved_questions: [],
        invented_authority: true,
      }),
    },
  })
  expect(screen.getByRole("alert")).toHaveTextContent(
    "manifest.invented_authority is not allowed.",
  )
  expect(setupClient.submitProcessPackage).not.toHaveBeenCalled()
})

test("lets an architect reopen a completed business process from setup progress", async () => {
  const user = userEvent.setup()
  render(
    <SetupWorkbench
      client={setupClient}
      session={sessionEnvelope.data}
      setupEnvelope={{
        ...setupEnvelope,
        data: {
          ...setup,
          active_stage: "sources",
          stages: [
            setup.stages[0],
            ...setup.stages.slice(1).map((stage) =>
              stage.stage === "business_process" ? {...stage, state: "complete" as const} : stage,
            ),
          ],
          process_package: {
            package_ref: "bpp-current-process",
            version: 1,
            content_digest: "a".repeat(64),
            state: "ready",
            candidate_summary: "Revenue to cash",
          },
        },
      }}
    />,
  )

  await user.click(screen.getByRole("button", {name: /Business process/}))

  expect(screen.getByRole("heading", {name: "Current process package"})).toBeVisible()
  expect(screen.getByText("Revenue to cash")).toBeVisible()
})

test("submits the exact Markdown narrative and strict manifest with its SHA-256", async () => {
  const user = userEvent.setup()
  const expectedDigest = "52c3935626c104b2cbc9031291a1c4d56614c38f52072a361d658a58a9c48698"
  setupClient.submitProcessPackage.mockResolvedValueOnce({
    envelope: {
      meta: setupEnvelope.meta,
      data: {...terminalOperation, operation_id: "operation-process", state: "accepted"},
    },
    kind: "pending",
    state: "accepted",
    status: 202,
  })
  render(
    <SetupWorkbench
      client={setupClient}
      idempotencyKeyFactory={() => "idempotency-process-0001"}
      session={sessionEnvelope.data}
      setupEnvelope={{...setupEnvelope, data: {...setup, active_stage: "business_process"}}}
    />,
  )

  await user.upload(
    screen.getByLabelText("Process package"),
    new File(["original bytes"], "revenue-process.md", {type: "text/markdown"}),
  )
  const manifest = {
    process_name: "Revenue to cash",
    owner: "Finance operations",
    participants: ["Billing", "Finance"],
    outcomes: ["Settled invoice"],
    entities: ["Invoice"],
    events: ["Invoice settled"],
    states: ["settled"],
    rules: ["Only settled invoices close"],
    source_references: ["billing-postgresql"],
    unresolved_questions: [],
  }
  fireEvent.change(screen.getByLabelText("Business process manifest (JSON)"), {
    target: {value: JSON.stringify(manifest)},
  })

  expect(await screen.findByText(expectedDigest)).toBeVisible()
  expect(
    screen.getByText(
      "Saving changes creates a new process version. Approvals based on earlier content must be reviewed again.",
    ),
  ).toBeVisible()
  await user.click(screen.getByRole("button", {name: "Submit process package"}))

  expect(setupClient.submitProcessPackage).toHaveBeenCalledWith(
    {
      expected_revision: 4,
      package_digest: expectedDigest,
      active_role: "data_architect",
      file_name: "revenue-process.md",
      media_type: "text/markdown; charset=utf-8",
      narrative_markdown: "original bytes",
      manifest,
    },
    {
      csrfToken: "csrf-token-with-at-least-thirty-two-characters",
      idempotencyKey: "idempotency-process-0001",
    },
  )
})

test("reconciles an ambiguous process submission with the original command and key", async () => {
  const user = userEvent.setup()
  const idempotencyKeyFactory = vi.fn(() => "idempotency-process-ambiguous")
  setupClient.submitProcessPackage
    .mockRejectedValueOnce(
      new ConsoleMutationOutcomeUnknown(
        "operation_response",
        "idempotency-process-ambiguous",
        null,
        null,
      ),
    )
    .mockResolvedValueOnce({
      envelope: {
        meta: setupEnvelope.meta,
        data: {...terminalOperation, operation_id: "operation-process-reconciled", state: "accepted"},
      },
      kind: "pending",
      state: "accepted",
      status: 202,
    })

  render(
    <SetupWorkbench
      client={setupClient}
      digestFile={async () => "e".repeat(64)}
      idempotencyKeyFactory={idempotencyKeyFactory}
      session={sessionEnvelope.data}
      setupEnvelope={{...setupEnvelope, data: {...setup, active_stage: "business_process"}}}
    />,
  )
  await user.upload(
    screen.getByLabelText("Process package"),
    new File(["process"], "process.md", {type: "text/markdown"}),
  )
  fireEvent.change(screen.getByLabelText("Business process manifest (JSON)"), {
    target: {
      value: JSON.stringify({
        process_name: "Process",
        owner: "Operations",
        participants: [],
        outcomes: [],
        entities: [],
        events: [],
        states: [],
        rules: [],
        source_references: [],
        unresolved_questions: [],
      }),
    },
  })
  await user.click(await screen.findByRole("button", {name: "Submit process package"}))

  expect(await screen.findByText("The process submission outcome is unknown.")).toBeVisible()
  await user.click(screen.getByRole("button", {name: "Reconcile process submission"}))

  expect(setupClient.submitProcessPackage).toHaveBeenCalledTimes(2)
  expect(setupClient.submitProcessPackage.mock.calls[1]).toEqual(
    setupClient.submitProcessPackage.mock.calls[0],
  )
  expect(idempotencyKeyFactory).toHaveBeenCalledTimes(1)
  expect(await screen.findByText("Fixture operation completed.")).toBeVisible()
  expect(screen.getByText("operation-process-reconciled")).not.toBeVisible()
})

test("asks the shell to re-read its projections once provisioning settles", async () => {
  // The command changes the workspace, the governance spine and the stage list, and
  // none of them are this component's state. Without this the page kept offering the
  // engine choice and kept showing "Managed warehouse: Blocked" beside its own
  // "Completed", which reads as a failure rather than a stale view.
  const user = userEvent.setup()
  const onProjectionsChanged = vi.fn()
  setupClient.confirmWarehouseBinding.mockResolvedValueOnce({
    envelope: {
      meta: setupEnvelope.meta,
      data: {
        ...terminalOperation,
        operation_id: "operation-warehouse-settled",
        state: "succeeded",
        phase: "ready",
        summary: "The managed warehouse binding is ready.",
      },
    },
    kind: "terminal",
    state: "succeeded",
    status: 200,
  })

  render(
    <SetupWorkbench
      client={setupClient}
      idempotencyKeyFactory={() => "idempotency-warehouse-0002"}
      onProjectionsChanged={onProjectionsChanged}
      session={sessionEnvelope.data}
      setupEnvelope={setupEnvelope}
    />,
  )

  await user.click(
    screen.getByRole("checkbox", {
      name: "I understand that this warehouse binding is immutable after confirmation.",
    }),
  )
  await user.click(screen.getByRole("button", {name: "Confirm warehouse binding"}))

  await waitFor(() => expect(onProjectionsChanged).toHaveBeenCalled())
})

test("keeps the stage an architect opened when the projection changes underneath it", async () => {
  /**
   * Acting in a stage changes the projection: the digest moves and `active_stage` moves with it.
   * Dropping the selection then threw the architect out of the stage they had just acted in,
   * replacing the confirmation they were reading with a different stage's panel -- which was
   * reachable for the first time when source registration went live, because registering is the
   * first command that completes the stage it is issued from.
   */
  const user = userEvent.setup()
  // Rebuilt rather than mapped: the projection types the stage list as a non-empty tuple, and
  // a mapped array is not one.
  const [foundation, ...rest] = setup.stages
  const reachable: SetupView = {
    ...setup,
    stages: [
      foundation,
      ...rest.map((stage) =>
        stage.stage === "sources" ? {...stage, state: "current" as const} : stage,
      ),
    ],
  }
  const {rerender} = render(
    <SetupWorkbench
      client={setupClient}
      session={sessionEnvelope.data}
      setupEnvelope={{...setupEnvelope, data: reachable}}
    />,
  )

  await user.click(screen.getByRole("button", {name: /Sources/}))
  expect(screen.getByRole("heading", {level: 1, name: "Registered sources"})).toBeVisible()

  rerender(
    <SetupWorkbench
      client={setupClient}
      session={sessionEnvelope.data}
      setupEnvelope={{
        ...setupEnvelope,
        data: {...reachable, setup_digest: "b".repeat(64), active_stage: "business_process"},
      }}
    />,
  )

  expect(screen.getByRole("heading", {level: 1, name: "Registered sources"})).toBeVisible()
})

test("re-reads the stage after a process submission settles, and stays in it", async () => {
  /**
   * The stage took no `onProjectionsChanged` at all, so a submission that reached the server
   * left the page showing no current package and offering the same submission again. It was
   * unreachable while the capability was undelivered -- the control refused every press -- and
   * became reachable the moment contract-service's process package service was composed.
   */
  const user = userEvent.setup()
  setupClient.submitProcessPackage.mockResolvedValue({
    envelope: {
      meta: setupEnvelope.meta,
      data: {
        operation_id: "bpp-0123456789abcdef01234567",
        revision: 1,
        state: "succeeded",
        phase: "process_package_recorded",
        summary: "Business process package saved.",
        recovery_actions: [],
        evidence_ref: null,
        failure: null,
        operation_digest: null,
        retry_token: null,
      },
    },
    kind: "settled",
    state: "succeeded",
    status: 200,
  })
  const reRead = vi.fn()

  render(
    <SetupWorkbench
      client={setupClient}
      idempotencyKeyFactory={() => "idempotency-process-settles"}
      onProjectionsChanged={reRead}
      session={sessionEnvelope.data}
      setupEnvelope={{...setupEnvelope, data: {...setup, active_stage: "business_process"}}}
    />,
  )

  await user.upload(
    screen.getByLabelText("Process package"),
    new File(["original bytes"], "revenue-process.md", {type: "text/markdown"}),
  )
  fireEvent.change(screen.getByLabelText("Business process manifest (JSON)"), {
    target: {
      value: JSON.stringify({
        process_name: "Revenue to cash",
        owner: "Finance operations",
        participants: ["Billing"],
        outcomes: ["Settled invoice"],
        entities: ["Invoice"],
        events: ["Invoice settled"],
        states: ["settled"],
        rules: ["Only settled invoices close"],
        source_references: ["billing-postgresql"],
        unresolved_questions: [],
      }),
    },
  })
  await user.click(await screen.findByRole("button", {name: "Submit process package"}))

  await waitFor(() => expect(reRead).toHaveBeenCalledTimes(1))
  // And the stage the submission was made in is still the one on screen, rather than whichever
  // one the refreshed projection calls current.
  expect(screen.getByRole("heading", {level: 1, name: "Describe the business process"})).toBeVisible()
})
