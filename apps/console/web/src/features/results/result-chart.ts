/**
 * Whether a governed result can be drawn, and as what.
 *
 * A console that draws whatever it is handed will eventually draw something untrue: two
 * measures on one scale, a category axis of forty thousand identifiers, a bar chart of values
 * that go below zero with the baseline left at zero. So the shape is checked first and the
 * answer is often `null`, which is not a failure -- the table was always the result, and the
 * figure is only offered where it says the same thing faster.
 */
import {displayDecimal} from "../../format/decimal"
import type {ColumnDatum} from "../../components/column-chart"
import type {ResultColumn, ResultValue} from "./result-api"

/** Beyond this a column chart is a texture rather than a comparison. */
const MOST_COLUMNS = 24

export interface ResultChart {
  readonly columns: readonly ColumnDatum[]
  readonly measure: string
}

function isNumeric(column: ResultColumn): boolean {
  return column.value_type === "decimal" || column.value_type === "integer"
}

/** A cell as a number, or `null` where it is not one this chart may plot. */
function asValue(cell: ResultValue): number | null {
  if (typeof cell === "number") {
    return Number.isFinite(cell) ? cell : null
  }
  if (typeof cell !== "string") {
    return null
  }
  const parsed = Number(cell)
  return cell.trim() !== "" && Number.isFinite(parsed) ? parsed : null
}

function asLabel(cell: ResultValue): string | null {
  if (typeof cell === "string") {
    return cell
  }
  if (typeof cell === "number" || typeof cell === "boolean") {
    return String(cell)
  }
  return null
}

export function chartForResult(
  columns: readonly ResultColumn[],
  rows: readonly (readonly ResultValue[])[],
): ResultChart | null {
  // Exactly one measure. Two would want two scales, and one scale for both would be a lie
  // about their magnitudes; the honest answer for that shape is the table.
  const measureIndex = columns.findIndex(isNumeric)
  if (measureIndex === -1 || columns.filter(isNumeric).length !== 1) {
    return null
  }
  // Something to name each column by. The first non-measure column is the one the compiler
  // grouped on, because the group key leads the projection it emitted.
  const labelIndex = columns.findIndex((column, index) => index !== measureIndex && !isNumeric(column))
  if (labelIndex === -1) {
    return null
  }
  if (rows.length < 2 || rows.length > MOST_COLUMNS) {
    return null
  }

  const drawn: ColumnDatum[] = []
  for (const row of rows) {
    const value = asValue(row[measureIndex] ?? null)
    const label = asLabel(row[labelIndex] ?? null)
    // One unplottable cell and the figure goes: a chart silently missing a row is worse than
    // no chart, because nothing on screen says a row is missing.
    if (value === null || label === null || value < 0) {
      return null
    }
    const cell = row[measureIndex]
    drawn.push({
      label,
      display: typeof cell === "string" ? displayDecimal(cell) : value.toLocaleString(),
      value,
    })
  }
  // Every value the same height is a row of identical blocks that says nothing a number does
  // not, so it is not worth the space.
  if (drawn.every((column) => column.value === drawn[0]?.value)) {
    return null
  }
  return {columns: drawn, measure: columns[measureIndex]?.label ?? "Value"}
}
