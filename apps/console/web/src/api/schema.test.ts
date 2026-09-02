import { describe, expect, test, vi } from "vitest"

import consoleApiSchema from "../../../schema/console-api-v1.json"
import type {CreateRequestCommand, ResetCommand} from "./generated"
import * as schemaApi from "./schema"
import {ConsoleSchemaValidationError, validateConsoleResponse} from "./schema"

const resetCommand: ResetCommand = {
  expected_revision: 1,
  setup_digest: "a".repeat(64),
  reset_token: "reset_token_fixture_sequence-0000",
  active_role: "data_architect",
}
// @ts-expect-error ResetCommand requires the server-issued reset token.
const resetCommandWithoutToken: ResetCommand = {
  expected_revision: 1,
  setup_digest: "a".repeat(64),
  active_role: "data_architect",
}
// @ts-expect-error ResetCommand must not accept browser-supplied tenant authority.
const resetCommandWithAuthority: ResetCommand = {...resetCommand, tenant_id: "tenant-private"}
// @ts-expect-error CreateRequestCommand requires the explicit initial resource revision.
const createWithoutRevision: CreateRequestCommand = {
  request_digest: "a".repeat(64),
  active_role: "requester",
  title: "Explain revenue",
  request: {
    kind: "stakeholder_question",
    purpose: "Prepare a governed answer.",
    question: "Why did revenue change?",
  },
}
void resetCommand
void resetCommandWithoutToken
void resetCommandWithAuthority
void createWithoutRevision

const sessionEnvelope = {
  meta: {correlation_id: "correlation-session", data_provenance: "demo_fixture"},
  data: {
    actor: {display_name: "Dana Architect"},
    roles: ["data_architect"],
    active_role: "data_architect",
    tenant: {ref: "tenant-demo", display_name: "Northwind Demo"},
    workspace: {ref: "workspace-revenue", display_name: "Revenue to cash"},
    csrf_token: "csrf-token-with-at-least-thirty-two-characters",
  },
}

const validOperationResponse = {
  meta: {
    data_provenance: "governed_local",
    correlation_id: "corr-0001",
  },
  data: {
    operation_id: "op-0001",
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

const validRequestDetailResponse = {
  meta: {
    data_provenance: "governed_local",
    correlation_id: "corr-0001",
  },
  data: {
    request_id: "req-0001",
    kind: "stakeholder_question",
    state: "proposed",
    title: "Revenue trend",
    purpose: "Understand revenue",
    revision: 2,
    proposal_digest: "a".repeat(64),
    proposal: {
      kind: "stakeholder_answer",
      purpose: "Understand revenue",
      candidate: "Revenue increased by ten percent.",
      metric_version: "revenue-v1",
      as_of: "2026-09-01T12:00:00Z",
      freshness: "current",
      quality_limitations: [],
      datasets: [],
      lineage_summary: "Orders to revenue",
      authorization_summary: "Authorized for aggregate revenue",
      required_authorities: [],
    },
    conversation: {
      request_id: "req-0001",
      revision: 2,
      conversation_digest: "c".repeat(64),
      messages: [],
      awaiting_role: null,
    },
    lifecycle: [],
    evidence: {
      datasets: [],
      metric_versions: [],
      as_of: null,
      freshness: "current",
      quality_summary: "No material limitation",
      lineage_summary: "Orders to revenue",
      authorization_summary: "Authorized for aggregate revenue",
      evidence_refs: [],
    },
    available_actions: ["approve"],
  },
}

describe("validateConsoleResponse", () => {
  test("returns a modeled response only after runtime validation", () => {
    const response = validateConsoleResponse("operation_response", validOperationResponse)

    expect(response.data.state).toBe("accepted")
  })

  test("rejects an unknown field before feature use", () => {
    const featureUse = vi.fn()
    const payload = {
      ...validOperationResponse,
      data: {
        ...validOperationResponse.data,
        provider_operation_id: "private-operation-id",
      },
    }

    expect(() => {
      const response = validateConsoleResponse("operation_response", payload)
      featureUse(response.data)
    }).toThrow(ConsoleSchemaValidationError)
    expect(featureUse).not.toHaveBeenCalled()
  })

  test("rejects an invalid enum before feature use", () => {
    const featureUse = vi.fn()
    const payload = {
      ...validOperationResponse,
      data: {...validOperationResponse.data, state: "complete"},
    }

    expect(() => {
      const response = validateConsoleResponse("operation_response", payload)
      featureUse(response.data)
    }).toThrow(ConsoleSchemaValidationError)
    expect(featureUse).not.toHaveBeenCalled()
  })

  test("rejects a session whose selected role is not held while accepting held roles", () => {
    const invalid = {
      ...sessionEnvelope,
      data: {...sessionEnvelope.data, active_role: "data_owner"},
    }
    const valid = {
      ...sessionEnvelope,
      data: {
        ...sessionEnvelope.data,
        roles: ["data_architect", "data_owner"],
        active_role: "data_owner",
      },
    }

    expect(() => validateConsoleResponse("session_response", invalid)).toThrow(
      ConsoleSchemaValidationError,
    )
    expect(validateConsoleResponse("session_response", valid).data.active_role).toBe("data_owner")
  })

  test("rejects incomplete or non-transient retry authority while accepting complete variants", () => {
    const incomplete = {
      ...validOperationResponse,
      data: {
        ...validOperationResponse.data,
        state: "failed",
        failure: {
          code: "transient-failure",
          classification: "transient",
          safe_message: "Retry is safe.",
        },
        recovery_actions: ["retry"],
        operation_digest: "9".repeat(64),
      },
    }
    const permanent = {
      ...incomplete,
      data: {
        ...incomplete.data,
        failure: {...incomplete.data.failure, classification: "permanent"},
        retry_token: `retry_${"7".repeat(58)}`,
      },
    }
    const validRetry = {
      ...incomplete,
      data: {...incomplete.data, retry_token: `retry_${"7".repeat(58)}`},
    }

    expect(() => validateConsoleResponse("operation_response", incomplete)).toThrow(
      ConsoleSchemaValidationError,
    )
    expect(() => validateConsoleResponse("operation_response", permanent)).toThrow(
      ConsoleSchemaValidationError,
    )
    expect(validateConsoleResponse("operation_response", validRetry).data.recovery_actions).toEqual([
      "retry",
    ])
    expect(validateConsoleResponse("operation_response", validOperationResponse).data.state).toBe(
      "accepted",
    )
  })

  test("rejects direct fixture evidence while governed evidence remains valid", () => {
    const fixtureEvidence = {
      ...validOperationResponse,
      meta: {...validOperationResponse.meta, data_provenance: "demo_fixture"},
      data: {...validOperationResponse.data, evidence_ref: "ev-0001"},
    }
    const governedEvidence = {
      ...validOperationResponse,
      data: {...validOperationResponse.data, evidence_ref: "ev-0001"},
    }

    expect(() => validateConsoleResponse("operation_response", fixtureEvidence)).toThrow(
      ConsoleSchemaValidationError,
    )
    expect(validateConsoleResponse("operation_response", governedEvidence).data.evidence_ref).toBe(
      "ev-0001",
    )
  })

  test("rejects deeply nested fixture evidence references while governed evidence remains valid", () => {
    const governedEvidence = {
      ...validRequestDetailResponse,
      data: {
        ...validRequestDetailResponse.data,
        evidence: {...validRequestDetailResponse.data.evidence, evidence_refs: ["ev-0001"]},
      },
    }
    const fixtureEvidence = {
      ...governedEvidence,
      meta: {...governedEvidence.meta, data_provenance: "demo_fixture"},
    }

    expect(() => validateConsoleResponse("request_detail_response", fixtureEvidence)).toThrow(
      ConsoleSchemaValidationError,
    )
    expect(
      validateConsoleResponse("request_detail_response", governedEvidence).data.evidence
        .evidence_refs,
    ).toEqual(["ev-0001"])
  })

  test.each([
    [
      "missing",
      Object.fromEntries(
        Object.entries(validRequestDetailResponse.data.proposal).filter(([key]) => key !== "kind"),
      ),
    ],
    ["unknown", {...validRequestDetailResponse.data.proposal, kind: "unknown"}],
    [
      "mismatched",
      {
        ...validRequestDetailResponse.data.proposal,
        kind: "access_preview",
      },
    ],
  ])("rejects a %s discriminated-union tag", (_case, proposal) => {
    const payload = {
      ...validRequestDetailResponse,
      data: {...validRequestDetailResponse.data, proposal},
    }

    expect(() => validateConsoleResponse("request_detail_response", payload)).toThrow(
      ConsoleSchemaValidationError,
    )
  })

  test("accepts both valid discriminated-union branches", () => {
    const accessPayload = {
      ...validRequestDetailResponse,
      data: {
        ...validRequestDetailResponse.data,
        kind: "data_access",
        proposal: {
          kind: "access_preview",
          purpose: "Investigate revenue",
          data_product_ref: "product-revenue",
          access_mode: "query",
          requested_fields: ["order_id"],
          effective_scope: ["orders.order_id"],
          exclusions: [],
          expires_at: "2026-09-02T12:00:00Z",
          intended_checks: ["can read order_id"],
          denied_checks: ["cannot read payment_token"],
          authority_summary: "Data owner approval required",
          required_authorities: [],
        },
      },
    }

    expect(
      validateConsoleResponse("request_detail_response", validRequestDetailResponse).data.proposal
        ?.kind,
    ).toBe("stakeholder_answer")
    expect(validateConsoleResponse("request_detail_response", accessPayload).data.proposal?.kind).toBe(
      "access_preview",
    )
  })

  test.each([
    "2026-02-31T12:00:00Z",
    "2025-02-29T12:00:00Z",
    "2026-13-01T12:00:00Z",
    "2026-04-31T12:00:00Z",
    "0000-01-01T00:00:00Z",
    "2026-01-01T24:00:00Z",
    "2026-01-01T23:60:00Z",
    "2026-01-01T23:59:60Z",
    "2026-01-01T23:59:59+24:00",
    "2026-01-01T23:59:59+23:60",
    "0001-01-01T00:00:00+23:59",
    "9999-12-31T23:59:59-23:59",
  ])("rejects an invalid RFC 3339 component or UTC rollover: %s", (asOf) => {
    const payload = {
      ...validRequestDetailResponse,
      data: {
        ...validRequestDetailResponse.data,
        proposal: {...validRequestDetailResponse.data.proposal, as_of: asOf},
      },
    }

    expect(() => validateConsoleResponse("request_detail_response", payload)).toThrow(
      ConsoleSchemaValidationError,
    )
  })

  test.each([
    "0001-01-01T00:00:00Z",
    "0001-01-01T23:59:00+23:59",
    "9999-12-31T00:00:59-23:59",
    "9999-12-31T23:59:59.999999+23:59",
    "2024-02-29T23:59:59-23:59",
    "2026-01-01T00:00:00+00:00",
  ])("accepts a valid RFC 3339 component boundary: %s", (asOf) => {
    const payload = {
      ...validRequestDetailResponse,
      data: {
        ...validRequestDetailResponse.data,
        proposal: {
          ...validRequestDetailResponse.data.proposal,
          as_of: asOf,
        },
      },
    }

    expect(validateConsoleResponse("request_detail_response", payload).data.proposal).toMatchObject({
      kind: "stakeholder_answer",
      as_of: asOf,
    })
  })

  test.each([
    ["public ID", {...validOperationResponse.data, operation_id: "OP-0001"}],
    [
      "digest",
      {...validRequestDetailResponse.data, proposal_digest: "A".repeat(64)},
    ],
  ])("rejects a malformed %s", (kind, data) => {
    const responseName = kind === "public ID" ? "operation_response" : "request_detail_response"
    const base = kind === "public ID" ? validOperationResponse : validRequestDetailResponse
    const payload = {...base, data}

    expect(() => validateConsoleResponse(responseName, payload)).toThrow(
      ConsoleSchemaValidationError,
    )
  })

  test("compiles validators for every response declared by the generated schema", () => {
    const expected = Object.keys(consoleApiSchema.properties)
      .filter((name) => name.endsWith("_response"))
      .sort()
    const compiledNames: unknown = Reflect.get(schemaApi, "modeledResponseNames")

    expect(compiledNames).toEqual(expected)
  })
})
