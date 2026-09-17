import {render, screen, waitFor} from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import {expect, test, vi} from "vitest"

import type {ConsoleEnvelopeRunsView, LeasedRunView, RunView} from "../../api/generated"
import {ConsoleApiError} from "../../api/client"
import {RunsPage} from "./runs-page"

function envelope(
  runs: readonly RunView[],
  leasedRuns: readonly LeasedRunView[] = [],
): ConsoleEnvelopeRunsView {
  return {
    data: {runs: [...runs], leased_runs: [...leasedRuns]},
    meta: {data_provenance: "governed_local", correlation_id: "correlation-runs"},
  } as ConsoleEnvelopeRunsView
}

const run: RunView = {
  run_id: "run-000000000000000000000001",
  contract_digest: "a".repeat(64),
  state: "succeeded",
  created_at: "2026-09-01T12:00:00Z",
  updated_at: "2026-09-01T13:00:00Z",
}

test("a witnessed run keeps internal identifiers behind technical details", async () => {
  const client = {getRuns: vi.fn().mockResolvedValue(envelope([run]))}
  const user = userEvent.setup()

  render(<RunsPage client={client} />)

  await waitFor(() => expect(screen.getByRole("heading", {name: "Succeeded run"})).toBeVisible())
  expect(screen.queryByText("run-000000000000000000000001")).not.toBeVisible()
  expect(screen.queryByText(/a{64}/)).not.toBeVisible()

  await user.click(screen.getByText("Technical details"))

  expect(screen.getByText("run-000000000000000000000001")).toBeVisible()
  expect(screen.getByText(/a{64}/)).toBeVisible()
})

test("a tenant with no runs is told so rather than shown an empty frame", async () => {
  const client = {getRuns: vi.fn().mockResolvedValue(envelope([]))}

  render(<RunsPage client={client} />)

  await waitFor(() => expect(screen.getByText(/no runs have been recorded/i)).toBeVisible())
})

test("a run's own state is shown rather than mapped onto another vocabulary", async () => {
  const client = {
    getRuns: vi.fn().mockResolvedValue(envelope([{...run, state: "non_conforming"}])),
  }

  render(<RunsPage client={client} />)

  await waitFor(() => expect(screen.getByText(/non-conforming/i)).toBeVisible())
})

test("a failed read reports the server's own message", async () => {
  const client = {
    getRuns: vi.fn().mockRejectedValue(
      new ConsoleApiError(503, {
        error: {
          code: "capability_not_delivered",
          safe_message: "This capability is not delivered.",
          recovery_action: "none",
        },
        meta: {data_provenance: "governed_local", correlation_id: "correlation-runs"},
      } as never),
    ),
  }

  render(<RunsPage client={client} />)

  await waitFor(() =>
    expect(screen.getByText(/this capability is not delivered/i)).toBeVisible(),
  )
})

const leasedRun: LeasedRunView = {
  run_id: "leased-run-0001",
  contract_id: "contract-revenue",
  contract_revision: 2,
  trigger_reason: "scheduled",
  window_starts_at: "2026-09-16T00:00:00Z",
  window_ends_at: "2026-09-17T00:00:00Z",
  status: "retryable",
  last_durable_boundary_ref: "acquisition_prepared:prepared-receipt-1",
  observed_at: "2026-09-17T12:05:00Z",
  attempts: [
    {
      attempt_number: 1,
      epoch: 1,
      worker_ref: "worker-a",
      claimed_at: "2026-09-17T12:00:00Z",
      lease_expires_at: "2026-09-17T12:01:00Z",
    },
    {
      attempt_number: 2,
      epoch: 2,
      worker_ref: "worker-b",
      claimed_at: "2026-09-17T12:02:00Z",
      lease_expires_at: "2026-09-17T12:03:00Z",
      outcome: "failed",
      failure_classification: "transient",
      durable_boundary_ref: "acquisition_prepared:prepared-receipt-1",
      completed_at: "2026-09-17T12:02:30Z",
    },
  ],
}

test("a leased run shows its status, window and attempts with internals behind details", async () => {
  const client = {getRuns: vi.fn().mockResolvedValue(envelope([], [leasedRun]))}
  const user = userEvent.setup()

  render(<RunsPage client={client} />)

  await waitFor(() =>
    expect(
      screen.getByRole("heading", {name: "Scheduled run, failed and retryable"}),
    ).toBeVisible(),
  )
  expect(screen.getByText(/window 2026-09-16T00:00:00.000Z to 2026-09-17T00:00:00.000Z/i)).toBeVisible()
  expect(screen.getByText(/2 attempts, latest epoch 2/i)).toBeVisible()
  expect(screen.queryByText(/no runs have been recorded/i)).toBeNull()
  expect(screen.queryByText("worker-b")).not.toBeVisible()
  for (const boundary of screen.getAllByText("acquisition_prepared:prepared-receipt-1")) {
    expect(boundary).not.toBeVisible()
  }

  await user.click(screen.getByText("Technical details"))

  expect(screen.getByText("leased-run-0001")).toBeVisible()
  for (const boundary of screen.getAllByText("acquisition_prepared:prepared-receipt-1")) {
    expect(boundary).toBeVisible()
  }
  expect(screen.getByText("worker-b")).toBeVisible()
  expect(screen.getByText(/attempt 1, epoch 1: lease ended without an outcome/i)).toBeVisible()
  expect(screen.getByText(/attempt 2, epoch 2: failed \(transient\)/i)).toBeVisible()
})

test("a leased run that has proved no boundary says so", async () => {
  const pending: LeasedRunView = {
    ...leasedRun,
    status: "pending",
    attempts: [],
    last_durable_boundary_ref: null,
  }
  const client = {getRuns: vi.fn().mockResolvedValue(envelope([], [pending]))}
  const user = userEvent.setup()

  render(<RunsPage client={client} />)

  await waitFor(() =>
    expect(screen.getByRole("heading", {name: "Scheduled run, not yet started"})).toBeVisible(),
  )
  expect(screen.getByText(/no attempts yet/i)).toBeVisible()
  await user.click(screen.getByText("Technical details"))
  expect(screen.getByText(/no durable boundary proved yet/i)).toBeVisible()
})

test("an unavailable state-owned run read is reported rather than shown as no runs", async () => {
  const unavailable = {
    data: {runs: [], leased_runs: [], leased_runs_available: false},
    meta: {data_provenance: "governed_local", correlation_id: "correlation-runs"},
  } as ConsoleEnvelopeRunsView
  const client = {getRuns: vi.fn().mockResolvedValue(unavailable)}

  render(<RunsPage client={client} />)

  await waitFor(() =>
    expect(screen.getByText(/state-owned runs are unavailable right now/i)).toBeVisible(),
  )
  expect(screen.queryByText(/no runs have been recorded/i)).toBeNull()
})
