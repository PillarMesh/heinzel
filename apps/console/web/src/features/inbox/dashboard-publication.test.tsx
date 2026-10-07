import {render, screen, waitFor} from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import {expect, test, vi} from "vitest"

import type {ConsoleEnvelopePublishableDashboardsView, OperationView} from "../../api/generated"
import {DashboardPublication, type DashboardPublicationClient} from "./dashboard-publication"

const context = () => ({csrfToken: "csrf", idempotencyKey: "idem-publication"})

function offering(
  dashboards: {
    dashboard_id: string
    dashboard_version: number
    next_revision: number
    owner: string
  }[],
  answerTitle: string | null = "Revenue by region",
  publicationAvailable = true,
): ConsoleEnvelopePublishableDashboardsView {
  return {
    data: {
      answer_title: answerTitle,
      dashboards,
      publication_available: publicationAvailable,
      request_id: "req-1",
    },
    meta: {correlation_id: "correlation-publication", data_provenance: "demo_fixture"},
  } as ConsoleEnvelopePublishableDashboardsView
}

function operation(view: Partial<OperationView>): OperationView {
  return {
    operation_id: "dashboard-publication-1",
    phase: "dashboard_published",
    revision: 1,
    state: "succeeded",
    summary: "Dashboard published from its delivered answer.",
    ...view,
  } as OperationView
}

function client(overrides: Partial<DashboardPublicationClient> = {}): DashboardPublicationClient {
  return {
    getPublishableDashboards: vi.fn().mockResolvedValue(
      offering([
        {
          dashboard_id: "dashboard:revenue",
          dashboard_version: 3,
          next_revision: 2,
          owner: "principal:finance-owner",
        },
      ]),
    ),
    publishDashboard: vi.fn().mockResolvedValue({
      envelope: {data: operation({}), meta: {correlation_id: "correlation-publication", data_provenance: "demo_fixture"}},
      kind: "terminal",
      state: "succeeded",
      status: 200,
    }),
    ...overrides,
  } as DashboardPublicationClient
}

test("publishing sends the revision the offering named and never one the browser chose", async () => {
  const api = client()
  render(<DashboardPublication client={api} mutationContext={context} requestId="req-1" />)

  await userEvent.click(await screen.findByRole("button", {name: "Publish"}))

  await waitFor(() => {
    expect(api.publishDashboard).toHaveBeenCalledWith(
      {
        active_role: "data_architect",
        dashboard_id: "dashboard:revenue",
        dashboard_version: 3,
        expected_revision: 2,
        request_id: "req-1",
      },
      {csrfToken: "csrf", idempotencyKey: "idem-publication"},
    )
  })
  expect(await screen.findByRole("status")).toHaveTextContent("Dashboard published.")
})

test("an empty offering offers no publish control at all", async () => {
  const api = client({
    getPublishableDashboards: vi.fn().mockResolvedValue(offering([], null)),
  })
  render(<DashboardPublication client={api} mutationContext={context} requestId="req-1" />)

  expect(
    await screen.findByText(/No certified dashboard matches this answer/),
  ).toBeInTheDocument()
  expect(screen.queryByRole("button", {name: "Publish"})).not.toBeInTheDocument()
})

test("a refused publication shows the reason the server gave", async () => {
  const api = client({
    publishDashboard: vi.fn().mockResolvedValue({
      envelope: {
        data: operation({
          failure: {
            classification: "permanent",
            code: "dashboard_publication_window_expired",
            safe_message: "This answer's result is no longer readable.",
          },
          phase: "dashboard_publication_window_closed",
          state: "failed",
          summary: "The window to publish this answer has closed.",
        }),
        meta: {correlation_id: "correlation-publication", data_provenance: "demo_fixture"},
      },
      kind: "terminal",
      state: "failed",
      status: 200,
    }),
  })
  render(<DashboardPublication client={api} mutationContext={context} requestId="req-1" />)

  await userEvent.click(await screen.findByRole("button", {name: "Publish"}))

  expect(await screen.findByRole("status")).toHaveTextContent(
    "This answer's result is no longer readable.",
  )
})

test("an offering a deployment cannot publish shows the matches and no control", async () => {
  const api = client({
    getPublishableDashboards: vi.fn().mockResolvedValue(
      offering(
        [
          {
            dashboard_id: "dashboard:revenue",
            dashboard_version: 3,
            next_revision: 1,
            owner: "principal:finance-owner",
          },
        ],
        "Revenue by region",
        false,
      ),
    ),
  })
  render(<DashboardPublication client={api} mutationContext={context} requestId="req-1" />)

  expect(await screen.findByText("dashboard:revenue")).toBeInTheDocument()
  expect(await screen.findByText(/Publishing is not delivered/)).toBeInTheDocument()
  expect(screen.queryByRole("button", {name: "Publish"})).not.toBeInTheDocument()
  expect(api.publishDashboard).not.toHaveBeenCalled()
})
