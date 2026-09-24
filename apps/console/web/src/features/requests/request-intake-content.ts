import type {RequestInput} from "../../api/generated"

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

// Mirrors request-management RequestIntakeContent and contract-model canonical_bytes:
// sorted keys, UTF-8 JSON, array order preserved, UTC with six fractional digits.
export function canonicalRequestIntakeContent(title: string, request: RequestInput): string {
  const payload = request.kind === "stakeholder_question"
    ? {
        purpose: request.purpose,
        question: request.question,
        request_type: request.kind,
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
