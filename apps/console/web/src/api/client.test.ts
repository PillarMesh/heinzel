import {describe, expect, test, vi} from "vitest"

import type {
  AcquisitionRunNowCommand,
  DecisionCommand,
  IncidentRecoveryCommand,
  WarehouseBindingCommand,
} from "./generated"
import {
  ConsoleApiClient,
  ConsoleApiError,
  ConsoleMutationOutcomeUnknown,
  ConsoleReadTransportError,
  MalformedConsoleResponse,
  type MutationRequestContext,
} from "./client"

const sessionEnvelope = {
  meta: {correlation_id: "correlation-0001", data_provenance: "demo_fixture"},
  data: {
    actor: {display_name: "Dana Architect"},
    roles: ["data_architect"],
    active_role: "data_architect",
    tenant: {ref: "tenant-demo", display_name: "Northwind Demo"},
    workspace: {ref: "workspace-revenue", display_name: "Revenue to cash"},
    csrf_token: "csrf-token-with-at-least-thirty-two-characters",
  },
}

const workspaceEnvelope = {
  meta: {correlation_id: "correlation-0002", data_provenance: "demo_fixture"},
  data: {
    workspace: {ref: "workspace-revenue", display_name: "Revenue to cash"},
    state: "setup",
    capabilities: [
      {
        capability_id: "governed-evidence",
        label: "Governed evidence",
        state: "not_delivered",
        detail: "Fixture mode cannot issue authoritative evidence.",
        dependency: "Governed evidence service wiring",
      },
    ],
    recovery_message: null,
  },
}

const reviewEnvelope = {
  meta: {correlation_id: "correlation-0003", data_provenance: "demo_fixture"},
  data: {
    review_id: "review-activation",
    kind: "activation",
    title: "Activation approval",
    summary: "Review the exact activation boundary.",
    revision: 3,
    reviewed_digest: "a".repeat(64),
    sections: [
      {
        section_id: "section-activation",
        title: "Activation boundary",
        summary: null,
        items: [{label: "Scope", value: "Revenue to cash", material_change: false}],
      },
    ],
    required_authorities: [],
    constraints: [],
    evidence_refs: [],
    decisions: [],
    can_decide: true,
  },
}

const operationEnvelope = {
  meta: {correlation_id: "correlation-0004", data_provenance: "demo_fixture"},
  data: {
    operation_id: "operation-0001",
    revision: 1,
    state: "accepted",
    phase: "provisioning",
    summary: "Provisioning accepted",
    evidence_ref: null,
    failure: null,
    recovery_actions: [],
    operation_digest: null,
    retry_token: null,
  },
}

const incidentEnvelope = {
  meta: {correlation_id: "correlation-incident", data_provenance: "governed_local"},
  data: {
    incident_id: "incident-a",
    revision: 1,
    kind: "source_unavailable",
    classification: "transient",
    last_successful_stage: "contract_activation",
    failed_stage: "extract",
    user_impact: "The latest interval is unavailable.",
    next_automatic_action: "retry_transient_attempt",
    allowed_operator_actions: ["retry_transient_attempt"],
    opened_at: "2026-09-12T20:00:00Z",
    updated_at: "2026-09-12T20:01:00Z",
    recovery_recorded: false,
  },
}

function jsonResponse(payload: unknown, init?: ResponseInit): Response {
  const headers = new Headers({
    "Content-Type": "application/json",
    "X-Correlation-ID": "correlation-header",
    "X-PillarMesh-Data-Provenance": "demo_fixture",
  })
  new Headers(init?.headers).forEach((value, name) => headers.set(name, value))
  return new Response(JSON.stringify(payload), {
    status: init?.status ?? 200,
    headers,
  })
}

function clientReturning(payload: unknown, init?: ResponseInit): ConsoleApiClient {
  const transport: typeof fetch = vi.fn(async () => jsonResponse(payload, init))
  return new ConsoleApiClient({transport})
}

function exportedErrorSurface(value: unknown): string {
  const seen = new Set<object>()
  const parts: string[] = []

  function visit(current: unknown): void {
    if (current === null || (typeof current !== "object" && typeof current !== "function")) {
      parts.push(String(current))
      return
    }
    if (seen.has(current)) {
      return
    }
    seen.add(current)
    parts.push(String(current))
    try {
      parts.push(JSON.stringify(current) ?? "undefined")
    } catch {
      parts.push("unserializable")
    }
    for (const propertyName of Object.getOwnPropertyNames(current)) {
      parts.push(propertyName)
      const descriptor = Object.getOwnPropertyDescriptor(current, propertyName)
      if (descriptor !== undefined && "value" in descriptor) {
        visit(descriptor.value)
      }
    }
  }

  visit(value)
  return parts.join("\n")
}

describe("ConsoleApiClient response boundary", () => {
  test("returns a typed envelope only after the generated validator accepts it", async () => {
    const client = clientReturning(sessionEnvelope)

    const response = await client.getSession()

    expect(response.data.active_role).toBe("data_architect")
    expect(response.meta.data_provenance).toBe("demo_fixture")
  })

  test.each([
    [
      "unknown field",
      {
        ...workspaceEnvelope,
        data: {...workspaceEnvelope.data, provider_resource_id: "private-resource"},
      },
    ],
    ["invalid state", {...workspaceEnvelope, data: {...workspaceEnvelope.data, state: "complete"}}],
    ["missing envelope data", {meta: workspaceEnvelope.meta}],
    ["missing envelope metadata", {data: workspaceEnvelope.data}],
  ])("fails closed for a response with %s", async (_case, payload) => {
    const client = clientReturning(payload)

    await expect(client.getWorkspace()).rejects.toMatchObject({
      name: "MalformedConsoleResponse",
      responseName: "workspace_response",
    })
  })

  test("fails closed when the response body is not JSON", async () => {
    const transport: typeof fetch = vi.fn(async () =>
      new Response("not-json", {status: 200, headers: {"Content-Type": "application/json"}}),
    )
    const client = new ConsoleApiClient({transport})

    await expect(client.getWorkspace()).rejects.toMatchObject({
      name: "MalformedConsoleResponse",
      responseName: "workspace_response",
    })
  })

  test("wraps a rejected read transport without exposing raw transport output", async () => {
    const transport: typeof fetch = vi.fn(async () => {
      throw new TypeError("private network canary")
    })
    const client = new ConsoleApiClient({transport})

    const error = await client.getWorkspace().catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ConsoleReadTransportError)
    expect(error).toMatchObject({responseName: "workspace_response"})
    expect(Object.prototype.hasOwnProperty.call(error, "cause")).toBe(false)
    expect(exportedErrorSurface(error)).not.toContain("private network canary")
  })

  test("removes validator details from every exported malformed-response surface", async () => {
    const client = clientReturning({
      ...workspaceEnvelope,
      data: {
        ...workspaceEnvelope.data,
        validator_private_canary: "validator-value-canary",
      },
    })

    const error = await client.getWorkspace().catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(MalformedConsoleResponse)
    expect(Object.prototype.hasOwnProperty.call(error, "cause")).toBe(false)
    const surface = exportedErrorSurface(error)
    expect(surface).not.toContain("validator_private_canary")
    expect(surface).not.toContain("validator-value-canary")
  })

  test("normalizes an unexpected validator throw without exposing it", async () => {
    const validatorCanary = "unexpected-validator-private-canary"
    const response = jsonResponse({})
    const payload = new Proxy(workspaceEnvelope, {
      get(target, property, receiver) {
        if (property === "data") {
          throw new TypeError(validatorCanary)
        }
        return Reflect.get(target, property, receiver)
      },
    })
    Object.defineProperty(response, "json", {value: async () => payload})
    const transport: typeof fetch = vi.fn(async () => response)
    const client = new ConsoleApiClient({transport})

    const error = await client.getWorkspace().catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(MalformedConsoleResponse)
    expect(Object.prototype.hasOwnProperty.call(error, "cause")).toBe(false)
    expect(exportedErrorSurface(error)).not.toContain(validatorCanary)
  })

  test("rejects a read success status outside the exact 200 contract", async () => {
    const client = clientReturning(workspaceEnvelope, {status: 201})

    await expect(client.getWorkspace()).rejects.toMatchObject({
      name: "MalformedConsoleResponse",
      responseName: "workspace_response",
      status: 201,
    })
  })

  test("preserves only validated response headers on malformed data", async () => {
    const client = clientReturning(
      {
        meta: {
          correlation_id: "body-private-canary",
          data_provenance: "governed_local",
        },
      },
      {
        headers: {
          "X-Correlation-ID": "correlation-safe-header",
          "X-PillarMesh-Data-Provenance": "demo_fixture",
        },
      },
    )

    const error = await client.getWorkspace().catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(MalformedConsoleResponse)
    expect(error).toMatchObject({
      metadata: {
        correlationId: "correlation-safe-header",
        dataProvenance: "demo_fixture",
      },
    })
    expect(JSON.stringify(error)).not.toContain("body-private-canary")
  })

  test("drops malformed header metadata instead of trusting it", async () => {
    const client = clientReturning({meta: workspaceEnvelope.meta}, {
      headers: {
        "X-Correlation-ID": "INVALID PRIVATE HEADER",
        "X-PillarMesh-Data-Provenance": "provider_private",
      },
    })

    await expect(client.getWorkspace()).rejects.toMatchObject({metadata: null})
  })

  test("returns typed stale recovery metadata from a validated 409 envelope", async () => {
    const errorEnvelope = {
      meta: {correlation_id: "correlation-stale", data_provenance: "governed_local"},
      error: {
        code: "stale_revision",
        safe_message: "The review changed after it was loaded.",
        recovery_action: "reload",
        field: "expected_revision",
      },
    }
    const client = clientReturning(errorEnvelope, {status: 409})

    const error = await client
      .getReview("review-activation")
      .then(() => undefined)
      .catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ConsoleApiError)
    expect(error).toMatchObject({
      status: 409,
      code: "stale_revision",
      recoveryAction: "reload",
      correlationId: "correlation-stale",
      field: "expected_revision",
    })
  })

  test("keeps a typed server-unavailable response distinct from malformed data", async () => {
    const errorEnvelope = {
      meta: {correlation_id: "correlation-unavailable", data_provenance: "governed_local"},
      error: {
        code: "service_unavailable",
        safe_message: "The workspace service is temporarily unavailable.",
        recovery_action: "retry",
        field: null,
      },
    }
    const client = clientReturning(errorEnvelope, {status: 503})

    await expect(client.getWorkspace()).rejects.toMatchObject({
      name: "ConsoleApiError",
      status: 503,
      recoveryAction: "retry",
    })
  })

  test("rejects a malformed error envelope instead of exposing raw JSON", async () => {
    const client = clientReturning(
      {
        meta: {correlation_id: "correlation-stale", data_provenance: "governed_local"},
        error: {
          code: "stale_revision",
          safe_message: "The review changed.",
          recovery_action: "refresh-silently",
          private_detail: "provider canary",
        },
      },
      {status: 409},
    )

    await expect(client.getReview("review-activation")).rejects.toBeInstanceOf(
      MalformedConsoleResponse,
    )
  })
})

describe("ConsoleApiClient mutation authority", () => {
  test("starts acquisition with contract authority and no browser tenant", async () => {
    let capturedInput: RequestInfo | URL | undefined
    let capturedInit: RequestInit | undefined
    const transport: typeof fetch = vi.fn(async (input, init) => {
      capturedInput = input
      capturedInit = init
      return jsonResponse({
        data: {
          evidence_id: "evidence-ref:prepared-1",
          contract_ref: "contract:orders:v1",
          source_binding_ref: "source-binding:orders",
          acquisition_mode: "snapshot",
          logical_object_refs: ["orders"],
          outcome: "prepared",
          reason_codes: [],
          created_at: "2026-09-14T12:00:00Z",
        },
        meta: {correlation_id: "correlation-acquisition", data_provenance: "governed_local"},
      })
    })
    const client = new ConsoleApiClient({transport})
    const command: AcquisitionRunNowCommand = {
      acquisition_mode: "snapshot",
      active_role: "data_architect",
      contract_ref: "contract:orders:v1",
      trigger_window: "2026-09-14T12:00:00Z/2026-09-14T13:00:00Z",
    }

    await client.runAcquisitionNow(command, {
      csrfToken: "csrf-token-bound-to-session",
      idempotencyKey: "idempotency-acquisition-0001",
    })

    expect(capturedInput).toBe("/api/v1/acquisitions/run-now")
    expect(JSON.parse(String(capturedInit?.body))).toEqual(command)
    expect(String(capturedInit?.body)).not.toContain("tenant_id")
    expect(String(capturedInit?.body)).not.toContain("source_binding_ref")
  })

  test("sends incident recovery authority without browser tenant or actor", async () => {
    let capturedInput: RequestInfo | URL | undefined
    let capturedInit: RequestInit | undefined
    const transport: typeof fetch = vi.fn(async (input, init) => {
      capturedInput = input
      capturedInit = init
      return jsonResponse(incidentEnvelope)
    })
    const client = new ConsoleApiClient({transport})
    const command: IncidentRecoveryCommand = {
      expected_revision: 1,
      action: "retry_transient_attempt",
      active_role: "data_architect",
      reason: "Source access restored.",
    }

    await client.recoverIncident("incident-a", command, {
      csrfToken: "csrf-token-bound-to-session",
      idempotencyKey: "idempotency-incident-0001",
    })

    expect(capturedInput).toBe("/api/v1/incidents/incident-a/recovery")
    expect(JSON.parse(String(capturedInit?.body))).toEqual(command)
    expect(String(capturedInit?.body)).not.toContain("tenant_id")
    expect(String(capturedInit?.body)).not.toContain("actor_id")
  })

  test("sends exact review authority and replay material without browser tenant or actor", async () => {
    let capturedInput: RequestInfo | URL | undefined
    let capturedInit: RequestInit | undefined
    const transport: typeof fetch = vi.fn(async (input, init) => {
      capturedInput = input
      capturedInit = init
      return jsonResponse(reviewEnvelope)
    })
    const client = new ConsoleApiClient({transport})
    const command: DecisionCommand = {
      expected_revision: 3,
      reviewed_digest: "a".repeat(64),
      active_role: "budget_approver",
      decision: "approve",
    }
    const context: MutationRequestContext = {
      csrfToken: "csrf-token-bound-to-session",
      idempotencyKey: "idempotency-review-0001",
    }

    const response = await client.decideReview("review-activation", command, context)

    expect(response.data.review_id).toBe("review-activation")
    expect(capturedInput).toBe("/api/v1/reviews/review-activation/decisions")
    expect(capturedInit).toMatchObject({method: "POST", credentials: "same-origin"})
    expect(new Headers(capturedInit?.headers)).toEqual(
      new Headers({
        "Content-Type": "application/json",
        "Idempotency-Key": "idempotency-review-0001",
        "X-CSRF-Token": "csrf-token-bound-to-session",
      }),
    )
    expect(JSON.parse(String(capturedInit?.body))).toEqual({
      expected_revision: 3,
      reviewed_digest: "a".repeat(64),
      active_role: "budget_approver",
      decision: "approve",
    })
    expect(String(capturedInit?.body)).not.toContain("tenant_id")
    expect(String(capturedInit?.body)).not.toContain("actor_id")
  })

  test("validates an accepted operation returned by a warehouse mutation", async () => {
    const client = clientReturning(operationEnvelope, {status: 202})
    const command: WarehouseBindingCommand = {
      expected_revision: 1,
      reviewed_digest: "b".repeat(64),
      active_role: "data_architect",
      engine: "postgresql",
      region: "us-west-2",
      capacity: "fixed-small",
    }

    const response = await client.confirmWarehouseBinding(command, {
      csrfToken: "csrf-token-bound-to-session",
      idempotencyKey: "idempotency-warehouse-0001",
    })

    expect(response).toMatchObject({kind: "pending", state: "accepted", status: 202})
    expect(response.envelope.data.state).toBe("accepted")
  })

  test.each([
    [202, "accepted", "pending"],
    [202, "running", "pending"],
    [202, "outcome_unknown", "outcome_unknown"],
    [200, "succeeded", "terminal"],
    [200, "failed", "terminal"],
  ] as const)("returns status %i and state %s as a discriminated %s operation submission", async (
    status,
    state,
    kind,
  ) => {
    const client = clientReturning(
      {...operationEnvelope, data: {...operationEnvelope.data, state}},
      {status},
    )
    const command: WarehouseBindingCommand = {
      expected_revision: 1,
      reviewed_digest: "b".repeat(64),
      active_role: "data_architect",
      engine: "postgresql",
      region: "us-west-2",
      capacity: "fixed-small",
    }

    const result = await client.confirmWarehouseBinding(command, {
      csrfToken: "csrf-token-bound-to-session",
      idempotencyKey: `idempotency-operation-${state}`,
    })

    expect(result.kind).toBe(kind)
    expect(result.state).toBe(state)
    expect(result.status).toBe(status)
  })

  test.each([
    [200, "accepted"],
    [200, "running"],
    [200, "outcome_unknown"],
    [202, "succeeded"],
    [202, "failed"],
  ] as const)("rejects contradictory operation status %i and state %s", async (status, state) => {
    const client = clientReturning(
      {...operationEnvelope, data: {...operationEnvelope.data, state}},
      {status},
    )
    const idempotencyKey = `idempotency-contradictory-${state}`

    const error = await client
      .confirmWarehouseBinding(
        {
          expected_revision: 1,
          reviewed_digest: "b".repeat(64),
          active_role: "data_architect",
          engine: "postgresql",
          region: "us-west-2",
          capacity: "fixed-small",
        },
        {csrfToken: "csrf-token-bound-to-session", idempotencyKey},
      )
      .catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ConsoleMutationOutcomeUnknown)
    expect(error).toMatchObject({
      idempotencyKey,
      metadata: {
        correlationId: "correlation-header",
        dataProvenance: "demo_fixture",
      },
      responseName: "operation_response",
      status,
    })
  })

  test("rejects an operation submission status outside 200 or 202 with reconciliation context", async () => {
    const client = clientReturning(operationEnvelope, {status: 201})

    const error = await client
      .confirmWarehouseBinding(
        {
          expected_revision: 1,
          reviewed_digest: "b".repeat(64),
          active_role: "data_architect",
          engine: "postgresql",
          region: "us-west-2",
          capacity: "fixed-small",
        },
        {
          csrfToken: "csrf-token-bound-to-session",
          idempotencyKey: "idempotency-operation-invalid-status",
        },
      )
      .catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ConsoleMutationOutcomeUnknown)
    expect(error).toMatchObject({
      idempotencyKey: "idempotency-operation-invalid-status",
      responseName: "operation_response",
      status: 201,
    })
  })

  test("rejects a 202 synchronous command response with the original replay key", async () => {
    const client = clientReturning(reviewEnvelope, {status: 202})

    await expect(
      client.decideReview(
        "review-activation",
        {
          expected_revision: 3,
          reviewed_digest: "a".repeat(64),
          active_role: "data_architect",
          decision: "approve",
        },
        {
          csrfToken: "csrf-token-bound-to-session",
          idempotencyKey: "idempotency-sync-invalid-status",
        },
      ),
    ).rejects.toMatchObject({
      name: "ConsoleMutationOutcomeUnknown",
      idempotencyKey: "idempotency-sync-invalid-status",
      status: 202,
    })
  })

  test("retains the original idempotency key when mutation transport rejects", async () => {
    const transport: typeof fetch = vi.fn(async () => {
      throw new TypeError("private transport failure")
    })
    const client = new ConsoleApiClient({transport})

    const error = await client
      .decideReview(
        "review-activation",
        {
          expected_revision: 3,
          reviewed_digest: "a".repeat(64),
          active_role: "data_architect",
          decision: "approve",
        },
        {
          csrfToken: "csrf-token-bound-to-session",
          idempotencyKey: "idempotency-review-reconcile",
        },
      )
      .catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ConsoleMutationOutcomeUnknown)
    expect(error).toMatchObject({
      idempotencyKey: "idempotency-review-reconcile",
      status: null,
    })
    if (!(error instanceof ConsoleMutationOutcomeUnknown)) {
      throw new Error("expected a typed mutation reconciliation error")
    }
    expect(error.message).toContain("same Idempotency-Key")
    expect(Object.prototype.hasOwnProperty.call(error, "cause")).toBe(false)
    expect(exportedErrorSurface(error)).not.toContain("private transport failure")
  })

  test("retains the original key and validated headers for malformed mutation success", async () => {
    const client = clientReturning(
      {...reviewEnvelope, data: {...reviewEnvelope.data, private_provider_id: "canary"}},
      {headers: {"X-Correlation-ID": "correlation-reconcile"}},
    )

    const error = await client
      .decideReview(
        "review-activation",
        {
          expected_revision: 3,
          reviewed_digest: "a".repeat(64),
          active_role: "data_architect",
          decision: "approve",
        },
        {
          csrfToken: "csrf-token-bound-to-session",
          idempotencyKey: "idempotency-malformed-success",
        },
      )
      .catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ConsoleMutationOutcomeUnknown)
    expect(error).toMatchObject({
      idempotencyKey: "idempotency-malformed-success",
      status: 200,
      metadata: {
        correlationId: "correlation-reconcile",
        dataProvenance: "demo_fixture",
      },
    })
    expect(Object.prototype.hasOwnProperty.call(error, "cause")).toBe(false)
    expect(exportedErrorSurface(error)).not.toContain("private_provider_id")
  })

  test("treats a malformed create-request success as outcome unknown", async () => {
    const client = clientReturning({
      meta: {correlation_id: "correlation-request", data_provenance: "demo_fixture"},
      data: {
        request_id: "request-0001",
        kind: "stakeholder_question",
        state: "submitted",
        title: "Explain synthetic revenue",
        requested_outcome: "Prepare a synthetic board update.",
        revision: 1,
        updated_at: "2026-09-01T16:00:00Z",
        own_decisions: [],
        clarified_outcome: null,
        denial_explanation: null,
        provider_request_id: "private-request",
      },
    })

    await expect(
      client.createRequest(
        {
          expected_revision: 1,
          request_digest: "c".repeat(64),
          active_role: "requester",
          title: "Explain synthetic revenue",
          request: {
            kind: "stakeholder_question",
            purpose: "Prepare a synthetic board update.",
            question: "Why did synthetic revenue change?",
          },
        },
        {
          csrfToken: "csrf-token-bound-to-session",
          idempotencyKey: "idempotency-request-0001",
        },
      ),
    ).rejects.toMatchObject({
      name: "ConsoleMutationOutcomeUnknown",
      responseName: "requester_request_response",
      idempotencyKey: "idempotency-request-0001",
    })
  })

  test("validates create request against its exact generated response", async () => {
    const responseEnvelope = {
      meta: {correlation_id: "correlation-request", data_provenance: "demo_fixture"},
      data: {
        request_id: "request-0001",
        kind: "stakeholder_question",
        state: "submitted",
        title: "Explain synthetic revenue",
        requested_outcome: "Prepare a synthetic board update.",
        revision: 1,
        updated_at: "2026-09-01T16:00:00Z",
        own_decisions: [],
        clarified_outcome: null,
        denial_explanation: null,
      },
    }
    const client = clientReturning(responseEnvelope)

    const response = await client.createRequest(
      {
        expected_revision: 1,
        request_digest: "c".repeat(64),
        active_role: "requester",
        title: "Explain synthetic revenue",
        request: {
          kind: "stakeholder_question",
          purpose: "Prepare a synthetic board update.",
          question: "Why did synthetic revenue change?",
        },
      },
      {
        csrfToken: "csrf-token-bound-to-session",
        idempotencyKey: "idempotency-request-exact",
      },
    )

    expect(response.data.request_id).toBe("request-0001")
  })

  test("sends exact reset snapshot authority without browser tenant or actor", async () => {
    let capturedInit: RequestInit | undefined
    const setupEnvelope = {
      meta: {correlation_id: "correlation-setup", data_provenance: "demo_fixture"},
      data: {
        workspace_ref: "workspace-revenue",
        revision: 1,
        setup_digest: "a".repeat(64),
        reset_token: "reset_token_fixture_sequence-0001",
        active_stage: "foundation",
        stages: [{stage: "foundation", label: "Foundation", state: "current", detail: null}],
        warehouse_options: [
          {
            engine: "postgresql",
            label: "PostgreSQL",
            supported_region: "us-west-2",
            fixed_capacity: "fixed-small",
          },
        ],
        warehouse_binding: null,
        managed_services: [],
        sources: [],
        process_package: null,
        pending_review_refs: [],
      },
    }
    const transport: typeof fetch = vi.fn(async (_input, init) => {
      capturedInit = init
      return jsonResponse(setupEnvelope)
    })
    const client = new ConsoleApiClient({transport})

    await client.resetDemo(
      {
        expected_revision: 4,
        setup_digest: "f".repeat(64),
        reset_token: "reset_token_fixture_sequence-0004",
        active_role: "data_architect",
      },
      {
        csrfToken: "csrf-token-bound-to-session",
        idempotencyKey: "idempotency-reset-0001",
      },
    )

    expect(JSON.parse(String(capturedInit?.body))).toEqual({
      expected_revision: 4,
      setup_digest: "f".repeat(64),
      reset_token: "reset_token_fixture_sequence-0004",
      active_role: "data_architect",
    })
    expect(new Headers(capturedInit?.headers).get("Idempotency-Key")).toBe(
      "idempotency-reset-0001",
    )
    expect(String(capturedInit?.body)).not.toMatch(/tenant_id|actor_id/)
  })
})

type WorkspaceParameters = Parameters<ConsoleApiClient["getWorkspace"]>
const workspaceParametersWithoutAuthority: WorkspaceParameters = []
// @ts-expect-error Browser reads never accept tenant authority.
const workspaceParametersWithTenant: WorkspaceParameters = [{tenant_id: "tenant-private"}]
type ReviewDecisionParameters = Parameters<ConsoleApiClient["decideReview"]>
const reviewDecisionWithActor: ReviewDecisionParameters = [
  "review-activation",
  {
    expected_revision: 3,
    reviewed_digest: "a".repeat(64),
    active_role: "budget_approver",
    decision: "approve",
  },
  {
    csrfToken: "csrf-token-bound-to-session",
    idempotencyKey: "idempotency-review-0001",
    // @ts-expect-error Mutation methods never accept actor authority outside the generated command.
    actor_id: "actor-private",
  },
]
void workspaceParametersWithoutAuthority
void workspaceParametersWithTenant
void reviewDecisionWithActor
