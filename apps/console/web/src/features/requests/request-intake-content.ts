import type {QuestionTermSelectionInput, RequestInput} from "../../api/generated"

function canonicalExpiry(value: string): string {
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$/.exec(value)
  if (match === null) {
    throw new Error("Request expiry must be an RFC 3339 timestamp.")
  }
  // Normalize the whole seconds separately so Date cannot discard microseconds.
  const seconds = new Date(`${match[1]}${match[3]}`).toISOString()
  const fraction = (match[2] ?? "").slice(0, 6).padEnd(6, "0")
  return seconds.replace(".000Z", `.${fraction}Z`)
}

// Mirrors request-management's QuestionTermSelection, whose `schema_version` the service supplies
// and the digest therefore covers. A selection the browser sent without it would be rebuilt by the
// service with it and refused as a digest mismatch, so it is written here rather than omitted.
//
// An absent selection contributes nothing at all: the artifact excludes the field when it is
// unset, so a question with no selection digests to exactly what it digested to before selections
// existed.
function canonicalSelection(
  selection: QuestionTermSelectionInput,
): Record<string, unknown> {
  return {
    dimension_refs: selection.dimension_refs,
    metric_ref: selection.metric_ref,
    schema_version: "1",
  }
}

// Mirrors request-management RequestIntakeContent and contract-model canonical_bytes:
// sorted keys, UTF-8 JSON, array order preserved, UTC with six fractional digits.
export function canonicalRequestIntakeContent(title: string, request: RequestInput): string {
  const payload = request.kind === "stakeholder_question"
    ? {
        purpose: request.purpose,
        question: request.question,
        request_type: request.kind,
        ...(request.selection === null || request.selection === undefined
          ? {}
          : {selection: canonicalSelection(request.selection)}),
      }
    : {
        access_mode: request.access_mode,
        data_product_id: request.data_product_ref,
        expires_at: canonicalExpiry(request.expires_at),
        purpose: request.purpose,
        request_type: request.kind,
        requested_fields: request.requested_fields,
      }
  return JSON.stringify({payload, title})
}
