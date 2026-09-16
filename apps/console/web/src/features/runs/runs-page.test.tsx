import {render, screen, waitFor} from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import {expect, test, vi} from "vitest"

import type {ConsoleEnvelopeRunsView, RunView} from "../../api/generated"
import {ConsoleApiError} from "../../api/client"
import {RunsPage} from "./runs-page"

function envelope(runs: readonly RunView[]): ConsoleEnvelopeRunsView {
  return {
    data: {runs: [...runs]},
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
