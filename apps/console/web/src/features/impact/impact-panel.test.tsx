import {render, screen, waitFor, within} from "@testing-library/react"
import {vi} from "vitest"

import {ConsoleApiError} from "../../api/client"
import type {ConsoleEnvelopeImpactView} from "../../api/generated"
import {ImpactPanel} from "./impact-panel"

const envelope: ConsoleEnvelopeImpactView = {
  meta: {correlation_id: "correlation-impact", data_provenance: "governed_local"},
  data: {
    request_id: "request-answer",
    change_type: "metric_version_change",
    subject_label: "Net revenue v2",
    analyzed_at: "2026-09-12T12:00:00Z",
    validated_impacts: [
      {
        impact_handle: "impact-visible-dashboard",
        label: "Revenue overview",
        asset_type: "Dashboard",
        owner_label: "Revenue data owner",
      },
    ],
    possible_impacts: [
      {
        impact_handle: "impact-visible-report",
        label: "Quarterly forecast",
        asset_type: "Report",
        owner_label: "Finance data owner",
      },
    ],
    affected_owners: ["Finance data owner", "Revenue data owner"],
    added_approvers: [
      {
        authority_label: "Revenue data owner",
        reason: "Approval required for a validated dependency.",
      },
    ],
  },
}

test("separates validated dependencies from possible advisory impacts", async () => {
  render(
    <ImpactPanel
      client={{getRequestImpact: vi.fn(async () => envelope)}}
      dataProvenance="governed_local"
      requestId="request-answer"
    />,
  )

  const panel = await screen.findByRole("region", {name: "Impact analysis"})
  expect(within(panel).getByText("Net revenue v2")).toBeVisible()
  expect(within(panel).getByRole("heading", {name: "Validated impacts"})).toBeVisible()
  expect(within(panel).getByText("Revenue overview")).toBeVisible()
  expect(within(panel).getByRole("heading", {name: "Possible impacts"})).toBeVisible()
  expect(within(panel).getByText("Quarterly forecast")).toBeVisible()
  expect(within(panel).getByText("These are advisory until their dependency is validated.")).toBeVisible()
  expect(within(panel).getByRole("heading", {name: "Added approvers"})).toBeVisible()
  expect(within(panel).queryByRole("button")).toBeNull()
  expect(within(panel).queryByText("request-answer")).toBeNull()
  expect(within(panel).queryByText("impact-visible-dashboard")).toBeNull()
})

test("fails closed when the impact reader is absent", async () => {
  render(
    <ImpactPanel
      client={{getRequestImpact: vi.fn(async () => { throw new Error("not wired") })}}
      dataProvenance="governed_local"
      requestId="request-answer"
    />,
  )

  expect(await screen.findByText("Impact analysis is unavailable for this request.")).toHaveAttribute(
    "role",
    "status",
  )
  expect(screen.queryByText("Revenue overview")).toBeNull()
})

test("renders no impact block when the request has no analysis", async () => {
  const notFound = new ConsoleApiError(404, {
    meta: {correlation_id: "correlation-not-found", data_provenance: "governed_local"},
    error: {
      code: "not_found",
      safe_message: "The requested resource was not found.",
      recovery_action: "none",
    },
  })
  render(
    <ImpactPanel
      client={{getRequestImpact: vi.fn(async () => { throw notFound })}}
      dataProvenance="governed_local"
      requestId="request-without-impact"
    />,
  )

  await waitFor(() => expect(screen.queryByText("Loading impact analysis…")).toBeNull())
  expect(screen.queryByRole("region", {name: "Impact analysis"})).toBeNull()
  expect(screen.queryByText("Impact analysis is unavailable for this request.")).toBeNull()
})

test.each([
  ["another request", {...envelope, data: {...envelope.data, request_id: "request-private"}}],
  ["another provenance", {...envelope, meta: {...envelope.meta, data_provenance: "demo_fixture" as const}}],
])("rejects a projection from %s without showing private data", async (_case, response) => {
  const privateResponse = {
    ...response,
    data: {
      ...response.data,
      validated_impacts: [
        {
          impact_handle: "impact-private",
          label: "PRIVATE EXECUTIVE MARGIN",
          asset_type: "Dashboard",
          owner_label: "Private owner",
        },
      ],
    },
  }
  render(
    <ImpactPanel
      client={{getRequestImpact: vi.fn(async () => privateResponse)}}
      dataProvenance="governed_local"
      requestId="request-answer"
    />,
  )

  expect(await screen.findByText("Impact analysis is unavailable for this request.")).toHaveAttribute(
    "role",
    "status",
  )
  expect(screen.queryByText("PRIVATE EXECUTIVE MARGIN")).toBeNull()
})
