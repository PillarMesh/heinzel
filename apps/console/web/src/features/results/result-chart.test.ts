import {expect, test} from "vitest"

import {chartForResult} from "./result-chart"
import type {ResultColumn, ResultValue} from "./result-api"

function column(name: string, valueType: ResultColumn["value_type"]): ResultColumn {
  return {label: name, name, value_type: valueType, allowed_operations: []}
}

const DAY = column("ordered_on", "string")
const TOTAL = column("total_order_value", "decimal")

const ROWS: readonly (readonly ResultValue[])[] = [
  ["2026-09-10", "30.000000000"],
  ["2026-09-11", "125.500000000"],
  ["2026-09-12", "99.000000000"],
]

test("a measure over named slices is drawn, at the table's own precision", () => {
  const chart = chartForResult([DAY, TOTAL], ROWS)

  expect(chart).not.toBeNull()
  expect(chart?.measure).toBe("total_order_value")
  expect(chart?.columns.map((entry) => entry.label)).toEqual([
    "2026-09-10",
    "2026-09-11",
    "2026-09-12",
  ])
  // The declared scale is trimmed exactly as the table trims it, so the two never disagree.
  expect(chart?.columns.map((entry) => entry.display)).toEqual(["30.00", "125.50", "99.00"])
  expect(chart?.columns.map((entry) => entry.value)).toEqual([30, 125.5, 99])
})

test("two measures are not drawn on one scale", () => {
  const second = column("order_count", "integer")
  const rows = ROWS.map((row) => [...row, "4"])

  expect(chartForResult([DAY, TOTAL, second], rows)).toBeNull()
})

test("a result with nothing to name its columns by is not drawn", () => {
  expect(chartForResult([TOTAL], [["30.0"], ["125.5"]])).toBeNull()
})

test.each([
  ["a single row, which compares nothing", ROWS.slice(0, 1)],
  ["a value that is not a number", [["2026-09-10", "unavailable"], ["2026-09-11", "2"]]],
  ["a missing value, which would drop a row silently", [["2026-09-10", null], ["2026-09-11", "2"]]],
  ["a value below the baseline", [["2026-09-10", "-4"], ["2026-09-11", "2"]]],
  [
    "values that are all the same, which draws nothing a number does not say",
    [["2026-09-10", "7"], ["2026-09-11", "7"]],
  ],
])("%s is left to the table", (_name, rows) => {
  expect(chartForResult([DAY, TOTAL], rows as readonly (readonly ResultValue[])[])).toBeNull()
})

test("more columns than a reader can compare is left to the table", () => {
  const rows = Array.from({length: 25}, (_unused, index) => [`day-${index}`, String(index + 1)])

  expect(chartForResult([DAY, TOTAL], rows)).toBeNull()
})
