import {render, screen, waitFor} from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import {describe, expect, test, vi} from "vitest"

import type {
  ConsoleEnvelopeIncidentView,
  ConsoleEnvelopeIncidentsView,
  IncidentView,
  SessionView,
} from "../../api/generated"
import {OperationsPage, type OperationsClient} from "./operations-page"

const incident: IncidentView = {
  allowed_operator_actions: ["retry_transient_attempt"],
  classification: "transient",
  failed_stage: "extract",
  incident_id: "incident-internal-a",
  kind: "source_unavailable",
  last_successful_stage: "contract_activation",
  next_automatic_action: "retry_transient_attempt",
  opened_at: "2026-09-12T20:00:00Z",
  recovery_recorded: false,
  revision: 3,
  updated_at: "2026-09-12T20:01:00Z",
  user_impact: "The latest source interval is not available yet.",
}

const session: SessionView = {
  active_role: "data_architect",
  actor: {display_name: "Architect"},
  csrf_token: "csrf-token-with-at-least-thirty-two-characters",
  roles: ["data_architect"],
  tenant: {display_name: "Tenant A", ref: "tenant-a"},
  workspace: {display_name: "Workspace A", ref: "workspace-a"},
}

function incidentEnvelope(value: IncidentView): ConsoleEnvelopeIncidentView {
  return {
    data: value,
    meta: {correlation_id: "correlation-operations", data_provenance: "governed_local"},
  }
}

function incidentsEnvelope(values: readonly IncidentView[]): ConsoleEnvelopeIncidentsView {
  return {
    data: {incidents: [...values]},
    meta: {correlation_id: "correlation-operations", data_provenance: "governed_local"},
  }
}

describe("OperationsPage", () => {
  test("shows safe operational context without internal references", async () => {
    const client = {
      getIncidents: vi.fn(async () => incidentsEnvelope([incident])),
      recoverIncident: vi.fn(),
    } satisfies OperationsClient

    render(<OperationsPage client={client} session={session} />)

    expect(await screen.findByRole("heading", {name: "Source unavailable"})).toBeVisible()
    expect(screen.getByText("Source extraction")).toBeVisible()
    expect(screen.getByText("Temporary failure")).toBeVisible()
    expect(screen.getByText(incident.user_impact)).toBeVisible()
    expect(screen.getByText(/will retry the failed attempt automatically/i)).toBeVisible()
    expect(screen.queryByText(incident.incident_id)).toBeNull()
    expect(screen.queryByText(/attempt:private|evidence:private|run-/)).toBeNull()
  })

  test("requires a reason and sends the admitted action with current revision", async () => {
    const user = userEvent.setup()
    const recovered = {
      ...incident,
      allowed_operator_actions: [],
      next_automatic_action: null,
      recovery_recorded: true,
    } satisfies IncidentView
    const recoverIncident = vi.fn(async () => incidentEnvelope(recovered))
    const client = {
      getIncidents: vi.fn(async () => incidentsEnvelope([incident])),
      recoverIncident,
    } satisfies OperationsClient

    render(
      <OperationsPage
        client={client}
        idempotencyKeyFactory={() => "incident-idempotency-a"}
        session={session}
      />,
    )

    const action = await screen.findByRole("button", {name: "Retry failed attempt"})
    expect(action).toBeDisabled()
    await user.type(screen.getByLabelText("Reason for this action"), "Source access restored.")
    await user.click(action)

    await waitFor(() =>
      expect(recoverIncident).toHaveBeenCalledWith(
        incident.incident_id,
        {
          action: "retry_transient_attempt",
          active_role: "data_architect",
          expected_revision: 3,
          reason: "Source access restored.",
        },
        {
          csrfToken: session.csrf_token,
          idempotencyKey: "incident-idempotency-a",
        },
      ),
    )
    expect(await screen.findByText("Recovery request recorded.")).toBeVisible()
    expect(screen.queryByRole("button", {name: "Retry failed attempt"})).toBeNull()
  })

  test("does not render recovery controls for unsupported owner actions", async () => {
    const unsupported = {
      ...incident,
      allowed_operator_actions: [],
      classification: "no_valid_plan",
      kind: "no_valid_plan",
      next_automatic_action: null,
    } satisfies IncidentView
    const client = {
      getIncidents: vi.fn(async () => incidentsEnvelope([unsupported])),
      recoverIncident: vi.fn(),
    } satisfies OperationsClient

    render(<OperationsPage client={client} session={session} />)

    expect(await screen.findByRole("heading", {name: "No valid plan"})).toBeVisible()
    expect(screen.queryByLabelText("Reason for this action")).toBeNull()
  })

  test("names the bounded cancellation action in user language", async () => {
    const cancellable = {
      ...incident,
      allowed_operator_actions: ["cancel_unstarted_work"],
      next_automatic_action: null,
    } satisfies IncidentView
    const client = {
      getIncidents: vi.fn(async () => incidentsEnvelope([cancellable])),
      recoverIncident: vi.fn(),
    } satisfies OperationsClient

    render(<OperationsPage client={client} session={session} />)

    expect(
      await screen.findByRole("button", {name: "Cancel work before it starts"}),
    ).toBeDisabled()
  })

  test("offers owner-authorized catalog reconciliation in user language", async () => {
    const pendingPublication = {
      ...incident,
      allowed_operator_actions: ["reconcile_external_effect"],
      classification: "ambiguous_outcome",
      failed_stage: "catalog_publication",
      kind: "catalog_pending",
      last_successful_stage: "transform",
      next_automatic_action: "reconcile_external_effect",
    } satisfies IncidentView
    const client = {
      getIncidents: vi.fn(async () => incidentsEnvelope([pendingPublication])),
      recoverIncident: vi.fn(),
    } satisfies OperationsClient

    render(<OperationsPage client={client} session={session} />)

    expect(
      await screen.findByRole("button", {name: "Reconcile catalog publication"}),
    ).toBeDisabled()
  })
})
