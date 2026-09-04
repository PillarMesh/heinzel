import {render, screen, waitFor} from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import {MemoryRouter} from "react-router-dom"
import {afterEach, describe, expect, test, vi} from "vitest"
import {featureClientStubs} from "../test/client-stubs"

import type {
  ConsoleEnvelopeReviewView,
  ConsoleEnvelopeSessionView,
  ConsoleEnvelopeSetupView,
  ConsoleEnvelopeWorkspaceView,
  SetupView,
  WorkspaceView,
} from "../api/generated"
import {
  ConsoleApiError,
  ConsoleReadTransportError,
  MalformedConsoleResponse,
} from "../api/client"
import {App, type ConsoleBootstrapClient} from "../app"
import {selectLandingRoute} from "./landing-route"
import {ConsoleRoutes} from "./router"

const sessionEnvelope: ConsoleEnvelopeSessionView = {
  meta: {correlation_id: "correlation-session", data_provenance: "demo_fixture"},
  data: {
    actor: {display_name: "Dana Architect"},
    roles: ["data_architect"],
    active_role: "data_architect",
    tenant: {ref: "tenant-demo", display_name: "Northwind Demo"},
    workspace: {ref: "workspace-revenue", display_name: "Revenue to cash"},
    csrf_token: "csrf-token-with-at-least-thirty-two-characters",
  },
}

const capabilities: NonNullable<WorkspaceView["capabilities"]> = [
  {
    capability_id: "fixture-journey",
    label: "Fixture journey",
    state: "ready",
    detail: "Synthetic lifecycle interactions are available.",
  },
  {
    capability_id: "governed-evidence",
    label: "Governed evidence",
    state: "not_delivered",
    detail: "Fixture mode cannot issue authoritative evidence.",
    dependency: "Governed evidence service wiring",
  },
]

function workspaceEnvelope(state: WorkspaceView["state"]): ConsoleEnvelopeWorkspaceView {
  return {
    meta: {correlation_id: `correlation-${state}`, data_provenance: "demo_fixture"},
    data: {
      workspace: {ref: "workspace-revenue", display_name: "Revenue to cash"},
      state,
      capabilities,
      recovery_message: state === "unavailable" ? "Workspace projection is unavailable." : null,
    },
  }
}

const setup: SetupView = {
  workspace_ref: "workspace-revenue",
  revision: 4,
  setup_digest: "a".repeat(64),
  reset_token: "reset_token_fixture_sequence-0004",
  active_stage: "activation",
  stages: [
    {stage: "foundation", label: "Foundation", state: "complete", detail: null},
    {stage: "activation", label: "Activation", state: "current", detail: null},
  ],
  warehouse_options: [
    {
      engine: "postgresql",
      label: "PostgreSQL",
      supported_region: "us-west-2",
      fixed_capacity: "fixed-small",
    },
  ],
  pending_review_refs: ["review-activation"],
  managed_services: [],
  sources: [],
  warehouse_binding: null,
  process_package: null,
}

const setupEnvelope: ConsoleEnvelopeSetupView = {
  meta: {correlation_id: "correlation-setup", data_provenance: "demo_fixture"},
  data: setup,
}

const activationEnvelope: ConsoleEnvelopeReviewView = {
  meta: {correlation_id: "correlation-activation", data_provenance: "demo_fixture"},
  data: {
    review_id: "review-activation",
    kind: "activation",
    title: "Activation approval",
    summary: "Confirm the exact activation projection before managed effects are admitted.",
    revision: 6,
    reviewed_digest: "b".repeat(64),
    sections: [
      {
        section_id: "activation-scope",
        title: "Activation scope",
        summary: null,
        items: [{label: "Execution mode", value: "Governed local", material_change: false}],
      },
    ],
    required_authorities: [
      {
        role: "data_architect",
        reason: "Confirm the activation scope.",
        subject_digest: "c".repeat(64),
        satisfied: false,
      },
    ],
    decisions: [],
    constraints: [],
    evidence_refs: ["evidence-activation-plan"],
    can_decide: true,
  },
}

const setupClient = {
  confirmWarehouseBinding: vi.fn(),
  decideReview: vi.fn(),
  getOperation: vi.fn(),
  getReview: vi.fn(async () => activationEnvelope),
  ...featureClientStubs(),
  getSetup: vi.fn(async () => setupEnvelope),
  submitProcessPackage: vi.fn(),
}

afterEach(() => {
  vi.unstubAllGlobals()
  window.history.replaceState(null, "", "/")
})

describe("selectLandingRoute", () => {
  test.each([
    ["setup", undefined, "/setup"],
    ["pending_activation", setup, "/reviews/review-activation"],
    ["active", undefined, "/inbox"],
    ["unavailable", undefined, "/recovery"],
  ] satisfies ReadonlyArray<[WorkspaceView["state"], SetupView | undefined, string]>) (
    "selects the server-authorized %s landing route",
    (state, pendingSetup, expectedRoute) => {
      expect(selectLandingRoute(workspaceEnvelope(state).data, pendingSetup)).toBe(expectedRoute)
    },
  )

  test("fails closed when pending activation has no server-issued review reference", () => {
    expect(selectLandingRoute(workspaceEnvelope("pending_activation").data)).toBe("/recovery")
  })
})

function renderRoutes(path: string, state: WorkspaceView["state"] = "active") {
  const needsSetup =
    state === "setup" ||
    state === "pending_activation" ||
    path.startsWith("/setup") ||
    path.startsWith("/reviews/")
  return render(
    <MemoryRouter initialEntries={[path]}>
      <ConsoleRoutes
        sessionEnvelope={sessionEnvelope}
        setupClient={setupClient}
        setupEnvelope={needsSetup ? setupEnvelope : undefined}
        workspaceEnvelope={workspaceEnvelope(state)}
      />
    </MemoryRouter>,
  )
}

describe("ConsoleRoutes", () => {
  test("fails closed rather than redirecting /setup to itself", async () => {
    // `selectLandingRoute` maps the `setup` state back to `/setup`, so redirecting
    // an absent envelope to the landing route made this route navigate to itself.
    // Rendering that combination used to hang the suite instead of failing it.
    render(
      <MemoryRouter initialEntries={["/setup"]}>
        <ConsoleRoutes
          sessionEnvelope={sessionEnvelope}
          setupClient={setupClient}
          setupEnvelope={undefined}
          workspaceEnvelope={workspaceEnvelope("setup")}
        />
      </MemoryRouter>,
    )

    await waitFor(() =>
      expect(screen.getByRole("heading", {name: "Workspace projections do not match"})).toBeVisible(),
    )
  })

  test("sends a finished setup back to the workspace instead of a recovery boundary", async () => {
    // Once the warehouse binding is ready the workspace turns `active`, and the
    // app deliberately stops fetching the setup projection. Reaching `/setup`
    // after that -- a bookmark, a back button, a reload -- is an ordinary thing to
    // do, and it used to render "Workspace projections do not match", which is
    // both alarming and untrue: nothing mismatched, the stage is simply over.
    render(
      <MemoryRouter initialEntries={["/setup"]}>
        <ConsoleRoutes
          sessionEnvelope={sessionEnvelope}
          setupClient={setupClient}
          setupEnvelope={undefined}
          workspaceEnvelope={workspaceEnvelope("active")}
        />
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByRole("region", {name: "Decision queue"})).toBeVisible())
    expect(screen.queryByText("Workspace projections do not match")).toBeNull()
  })


  test.each([
    ["setup", "Activation approval"],
    ["pending_activation", "Activation approval"],
    ["active", "Decision queue"],
    ["unavailable", "Workspace unavailable"],
  ] satisfies ReadonlyArray<[WorkspaceView["state"], string]>) (
    "lands a %s workspace on its authorized product surface",
    async (state, heading) => {
      renderRoutes("/", state)

      const role = heading === "Decision queue" ? "region" : "heading"
      await waitFor(() => expect(screen.getByRole(role, {name: heading})).toBeVisible())
    },
  )

  test.each([
    ["/setup", "Activation approval"],
    ["/reviews/review-activation", "Activation approval"],
    ["/inbox", "Decision queue"],
    ["/data-products", "Data products"],
    ["/runs", "Runs"],
    ["/catalog", "Catalog"],
    ["/dashboards", "Dashboards"],
    ["/evidence", "Evidence"],
    ["/recovery", "Workspace unavailable"],
  ])("keeps fixture provenance visible on %s", async (path, heading) => {
    renderRoutes(path)

    const role = heading === "Decision queue" ? "region" : "heading"
    await waitFor(() => expect(screen.getByRole(role, {name: heading})).toBeVisible())
    expect(screen.getByText("Demo scenario - no managed effects")).toBeVisible()
  })

  test.each([
    ["/data-products", "Data products", "Review versioned products and their governed state."],
    ["/catalog", "Catalog", "Inspect published meaning, ownership, and lineage."],
    ["/dashboards", "Dashboards", "Review managed dashboard capability and delivery boundaries."],
    ["/evidence", "Evidence", "Trace decisions and outcomes to immutable evidence references."],
  ])("renders %s as a substantive typed capability summary", (path, heading, summary) => {
    renderRoutes(path)

    expect(screen.getByRole("heading", {name: heading})).toBeVisible()
    expect(screen.getByText(summary)).toBeVisible()
    expect(screen.getByRole("list", {name: `${heading} capability states`})).toBeVisible()
    expect(screen.getAllByText("Ready").length).toBeGreaterThan(0)
    expect(screen.getAllByText("Not delivered").length).toBeGreaterThan(0)
  })

  test("reaches the acquisition receipt listing from its own route", async () => {
    // A route with no navigation entry is a dead feature, so this asserts the
    // listing renders at the path the shell links to.
    renderRoutes("/acquisition-receipts")

    await waitFor(() =>
      expect(screen.getByText(/no acquisition has recorded a receipt/i)).toBeVisible(),
    )
    expect(screen.getByRole("heading", {name: "Acquisition evidence"})).toBeVisible()
  })

  test("renders /runs as the delivered listing rather than a capability summary", async () => {
    // `/runs` was one of the summary placeholders above until the owning services
    // published the reads it needs. It now reads real runs, so it is asserted as a
    // listing; leaving it in the parametrised set would have kept pinning the
    // placeholder in place.
    renderRoutes("/runs")

    await waitFor(() =>
      expect(screen.getByText(/no runs have been recorded/i)).toBeVisible(),
    )
    expect(screen.getByRole("heading", {name: "Runs"})).toBeVisible()
  })

  test.each([
    [
      "session/workspace",
      {...sessionEnvelope, data: {...sessionEnvelope.data, workspace: {...sessionEnvelope.data.workspace, ref: "workspace-other"}}},
      workspaceEnvelope("active"),
      undefined,
    ],
    [
      "setup/workspace",
      sessionEnvelope,
      workspaceEnvelope("pending_activation"),
      {...setupEnvelope, data: {...setupEnvelope.data, workspace_ref: "workspace-other"}},
    ],
    [
      "provenance",
      {...sessionEnvelope, meta: {...sessionEnvelope.meta, data_provenance: "governed_local"}},
      workspaceEnvelope("active"),
      undefined,
    ],
  ] as const)("fails closed before rendering mixed %s projections", (_case, session, workspace, pendingSetup) => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <ConsoleRoutes
          sessionEnvelope={session}
          setupClient={setupClient}
          setupEnvelope={pendingSetup}
          workspaceEnvelope={workspace}
        />
      </MemoryRouter>,
    )

    expect(screen.getByRole("heading", {name: "Workspace projections do not match"})).toBeVisible()
    expect(screen.queryByText("Revenue to cash")).not.toBeInTheDocument()
  })

  test("renders the exact pending activation review inside the seven-stage workbench", async () => {
    setupClient.getReview.mockClear()

    renderRoutes("/reviews/review-activation", "pending_activation")

    expect(await screen.findByRole("heading", {name: "Activation approval"})).toBeVisible()
    expect(screen.getByRole("navigation", {name: "Setup progress"})).toBeVisible()
    expect(screen.getByRole("heading", {name: "Activation scope"})).toBeVisible()
    expect(screen.getByText("evidence-activation-plan")).toBeVisible()
    expect(setupClient.getReview).toHaveBeenCalledWith("review-activation")
  })
})

test("App shows loading and then a server-unavailable recovery page", async () => {
  let rejectWorkspace: ((reason: unknown) => void) | undefined
  const pendingWorkspace = new Promise<ConsoleEnvelopeWorkspaceView>((_resolve, reject) => {
    rejectWorkspace = reject
  })
  const client: ConsoleBootstrapClient = {
    ...setupClient,
    getSession: vi.fn(async () => sessionEnvelope),
    getWorkspace: vi.fn(() => pendingWorkspace),
    ...featureClientStubs(),
    getSetup: vi.fn(async () => setupEnvelope),
  }

  render(<App client={client} />)

  expect(screen.getByRole("status")).toHaveTextContent("Loading governed workspace")
  rejectWorkspace?.(new TypeError("network canary"))

  expect(
    await screen.findByRole("heading", {name: "The console service is unavailable"}),
  ).toBeVisible()
  expect(screen.queryByText("network canary")).not.toBeInTheDocument()
})

test("App loads the setup projection for a workspace still in setup", async () => {
  const getSetup = vi.fn(async () => setupEnvelope)
  const client: ConsoleBootstrapClient = {
    ...setupClient,
    getSession: vi.fn(async () => sessionEnvelope),
    getWorkspace: vi.fn(async () => workspaceEnvelope("setup")),
    getSetup,
  }

  render(<App client={client} />)

  expect(await screen.findByRole("heading", {name: "Activation approval"})).toBeVisible()
  expect(getSetup).toHaveBeenCalledTimes(1)
})

test("App fails a malformed bootstrap response closed", async () => {
  const client: ConsoleBootstrapClient = {
    ...setupClient,
    getSession: vi.fn(async () => sessionEnvelope),
    getWorkspace: vi.fn(async () => {
      throw new MalformedConsoleResponse("workspace_response", 200)
    }),
    ...featureClientStubs(),
    getSetup: vi.fn(async () => setupEnvelope),
  }

  render(<App client={client} />)

  expect(
    await screen.findByRole("heading", {name: "This workspace response could not be displayed"}),
  ).toBeVisible()
  await waitFor(() => expect(screen.queryByRole("status")).not.toBeInTheDocument())
})

test("reauthentication requires an external trusted session and exposes no fake console action", async () => {
  const error = new ConsoleApiError(401, {
    meta: {correlation_id: "correlation-reauthenticate", data_provenance: "governed_local"},
    error: {
      code: "trusted-session-missing",
      safe_message: "The trusted session is unavailable.",
      recovery_action: "reauthenticate",
      field: null,
    },
  })
  const getSession = vi.fn(async () => {
    throw error
  })
  const client: ConsoleBootstrapClient = {
    ...setupClient,
    getSession,
    getWorkspace: vi.fn(async () => workspaceEnvelope("active")),
    ...featureClientStubs(),
    getSetup: vi.fn(async () => setupEnvelope),
  }

  render(<App client={client} />)

  expect(await screen.findByRole("heading", {name: "Sign in again"})).toBeVisible()
  expect(
    screen.getByText("The trusted session must be re-established outside this console."),
  ).toBeVisible()
  expect(screen.getByText("Support reference: correlation-reauthenticate")).toBeVisible()
  expect(screen.queryByRole("button")).not.toBeInTheDocument()
  expect(screen.queryByText("Reload sign-in")).not.toBeInTheDocument()
  expect(getSession).toHaveBeenCalledTimes(1)
})

test("reload recovery starts a second bootstrap and renders its authorized route", async () => {
  const user = userEvent.setup()
  const secondSession = Promise.withResolvers<ConsoleEnvelopeSessionView>()
  const reloadError = new ConsoleApiError(409, {
    meta: {correlation_id: "correlation-reload-action", data_provenance: "governed_local"},
    error: {
      code: "stale-bootstrap",
      safe_message: "The workspace projection changed.",
      recovery_action: "reload",
      field: null,
    },
  })
  const getSession = vi
    .fn(async () => sessionEnvelope)
    .mockRejectedValueOnce(reloadError)
    .mockImplementationOnce(() => secondSession.promise)
  const getWorkspace = vi.fn(async () => workspaceEnvelope("active"))
  const client: ConsoleBootstrapClient = {
    ...setupClient,
    getSession,
    getWorkspace,
    ...featureClientStubs(),
    getSetup: vi.fn(async () => setupEnvelope),
  }

  render(<App client={client} />)
  expect(await screen.findByRole("heading", {name: "Workspace state changed"})).toBeVisible()

  await user.click(screen.getByRole("button", {name: "Reload workspace"}))

  await waitFor(() => expect(getSession).toHaveBeenCalledTimes(2))
  expect(getWorkspace).toHaveBeenCalledTimes(2)
  expect(screen.getByRole("status")).toHaveTextContent("Loading governed workspace")
  expect(
    screen.queryByRole("heading", {name: "Workspace state changed"}),
  ).not.toBeInTheDocument()

  secondSession.resolve(sessionEnvelope)

  // findBy resolves the first match; the queue renders the same landmark in its
  // loading and loaded states, so a slower machine can hand back the node React
  // is about to replace. Re-query until the settled tree satisfies it.
  await waitFor(() => expect(screen.getByRole("region", {name: "Decision queue"})).toBeVisible())
  expect(screen.queryByText("Loading governed workspace")).not.toBeInTheDocument()
  expect(screen.queryByRole("button", {name: "Reload workspace"})).not.toBeInTheDocument()
})

test("read retry starts a second bootstrap and renders its authorized route", async () => {
  const user = userEvent.setup()
  const secondSession = Promise.withResolvers<ConsoleEnvelopeSessionView>()
  const getSession = vi
    .fn(async () => sessionEnvelope)
    .mockRejectedValueOnce(new ConsoleReadTransportError("session_response"))
    .mockImplementationOnce(() => secondSession.promise)
  const getWorkspace = vi.fn(async () => workspaceEnvelope("active"))
  const client: ConsoleBootstrapClient = {
    ...setupClient,
    getSession,
    getWorkspace,
    ...featureClientStubs(),
    getSetup: vi.fn(async () => setupEnvelope),
  }

  render(<App client={client} />)
  expect(
    await screen.findByRole("heading", {name: "The console service is unavailable"}),
  ).toBeVisible()

  await user.click(screen.getByRole("button", {name: "Try again"}))

  await waitFor(() => expect(getSession).toHaveBeenCalledTimes(2))
  expect(getWorkspace).toHaveBeenCalledTimes(2)
  expect(screen.getByRole("status")).toHaveTextContent("Loading governed workspace")
  expect(
    screen.queryByRole("heading", {name: "The console service is unavailable"}),
  ).not.toBeInTheDocument()

  secondSession.resolve(sessionEnvelope)

  // findBy resolves the first match; the queue renders the same landmark in its
  // loading and loaded states, so a slower machine can hand back the node React
  // is about to replace. Re-query until the settled tree satisfies it.
  await waitFor(() => expect(screen.getByRole("region", {name: "Decision queue"})).toBeVisible())
  expect(screen.queryByText("Loading governed workspace")).not.toBeInTheDocument()
  expect(screen.queryByRole("button", {name: "Try again"})).not.toBeInTheDocument()
})

test.each([
  ["reload", "Workspace state changed", "Reload workspace"],
  ["retry", "The console service is unavailable", "Try again"],
  ["contact_support", "Support is needed", null],
  ["correct_input", "The request needs correction", null],
  ["none", "No browser recovery is available", null],
] as const)(
  "maps bootstrap recovery action %s to explicit safe guidance",
  async (recoveryAction, heading, actionLabel) => {
    const error = new ConsoleApiError(503, {
      meta: {correlation_id: `correlation-${recoveryAction}`, data_provenance: "governed_local"},
      error: {
        code: `bootstrap-${recoveryAction}`,
        safe_message: `Safe ${recoveryAction} guidance.`,
        recovery_action: recoveryAction,
        field: null,
      },
    })
    const client: ConsoleBootstrapClient = {
      ...setupClient,
      getSession: vi.fn(async () => {
        throw error
      }),
      getWorkspace: vi.fn(async () => workspaceEnvelope("active")),
      ...featureClientStubs(),
      getSetup: vi.fn(async () => setupEnvelope),
    }

    render(<App client={client} />)

    expect(await screen.findByRole("heading", {name: heading})).toBeVisible()
    expect(screen.getByText(`Safe ${recoveryAction} guidance.`)).toBeVisible()
    expect(screen.getByText(`Support reference: correlation-${recoveryAction}`)).toBeVisible()
    if (actionLabel === null) {
      expect(screen.queryByRole("button")).not.toBeInTheDocument()
    } else {
      expect(screen.getByRole("button", {name: actionLabel})).toBeVisible()
    }
  },
)

test("App re-reads its projections after the warehouse command settles", async () => {
  // The command changes the workspace state, the governance spine and the stage
  // list, none of which this page owns. Without a re-read the shell kept the
  // pre-command projection: "Managed warehouse: Blocked" beside the stage's own
  // "Provisioning succeeded", with the engine choice still offered. An architect
  // reads that as a failure and confirms again, which the server then refuses.
  const user = userEvent.setup()
  const getWorkspace = vi.fn(async () => workspaceEnvelope("setup"))
  // The shared fixture sits at the activation stage; the warehouse choice is the
  // foundation stage's.
  const foundationEnvelope = {
    ...setupEnvelope,
    data: {...setupEnvelope.data, active_stage: "foundation" as const},
  }
  const getSetup = vi.fn(async () => foundationEnvelope)
  const client: ConsoleBootstrapClient = {
    ...setupClient,
    getSession: vi.fn(async () => sessionEnvelope),
    getWorkspace,
    ...featureClientStubs(),
    getSetup,
    confirmWarehouseBinding: vi.fn(async () => ({
      envelope: {
        meta: setupEnvelope.meta,
        data: {
          operation_id: "operation-warehouse-ready",
          revision: 1,
          state: "succeeded" as const,
          phase: "ready",
          summary: "The managed warehouse binding is ready.",
          recovery_actions: [],
          evidence_ref: null,
          failure: null,
          operation_digest: null,
          retry_token: null,
        },
      },
      kind: "terminal" as const,
      state: "succeeded" as const,
      status: 200 as const,
    })),
  }

  render(<App client={client} />)
  await screen.findByRole("radio", {name: /PostgreSQL/})
  const readsBefore = getWorkspace.mock.calls.length

  await user.click(
    screen.getByRole("checkbox", {
      name: "I understand that this warehouse binding is immutable after confirmation.",
    }),
  )
  await user.click(screen.getByRole("button", {name: "Confirm warehouse binding"}))

  await waitFor(() => expect(getWorkspace.mock.calls.length).toBeGreaterThan(readsBefore))
})
