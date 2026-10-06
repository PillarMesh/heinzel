import {expect, test} from "vitest"

import {canonicalRequestIntakeContent} from "./request-intake-content"

test("preserves Unicode and escaped control characters in sorted owning fields", () => {
  expect(canonicalRequestIntakeContent("Weekly 📊", {
    kind: "stakeholder_question", purpose: "Review café ☕", question: 'Why?\n"Revenue"',
  })).toBe('{"payload":{"purpose":"Review café ☕","question":"Why?\\n\\"Revenue\\"","request_type":"stakeholder_question"},"title":"Weekly 📊"}')
})

test.each([
  ["2026-09-15T12:30:00.123456Z", "2026-09-15T12:30:00.123456Z"],
  ["2026-09-15T12:30:00.123Z", "2026-09-15T12:30:00.123000Z"],
  ["2026-09-15T12:30:00Z", "2026-09-15T12:30:00.000000Z"],
  ["2026-09-15T14:30:00.123456+02:00", "2026-09-15T12:30:00.123456Z"],
  ["2026-09-15T12:30:00.1234567Z", "2026-09-15T12:30:00.123456Z"],
  ["2026-09-15T12:30:00+00:00", "2026-09-15T12:30:00.000000Z"],
])("normalizes %s to the service microsecond representation", (expires_at, canonicalExpiry) => {
  expect(canonicalRequestIntakeContent("Weekly 📊", {
    kind: "data_access", purpose: "Review café ☕", data_product_ref: "product-revenue",
    requested_fields: ["total", "date"], access_mode: "export", expires_at,
  })).toBe(`{"payload":{"access_mode":"export","data_product_id":"product-revenue","expires_at":"${canonicalExpiry}","purpose":"Review café ☕","request_type":"data_access","requested_fields":["total","date"]},"title":"Weekly 📊"}`)
})

test("binds the selected governed terms, including the artifact's own schema version", () => {
  expect(canonicalRequestIntakeContent("Weekly 📊", {
    kind: "stakeholder_question", purpose: "Review café ☕", question: 'Why?\n"Revenue"',
    selection: {metric_ref: "daily-order-value", dimension_refs: ["order_day", "order_region"]},
  })).toBe('{"payload":{"purpose":"Review café ☕","question":"Why?\\n\\"Revenue\\"","request_type":"stakeholder_question","selection":{"dimension_refs":["order_day","order_region"],"metric_ref":"daily-order-value","schema_version":"1"}},"title":"Weekly 📊"}')
})

const unselectedQuestion = '{"payload":{"purpose":"Review café ☕","question":"Why?\\n\\"Revenue\\"","request_type":"stakeholder_question"},"title":"Weekly 📊"}'

test("omits an absent selection entirely, as the artifact does", () => {
  expect(canonicalRequestIntakeContent("Weekly 📊", {
    kind: "stakeholder_question", purpose: "Review café ☕", question: 'Why?\n"Revenue"',
  })).toBe(unselectedQuestion)
})

test("omits a null selection entirely, so an unset builder digests as it always did", () => {
  expect(canonicalRequestIntakeContent("Weekly 📊", {
    kind: "stakeholder_question", purpose: "Review café ☕", question: 'Why?\n"Revenue"',
    selection: null,
  })).toBe(unselectedQuestion)
})
