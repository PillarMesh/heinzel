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
): ConsoleEnvelopeAcquisitionReceiptsView {
  return {
    data: {receipts: [...receipts]},
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
  await user.type(screen.getByLabelText(/activated contract reference/i), "contract:orders:v1")
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
