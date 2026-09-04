import {render, screen, waitFor} from "@testing-library/react"
import {expect, test, vi} from "vitest"

import type {
  AcquisitionReceiptView,
  ConsoleEnvelopeAcquisitionReceiptsView,
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

test("a recorded receipt is listed with the references it names", async () => {
  const client = {getAcquisitionReceipts: vi.fn().mockResolvedValue(envelope([receipt]))}

  render(<AcquisitionReceiptsPage client={client} />)

  await waitFor(() => expect(screen.getByText("evidence-ref:prepared-1")).toBeVisible())
  expect(screen.getByText("contract:orders:v1")).toBeVisible()
  expect(screen.getByText("source-binding:orders")).toBeVisible()
})

test("a governed refusal is shown with the reasons it carries", async () => {
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

  await waitFor(() => expect(screen.getByText(/no valid plan/i)).toBeVisible())
  expect(screen.getByText("contract_not_activated")).toBeVisible()
})

test("a tenant with no receipts is told so rather than shown an empty frame", async () => {
  const client = {getAcquisitionReceipts: vi.fn().mockResolvedValue(envelope([]))}

  render(<AcquisitionReceiptsPage client={client} />)

  await waitFor(() =>
    expect(screen.getByText(/no acquisition has recorded a receipt/i)).toBeVisible(),
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
