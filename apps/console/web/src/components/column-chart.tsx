/**
 * One measure across a handful of named slices, drawn rather than listed.
 *
 * The console's answers arrived as a table and nothing else: a stakeholder who asked what the
 * daily order value is read three numbers and compared them in their head. A column does that
 * comparison for them, and the table underneath stays exactly as it was -- the drawing is the
 * second reading of the same rows, never the only one, so nothing here is the accessible copy
 * of anything. That is why the figure is marked `aria-hidden` and carries no focus stop: a
 * screen reader and a keyboard both go to the table, which holds every value at full precision.
 *
 * Bars are HTML rather than SVG on purpose. An `svg` that scales to its column scales its text
 * with it, and the labels here sit next to body copy that must not shear; a block whose height
 * is a percentage of its track needs no measurement pass and no resize observer to stay right.
 */
import type {CSSProperties} from "react"

export interface ColumnDatum {
  /** The slice's name, as it will read under its column. */
  readonly label: string
  /** Already formatted for display, so the caller keeps its own precision rules. */
  readonly display: string
  readonly value: number
}

interface ColumnChartProps {
  readonly columns: readonly ColumnDatum[]
  /** The measure's name, shown once above the plot rather than on every column. */
  readonly measure: string
}

/** Rounds up to 1, 2 or 5 times a power of ten, so the top gridline is a number worth reading. */
function niceCeiling(value: number): number {
  if (value <= 0) {
    return 1
  }
  const magnitude = 10 ** Math.floor(Math.log10(value))
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (value <= step * magnitude) {
      return step * magnitude
    }
  }
  return 10 * magnitude
}

function tickLabel(value: number): string {
  if (value >= 1_000_000) {
    return `${(value / 1_000_000).toLocaleString(undefined, {maximumFractionDigits: 1})}M`
  }
  if (value >= 1_000) {
    return `${(value / 1_000).toLocaleString(undefined, {maximumFractionDigits: 1})}K`
  }
  return value.toLocaleString(undefined, {maximumFractionDigits: 2})
}

export function ColumnChart({columns, measure}: ColumnChartProps) {
  const ceiling = niceCeiling(Math.max(...columns.map((column) => column.value), 0))
  // Only the tallest column is labelled. A number on every cap is the thing that makes a small
  // chart unreadable, and every value is in the table a few lines below in any case.
  const peak = columns.reduce(
    (highest, column, index) => (column.value > (columns[highest]?.value ?? -Infinity) ? index : highest),
    0,
  )
  return (
    <figure
      aria-hidden="true"
      className="column-chart"
      // Width follows the column count. A fixed measure makes three columns drift apart with a
      // lane of empty track each, and a dozen crowd; roughly eight rems of slot per column
      // keeps the same rhythm either way, up to the measure the pane will carry.
      style={{maxWidth: `min(100%, ${4 + columns.length * 8}rem)`}}
    >
      <figcaption className="column-chart__measure">{measure}</figcaption>
      <div className="column-chart__plot">
        <div className="column-chart__scale">
          {/*
            Each tick is placed against its own rule, which takes it out of flow and leaves the
            column with no width of its own. These are the same labels left in flow and hidden,
            purely so the column is as wide as its widest tick without a guessed figure.
          */}
          <span className="column-chart__gauge">
            {[1, 0.5, 0].map((fraction) => (
              <span key={fraction}>{tickLabel(ceiling * fraction)}</span>
            ))}
          </span>
          {[1, 0.5, 0].map((fraction) => (
            <span
              className="column-chart__tick"
              key={fraction}
              style={{bottom: `${fraction * 100}%`}}
            >
              {tickLabel(ceiling * fraction)}
            </span>
          ))}
        </div>
        <div className="column-chart__track">
          {[0, 0.5, 1].map((fraction) => (
            <span
              className="column-chart__rule"
              key={fraction}
              style={{bottom: `${fraction * 100}%`}}
            />
          ))}
          <ol className="column-chart__columns">
            {columns.map((column, index) => (
              <li className="column-chart__column" key={`${column.label}:${index}`}>
                <span className="column-chart__value">
                  {index === peak ? column.display : ""}
                </span>
                <span
                  className="column-chart__bar"
                  style={
                    {
                      "--column-height": `${(column.value / ceiling) * 100}%`,
                      "--column-delay": `${index * 60}ms`,
                    } as CSSProperties
                  }
                >
                  {/* Shown on hover, because a column the reader is pointing at should say
                      what it is without making them find its row in the table. */}
                  <span className="column-chart__tip">
                    {column.label} · {column.display}
                  </span>
                </span>
              </li>
            ))}
          </ol>
        </div>
        {/* In the plot's own grid, in the track's column, so a name sits under its column
            however wide the tick labels turn out to be. */}
        <ol className="column-chart__labels">
          {columns.map((column, index) => (
            <li key={`${column.label}:${index}`}>{column.label}</li>
          ))}
        </ol>
      </div>
    </figure>
  )
}
