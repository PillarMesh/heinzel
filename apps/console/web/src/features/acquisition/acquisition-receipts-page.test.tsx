import {render, screen, waitFor} from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import {expect, test, vi} from "vitest"

import type {
  AcquisitionReceiptView,
  ConsoleEnvelopeAcquisitionReceiptView,
  ConsoleEnvelopeAcquisitionReceiptsView,
  SessionView,
} from "../../api/generated"
import {ConsoleApiError} from "../../api/client"
import {AcquisitionReceiptsPage} from "./acquisition-receipts-page"

function envelope(
  receipts: readonly AcquisitionReceiptView[],
  runAvailable = true,
): ConsoleEnvelopeAcquisitionReceiptsView {
  return {
    data: {receipts: [...receipts], run_available: runAvailable},
    meta: {data_provenance: "governed_local", correlation_id: "correlation-acquisition"},
  } as ConsoleEnvelopeAcquisitionReceiptsView
}

const receipt: AcquisitionReceiptView = {
  evidence_id: "evidence-ref:prepared-1",
  contract_ref: "contract:orders:v1",
  source_binding_ref: "source-binding:orders",
  acquisition_mode: "snapshot",
  logical_object_refs: ["orders"],
  outcome: "prepared",
  reason_codes: [],
  created_at: "2026-09-01T12:00:00Z",
}

const session: SessionView = {
  active_role: "data_architect",
  actor: {display_name: "Architect"},
  csrf_token: "csrf-token-with-at-least-thirty-two-characters",
  roles: ["data_architect"],
  tenant: {display_name: "Tenant A", ref: "tenant-a"},
  workspace: {display_name: "Workspace A", ref: "workspace-a"},
}

test("a recorded receipt is listed with the references it names", async () => {
  const user = userEvent.setup()
  const client = {getAcquisitionReceipts: vi.fn().mockResolvedValue(envelope([receipt]))}

  render(<AcquisitionReceiptsPage client={client} />)

  await waitFor(() => expect(screen.getByText(/prepared snapshot acquisition/i)).toBeVisible())
  await user.click(screen.getByText(/technical references/i))
  expect(screen.getByText("evidence-ref:prepared-1")).toBeVisible()
  expect(screen.getByText("contract:orders:v1")).toBeVisible()
  expect(screen.getByText("source-binding:orders")).toBeVisible()
})

test("a governed refusal is shown with the reasons it carries", async () => {
  const user = userEvent.setup()
  const client = {
    getAcquisitionReceipts: vi.fn().mockResolvedValue(
      envelope([
        {
          ...receipt,
          evidence_id: "evidence-ref:refused-1",
          outcome: "no_valid_plan",
          reason_codes: ["contract_not_activated"],
        },
      ]),
    ),
  }

  render(<AcquisitionReceiptsPage client={client} />)

  await waitFor(() =>
    expect(screen.getByRole("heading", {name: /no valid plan/i})).toBeVisible(),
  )
  await user.click(screen.getByText(/technical references/i))
  expect(screen.getByText("contract_not_activated")).toBeVisible()
})

test("a tenant with no receipts is told so rather than shown an empty frame", async () => {
  const client = {getAcquisitionReceipts: vi.fn().mockResolvedValue(envelope([]))}

  render(<AcquisitionReceiptsPage client={client} />)

  await waitFor(() =>
    expect(screen.getByText(/no acquisition receipts/i)).toBeVisible(),
  )
})

test("a failed read reports the server's own message", async () => {
  const client = {
    getAcquisitionReceipts: vi.fn().mockRejectedValue(
      new ConsoleApiError(503, {
        error: {
          code: "capability_not_delivered",
          safe_message: "This capability is not delivered.",
          recovery_action: "none",
        },
        meta: {data_provenance: "governed_local", correlation_id: "correlation-acquisition"},
      } as never),
    ),
  }

  render(<AcquisitionReceiptsPage client={client} />)

  await waitFor(() => expect(screen.getByText(/this capability is not delivered/i)).toBeVisible())
})

test("an architect can run an activated contract and sees its receipt", async () => {
  const user = userEvent.setup()
  const runAcquisitionNow = vi.fn().mockResolvedValue({
    data: receipt,
    meta: {data_provenance: "governed_local", correlation_id: "correlation-run"},
  } satisfies ConsoleEnvelopeAcquisitionReceiptView)
  const client = {
    getAcquisitionReceipts: vi.fn().mockResolvedValue(envelope([])),
    runAcquisitionNow,
  }

  render(
    <AcquisitionReceiptsPage
      client={client}
      idempotencyKeyFactory={() => "acquisition-run-test-key"}
      session={session}
      triggerWindowFactory={() => "2026-09-14T12:00:00Z/2026-09-14T13:00:00Z"}
    />,
  )
  // Awaited, because the form is offered only once the read has said a run can be commanded.
  await user.type(
    await screen.findByLabelText(/activated contract reference/i),
    "contract:orders:v1",
  )
  await user.click(screen.getByRole("button", {name: /run acquisition/i}))

  await waitFor(() => expect(screen.getByText(/acquisition prepared/i)).toBeVisible())
  expect(runAcquisitionNow).toHaveBeenCalledWith(
    {
      acquisition_mode: "snapshot",
      active_role: "data_architect",
      contract_ref: "contract:orders:v1",
      trigger_window: "2026-09-14T12:00:00Z/2026-09-14T13:00:00Z",
    },
    {
      csrfToken: session.csrf_token,
      idempotencyKey: "acquisition-run-test-key",
    },
  )
  expect(screen.getByText(/prepared snapshot acquisition/i)).toBeVisible()
})

test("a deployment that commands no run offers no control, and says why", async () => {
  // The page listed every receipt an acquisition ever wrote and then offered a form to ask for
  // another, on a deployment whose answer to that ask is `capability_not_delivered`. The
  // receipts are the page; the form is only offered where something could act on it.
  const runAcquisitionNow = vi.fn()
  render(
    <AcquisitionReceiptsPage
      client={{
        getAcquisitionReceipts: vi.fn().mockResolvedValue(envelope([receipt], false)),
        runAcquisitionNow,
      }}
      session={session}
    />,
  )

  expect(
    await screen.findByText(/acquisition runs are not commanded from this deployment/i),
  ).toBeVisible()
  expect(screen.queryByRole("button", {name: /run acquisition/i})).toBeNull()
  expect(screen.queryByLabelText(/activated contract reference/i)).toBeNull()
  expect(runAcquisitionNow).not.toHaveBeenCalled()
  // The receipts themselves are untouched: the read is delivered, only the command is not.
  expect(screen.getByRole("list", {name: "Acquisition receipts"})).toBeVisible()
})

test("a source that has been acquired is offered the changes since, not a second snapshot", async () => {
  /*
   * A snapshot is admitted only from checkpoint revision 0, and an acknowledged receipt is the
   * record of a checkpoint having advanced. The form sent `snapshot` whatever the state of the
   * source, so every press after the first one was refused -- and the refusal, left to the
   * provider, read as state the console could not trust.
   */
  const user = userEvent.setup()
  const acknowledged: AcquisitionReceiptView = {
    ...receipt,
    evidence_id: "evidence-ref:acknowledged-1",
    outcome: "acknowledged",
  }
  const runAcquisitionNow = vi.fn().mockResolvedValue({
    data: {...acknowledged, evidence_id: "evidence-ref:acknowledged-2"},
    meta: {data_provenance: "governed_local", correlation_id: "correlation-run"},
  } satisfies ConsoleEnvelopeAcquisitionReceiptView)
  const client = {
    getAcquisitionReceipts: vi.fn().mockResolvedValue(envelope([acknowledged])),
    runAcquisitionNow,
  }

  render(
    <AcquisitionReceiptsPage
      client={client}
      idempotencyKeyFactory={() => "acquisition-run-test-key"}
      session={session}
      triggerWindowFactory={() => "2026-09-14T12:00:00Z/2026-09-14T13:00:00Z"}
    />,
  )
  await user.type(
    await screen.findByLabelText(/activated contract reference/i),
    "contract:orders:v1",
  )

  expect(screen.getByLabelText(/what to acquire/i)).toHaveValue("incremental")
  expect(screen.getByText(/only what has changed since can be acquired/i)).toBeVisible()

  await user.click(screen.getByRole("button", {name: /run acquisition/i}))

  await waitFor(() => expect(runAcquisitionNow).toHaveBeenCalled())
  expect(runAcquisitionNow.mock.calls[0]?.[0]).toMatchObject({
    acquisition_mode: "incremental",
    contract_ref: "contract:orders:v1",
  })
  // What the run did, and what it did not do. A landed generation is not a rebuilt product.
  expect(await screen.findByText(/landed as a new generation/i)).toBeVisible()
  expect(screen.getByText(/building a product over that generation is a separate step/i)).toBeVisible()
})

test("a contract with no acknowledged receipt is still offered the whole source", async () => {
  // The complement, and the first run of a governed deployment: nothing has been acquired, so
  // a snapshot is the one mode the source admits.
  const user = userEvent.setup()
  const client = {
    getAcquisitionReceipts: vi.fn().mockResolvedValue(envelope([receipt])),
    runAcquisitionNow: vi.fn(),
  }

  render(<AcquisitionReceiptsPage client={client} session={session} />)
  await user.type(
    await screen.findByLabelText(/activated contract reference/i),
    "contract:orders:v1",
  )

  expect(screen.getByLabelText(/what to acquire/i)).toHaveValue("snapshot")
  expect(screen.getByText(/has not been acquired yet/i)).toBeVisible()
})
