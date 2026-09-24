import type {ResultColumn, ResultValue} from "./result-api"

interface ResultTableProps {
  readonly columns: readonly ResultColumn[]
  readonly rows: readonly (readonly ResultValue[])[]
}

function displayDecimal(value: string): string {
  if (!/^-?\d+\.\d+$/.test(value)) return value
  const [integer, fraction] = value.split(".") as [string, string]
  let visibleFraction = fraction
  while (visibleFraction.length > 2 && visibleFraction.endsWith("0")) {
    visibleFraction = visibleFraction.slice(0, -1)
  }
  return `${integer}.${visibleFraction}`
}

function ResultCell({
  value,
  valueType,
}: {
  readonly value: ResultValue
  readonly valueType: ResultColumn["value_type"]
}) {
  if (value === null) return <span aria-label="No value">—</span>
  if (typeof value === "boolean") return value ? "Yes" : "No"
  if (valueType === "decimal" && typeof value === "string") return displayDecimal(value)
  return value
}

export function ResultTable({columns, rows}: ResultTableProps) {
  return (
    <div className="result-table-frame">
      <table aria-label="Result rows" className="result-table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th aria-label={column.label} key={column.name} scope="col">
                <span>{column.label}</span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {columns.map((column, columnIndex) => (
                <td data-value-type={column.value_type} key={column.name}>
                  <ResultCell value={row[columnIndex] ?? null} valueType={column.value_type} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
