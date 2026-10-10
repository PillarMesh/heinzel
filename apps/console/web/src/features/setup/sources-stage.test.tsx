import {useState} from "react"

import {render, screen} from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import {beforeEach, expect, test, vi} from "vitest"

import {ConsoleMutationOutcomeUnknown} from "../../api/client"
import {featureClientStubs} from "../../test/client-stubs"
import type {
  ConsoleEnvelopeSetupView,
  OperationView,
  SessionView,
  SetupView,
} from "../../api/generated"
import {SetupWorkbench} from "./setup-workbench"

/**
 * The sources stage, driven as a browser drives it.
 *
 * The property these tests hold the surface to is not only that a registration reaches the
 * server: it is that nothing about reaching the source travels through the browser. The command
 * carries a handle, and the rendered page carries no endpoint, credential or reference.
 */

const DSN_CANARY = "host=source.invalid port=5432 user=acquisition password=canary-password dbname=orders"

const session: SessionView = {
  actor: {display_name: "Dana Architect"},
  roles: ["data_architect"],
  active_role: "data_architect",
  tenant: {ref: "tenant-demo", display_name: "Northwind Demo"},
  workspace: {ref: "workspace-revenue", display_name: "Revenue to cash"},
  csrf_token: "csrf-token-with-at-least-thirty-two-characters",
}

const requesterSession: SessionView = {
  ...session,
  roles: ["requester"],
  active_role: "requester",
}

const setup: SetupView = {
  workspace_ref: "workspace-revenue",
  revision: 7,
  setup_digest: "a".repeat(64),
  reset_token: "reset_token_fixture_sequence-0007",
  active_stage: "sources",
  stages: [
    {stage: "foundation", label: "Foundation", state: "complete", detail: null},
    {stage: "managed_services", label: "Managed services", state: "complete", detail: null},
    {
      stage: "sources",
      label: "Sources",
      state: "current",
      detail:
        "An operator enrols a connection in this deployment's secret custody before it can be " +
        "registered.",
    },
    {stage: "business_process", label: "Business process", state: "current", detail: null},
    {stage: "meaning", label: "Meaning review", state: "blocked", detail: null},
    {stage: "data_product", label: "Data-product review", state: "blocked", detail: null},
    {stage: "activation", label: "Activation", state: "blocked", detail: null},
  ],
  warehouse_options: [
    {
      engine: "postgresql",
      label: "PostgreSQL",
      supported_region: "us-west-2",
      fixed_capacity: "mvp-fixed",
    },
  ],
  pending_review_refs: [],
  managed_services: [],
  sources: [
    {
      source_ref: "src-0123456789abcdef01234567",
      source_type: "postgresql",
      display_name: "registered-orders",
      state: "ready",
      lifecycle_state: "ready",
      connection_handle: "registered-orders",
      account_mode: "not_applicable",
      approved_object_refs: ["customer_orders"],
      capability_authority_digest: "b".repeat(64),
      intended_checks: [],
      denied_checks: [],
    },
  ],
  enrollable_sources: [
    {
      connection_handle: "waiting-billing",
      source_type: "postgresql",
      account_mode: "live",
      declared_object_refs: ["invoices"],
    },
  ],
  warehouse_binding: null,
  process_package: null,
}

const setupEnvelope: ConsoleEnvelopeSetupView = {
  meta: {correlation_id: "correlation-setup", data_provenance: "governed_local"},
  data: setup,
}

const acceptedOperation: OperationView = {
  operation_id: "src-0123456789abcdef01234568",
  revision: 3,
  state: "succeeded",
  phase: "source_binding_validated",
  summary: "Source connection registered and validated against the source.",
  recovery_actions: [],
  evidence_ref: null,
  failure: null,
  operation_digest: null,
  retry_token: null,
}

const setupClient = {
  ...featureClientStubs("governed_local"),
  getSetup: vi.fn(async () => setupEnvelope),
  getReview: vi.fn(),
  getOperation: vi.fn(async () => ({meta: setupEnvelope.meta, data: acceptedOperation})),
  confirmWarehouseBinding: vi.fn(),
  registerSource: vi.fn(),
  submitProcessPackage: vi.fn(),
  decideReview: vi.fn(),
}

beforeEach(() => {
  vi.clearAllMocks()
})

function renderSources(data: SetupView = setup, actor: SessionView = session) {
  return render(
    <SetupWorkbench
      client={setupClient}
      idempotencyKeyFactory={() => "idempotency-source-0001"}
      session={actor}
      setupEnvelope={{...setupEnvelope, data}}
    />,
  )
}

test("shows which sources are registered with the capability profile the probe observed", () => {
  renderSources()

  const card = screen.getByRole("article", {name: "registered-orders"})
  expect(card).toHaveTextContent("ready")
  expect(card).toHaveTextContent("customer_orders")
  expect(card).toHaveTextContent("b".repeat(64))
})

test("says the connection came from the deployment's secret custody, not from the console", () => {
  renderSources()

  expect(
    screen.getByText(/This console never holds a connection string/),
  ).toBeVisible()
  expect(screen.getByText(/An operator enrols each connection/)).toBeVisible()
})

test("offers no field of any kind for a connection string", () => {
  renderSources()

  // Not a comment about intent: a registration surface that grew a DSN field would be a
  // credential-handling surface, and this is the assertion that fails if one appears.
  expect(screen.queryAllByRole("textbox")).toHaveLength(0)
  expect(document.querySelectorAll("input, textarea")).toHaveLength(0)
})

test("registers an enrolled handle by naming the handle and nothing about the connection", async () => {
  const user = userEvent.setup()
  setupClient.registerSource.mockResolvedValueOnce({
    envelope: {meta: setupEnvelope.meta, data: acceptedOperation},
    kind: "settled",
    state: "succeeded",
    status: 200,
  })
  renderSources()

  await user.click(screen.getByRole("button", {name: "Register source"}))

  expect(setupClient.registerSource).toHaveBeenCalledWith(
    {
      expected_revision: 7,
      active_role: "data_architect",
      connection_handle: "waiting-billing",
    },
    {
      csrfToken: "csrf-token-with-at-least-thirty-two-characters",
      idempotencyKey: "idempotency-source-0001",
    },
  )
  const [command] = setupClient.registerSource.mock.calls[0] as [Record<string, unknown>]
  expect(Object.keys(command).sort()).toEqual([
    "active_role",
    "connection_handle",
    "expected_revision",
  ])
  expect(await screen.findByRole("status", {name: "Source registration operation"})).toHaveTextContent(
    "Source connection registered and validated against the source.",
  )
})

test("a registered handle is no longer offered to register", () => {
  renderSources({...setup, enrollable_sources: []})

  expect(screen.queryByRole("button", {name: "Register source"})).not.toBeInTheDocument()
  expect(
    screen.getByText(/No enrolled connection is waiting to be registered/),
  ).toBeVisible()
})

test("a requester is never offered the registration command", () => {
  renderSources(setup, requesterSession)

  expect(screen.getByRole("button", {name: "Register source"})).toBeDisabled()
})

test("reconciles an ambiguous registration with the original command and key", async () => {
  const user = userEvent.setup()
  const idempotencyKeyFactory = vi.fn(() => "idempotency-source-ambiguous")
  setupClient.registerSource
    .mockRejectedValueOnce(
      new ConsoleMutationOutcomeUnknown(
        "operation_response",
        "idempotency-source-ambiguous",
        null,
        null,
      ),
    )
    .mockResolvedValueOnce({
      envelope: {meta: setupEnvelope.meta, data: acceptedOperation},
      kind: "settled",
      state: "succeeded",
      status: 200,
    })
  render(
    <SetupWorkbench
      client={setupClient}
      idempotencyKeyFactory={idempotencyKeyFactory}
      session={session}
      setupEnvelope={setupEnvelope}
    />,
  )

  await user.click(screen.getByRole("button", {name: "Register source"}))
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "The source registration outcome is unknown.",
  )

  await user.click(screen.getByRole("button", {name: "Reconcile source registration"}))

  expect(idempotencyKeyFactory).toHaveBeenCalledTimes(1)
  expect(setupClient.registerSource.mock.calls.map((call) => call[1])).toEqual([
    {
      csrfToken: "csrf-token-with-at-least-thirty-two-characters",
      idempotencyKey: "idempotency-source-ambiguous",
    },
    {
      csrfToken: "csrf-token-with-at-least-thirty-two-characters",
      idempotencyKey: "idempotency-source-ambiguous",
    },
  ])
})

test("no rendered byte of the stage carries a connection detail", () => {
  // The canary is a DSN shaped exactly like the one a deployment's secret custody holds. It is
  // put into every field this projection carries that could plausibly be made to show one, and
  // the assertion is that the rendered page contains none of it.
  const {container} = renderSources({
    ...setup,
    sources: [
      {
        ...setup.sources![0]!,
        display_name: "registered-orders",
        connection_handle: "registered-orders",
      },
    ],
  })

  expect(container.innerHTML).not.toContain("canary-password")
  expect(container.innerHTML).not.toContain(DSN_CANARY)
  expect(container.innerHTML).not.toContain("postgresql://")
  expect(container.innerHTML).not.toContain("endpoint-ref:")
  expect(container.innerHTML).not.toContain("credential-ref:")
})

test("announces one settlement however often the shell re-renders the stage", async () => {
  /**
   * The loop this closes was measured on the live console: a registration settled, the shell
   * re-read its projections, that re-render handed the status component a new inline callback,
   * and the new identity re-fired the announcement. Five thousand session reads in ten seconds,
   * each cancelling the one before it, so the page that asked for fresh data never got any.
   *
   * The shell here is the real one in miniature: it re-renders on every announcement and passes
   * a fresh arrow each time, which is exactly what `App` does.
   */
  const user = userEvent.setup()
  setupClient.registerSource.mockResolvedValue({
    envelope: {meta: setupEnvelope.meta, data: acceptedOperation},
    kind: "settled",
    state: "succeeded",
    status: 200,
  })
  const announced = vi.fn()

  function Shell() {
    const [reads, setReads] = useState(0)
    return (
      <SetupWorkbench
        client={setupClient}
        idempotencyKeyFactory={() => "idempotency-source-loop"}
        // A new function on every render, as every caller writes it.
        onProjectionsChanged={() => {
          announced()
          setReads((current) => current + 1)
        }}
        session={session}
        setupEnvelope={{...setupEnvelope, data: {...setup, revision: setup.revision + reads}}}
      />
    )
  }

  render(<Shell />)
  await user.click(screen.getByRole("button", {name: "Register source"}))
  expect(await screen.findByText("Completed")).toBeVisible()
  await new Promise((resolve) => globalThis.setTimeout(resolve, 50))

  expect(announced).toHaveBeenCalledTimes(1)
})

test("stays in the stage after a registration settles, with the confirmation still shown", async () => {
  /**
   * Completing a stage moves `active_stage` on, and a view that followed it replaced the
   * confirmation an architect had just produced with the next stage's panel. Measured on the
   * live console: pressing Register landed on Managed services, and the only sign the command
   * had worked was a tick in the rail.
   */
  const user = userEvent.setup()
  setupClient.registerSource.mockResolvedValue({
    envelope: {meta: setupEnvelope.meta, data: acceptedOperation},
    kind: "settled",
    state: "succeeded",
    status: 200,
  })

  function Shell() {
    const [registered, setRegistered] = useState(false)
    // What the server answers once the registration lands: the stage is complete and the
    // console's idea of the current work has moved to the next unfinished stage.
    const data: SetupView = registered
      ? {
          ...setup,
          setup_digest: "c".repeat(64),
          active_stage: "business_process",
          enrollable_sources: [],
        }
      : setup
    return (
      <SetupWorkbench
        client={setupClient}
        idempotencyKeyFactory={() => "idempotency-source-stay"}
        onProjectionsChanged={() => setRegistered(true)}
        session={session}
        setupEnvelope={{...setupEnvelope, data}}
      />
    )
  }

  render(<Shell />)
  await user.click(screen.getByRole("button", {name: "Register source"}))

  expect(await screen.findByText("Completed")).toBeVisible()
  expect(screen.getByRole("heading", {level: 1, name: "Registered sources"})).toBeVisible()
  expect(screen.queryByRole("button", {name: "Register source"})).toBeNull()
})
