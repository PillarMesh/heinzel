import {render, screen} from "@testing-library/react"
import {expect, test, vi} from "vitest"

import type {
  ConsoleEnvelopeDashboardView,
  ConsoleEnvelopeDashboardsView,
} from "../../api/generated"
import {DashboardsPage} from "./dashboards-page"

const dashboard = {
  dashboard_ref: "dashboard-0123456789abcdef01234567",
  display_name: "Current revenue overview",
  version: 3,
  lifecycle_state: "active",
  as_of: "2026-09-11T11:55:00Z",
  freshness: "current",
  access_state: "active",
  published_at: "2026-09-11T12:00:00Z",
  state: "ready",
  summary: "Revision 3 was published by the managed BI provider on September 11, 2026.",
  preview_ref: null,
  link_ref: null,
} as const

test("lists provider-receipted dashboards without exposing provider or internal identifiers", async () => {
  const client = {
    getDashboards: vi.fn().mockResolvedValue({
      data: {dashboards: [dashboard]},
      meta: {data_provenance: "governed_local", correlation_id: "correlation-dashboard-list"},
    } as ConsoleEnvelopeDashboardsView),
    getDashboard: vi.fn(),
  }

  render(<DashboardsPage client={client} />)

  const link = await screen.findByRole("link", {name: "Current revenue overview"})
  expect(link).toHaveAttribute("href", `/dashboards/${dashboard.dashboard_ref}`)
  expect(screen.getByText(/Revision 3 was published/)).toBeVisible()
  expect(screen.getByText("Ready")).toBeVisible()
  expect(screen.getByText("Current")).toBeVisible()
  expect(screen.getByText("Data as of")).toBeVisible()
  expect(screen.getByText(new Date(dashboard.as_of).toLocaleString())).toBeVisible()
  expect(screen.getByText("Access")).toBeVisible()
  expect(screen.getByText("Active")).toBeVisible()
  expect(screen.queryByText(/Superset/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/internal:/i)).not.toBeInTheDocument()
  expect(screen.queryByRole("link", {name: /open/i})).not.toBeInTheDocument()
})

test("shows a dashboard detail by its public reference without rendering that reference", async () => {
  const client = {
    getDashboards: vi.fn(),
    getDashboard: vi.fn().mockResolvedValue({
      data: dashboard,
      meta: {data_provenance: "governed_local", correlation_id: "correlation-dashboard"},
    } as ConsoleEnvelopeDashboardView),
  }

  render(<DashboardsPage client={client} dashboardRef={dashboard.dashboard_ref} />)

  expect(await screen.findByRole("heading", {name: "Current revenue overview"})).toBeVisible()
  expect(screen.getByRole("region", {name: "Dashboard publication"})).toHaveTextContent(
    "Revision 3",
  )
  expect(screen.queryByText(dashboard.dashboard_ref)).not.toBeInTheDocument()
})

test("states when the owning service has no provider-receipted dashboards", async () => {
  const client = {
    getDashboards: vi.fn().mockResolvedValue({
      data: {dashboards: []},
      meta: {data_provenance: "governed_local", correlation_id: "correlation-dashboard-list"},
    } as ConsoleEnvelopeDashboardsView),
    getDashboard: vi.fn(),
  }

  render(<DashboardsPage client={client} />)

  expect(await screen.findByText("No dashboards have been published.")).toBeVisible()
})
