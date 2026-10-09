import {render, screen, waitFor, within} from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import {expect, test, vi} from "vitest"

import {ConsoleApiError} from "../../api/client"
import {ResultPage, type ResultClient, type ResultView} from "./result-page"

const result: ResultView = {
  request_id: "request-00000000000000000001",
  title: "Weekly net revenue movement",
  status: "available",
  answer_text: "Net revenue was 1250.50 for the selected week.",
  as_of: "2026-09-11T12:00:00Z",
  freshness: "current",
  row_count: 2,
  columns: [
    {name: "region", label: "Region", value_type: "string", allowed_operations: []},
    {name: "net_revenue", label: "Net revenue", value_type: "decimal", allowed_operations: []},
  ],
  rows: [
    ["North", "1000.25"],
    ["South", "250.25"],
  ],
  next_cursor: null,
  technical_details: {
    plan_digest: "a".repeat(64),
    execution_receipt_id: "answer-receipt-00000000000000000001",
    result_digest: "b".repeat(64),
    result_schema_digest: "c".repeat(64),
  },
}

function client(value: ResultView = result): ResultClient {
  return {getResult: vi.fn().mockResolvedValue(value)}
}

test("shows a completed result as a titled typed table without exposing internals", async () => {
  render(<ResultPage client={client()} requestId={result.request_id} />)

  expect(await screen.findByRole("heading", {name: result.title})).toBeVisible()
  expect(screen.getByText(result.answer_text!)).toBeVisible()
  const table = screen.getByRole("table", {name: "Result rows"})
  expect(within(table).getByRole("columnheader", {name: "Region"})).toBeVisible()
  expect(within(table).getByRole("cell", {name: "1000.25"})).toBeVisible()
  expect(screen.queryByRole("button", {name: /filter|sort/i})).not.toBeInTheDocument()
  expect(screen.getByRole("link", {name: "Download CSV"})).toHaveAttribute(
    "href",
    "/api/v1/requests/request-00000000000000000001/result.csv",
  )
  expect(screen.getByText(result.request_id)).not.toBeVisible()
  expect(screen.getByText(result.technical_details!.plan_digest)).not.toBeVisible()
})

test("shows exact decimal strings without redundant storage-scale zeroes", async () => {
  render(
    <ResultPage
      client={client({
        ...result,
        rows: [
          ["Whole", "99.000000000"],
          ["Fractional", "1.234500000"],
          ["Small", "0.000000001"],
        ],
        row_count: 3,
      })}
      requestId={result.request_id}
    />,
  )

  const table = await screen.findByRole("table", {name: "Result rows"})
  expect(within(table).getByRole("cell", {name: "99.00"})).toBeVisible()
  expect(within(table).getByRole("cell", {name: "1.2345"})).toBeVisible()
  expect(within(table).getByRole("cell", {name: "0.000000001"})).toBeVisible()
})

test("shows an explicit empty result", async () => {
  render(
    <ResultPage
      client={client({...result, row_count: 0, rows: [], answer_text: "No matching facts."})}
      requestId={result.request_id}
    />,
  )

  expect(await screen.findByText("No rows matched this governed question.")).toBeVisible()
  expect(screen.queryByRole("table")).not.toBeInTheDocument()
})

test("loads additional rows from the server cursor", async () => {
  const getResult = vi
    .fn()
    .mockResolvedValueOnce({...result, rows: [["North", "1000.25"]], next_cursor: "page-2"})
    .mockResolvedValueOnce({...result, rows: [["South", "250.25"]], next_cursor: null})
  const user = userEvent.setup()

  render(
    <ResultPage
      client={{getResult}}
      requestId={result.request_id}
    />,
  )

  await user.click(await screen.findByRole("button", {name: "Load more rows"}))

  expect(await screen.findByRole("cell", {name: "South"})).toBeVisible()
  expect(screen.getByRole("cell", {name: "North"})).toBeVisible()
  expect(getResult).toHaveBeenNthCalledWith(
    2,
    result.request_id,
    "page-2",
  )
  expect(screen.queryByRole("button", {name: "Load more rows"})).not.toBeInTheDocument()
})

test("shows loading before the result arrives", () => {
  render(
    <ResultPage
      client={{getResult: vi.fn().mockReturnValue(new Promise(() => undefined))}}
      requestId={result.request_id}
    />,
  )

  expect(screen.getByRole("status")).toHaveTextContent("Loading your result")
})

test("shows expired access without revealing whether another result exists", async () => {
  render(<ResultPage client={client({...result, status: "expired", rows: []})} requestId={result.request_id} />)

  expect(await screen.findByText("This result has expired.")).toBeVisible()
  expect(screen.queryByRole("table")).not.toBeInTheDocument()
})

test("uses the same unavailable state for denied and missing results", async () => {
  const unavailable = new ConsoleApiError(404, {
    error: {
      code: "result_unavailable",
      safe_message: "The requested result is unavailable.",
      recovery_action: "none",
    },
    meta: {data_provenance: "governed_local", correlation_id: "correlation-result"},
  } as never)

  render(
    <ResultPage client={{getResult: vi.fn().mockRejectedValue(unavailable)}} requestId={result.request_id} />,
  )

  expect(await screen.findByRole("alert")).toHaveTextContent("The requested result is unavailable.")
})

test("shows a failed result without an empty table", async () => {
  render(<ResultPage client={client({...result, status: "failed", rows: []})} requestId={result.request_id} />)

  expect(await screen.findByRole("alert")).toHaveTextContent("This result could not be completed.")
  expect(screen.queryByRole("table")).not.toBeInTheDocument()
})

test("reports a malformed or failed result read safely", async () => {
  render(
    <ResultPage
      client={{getResult: vi.fn().mockRejectedValue(new Error("raw provider failure"))}}
      requestId={result.request_id}
    />,
  )

  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("could not be displayed safely"))
  expect(screen.queryByText("raw provider failure")).not.toBeInTheDocument()
})

test("removes displayed rows when retention expires during pagination", async () => {
  const getResult = vi.fn()
    .mockResolvedValueOnce({...result, next_cursor: "page-2"})
    .mockResolvedValueOnce({...result, status: "expired", rows: [], columns: [], technical_details: null})
  const user = userEvent.setup()
  render(<ResultPage client={{getResult}} requestId={result.request_id} />)

  await user.click(await screen.findByRole("button", {name: "Load more rows"}))

  expect(await screen.findByText("This result has expired.")).toBeVisible()
  expect(screen.queryByRole("table")).not.toBeInTheDocument()
  expect(screen.queryByRole("link", {name: "Download CSV"})).not.toBeInTheDocument()
  expect(screen.queryByText(result.answer_text!)).not.toBeInTheDocument()
})

test("draws the measure above the rows, without becoming the reading of record", async () => {
  render(<ResultPage client={client()} requestId={result.request_id} />)

  const figure = await screen.findByRole("figure", {hidden: true})
  expect(within(figure).getByText("Net revenue")).toBeDefined()
  // The tallest column is the only one labelled: a number on every cap is what makes a small
  // chart unreadable, and the table below carries all of them.
  expect(within(figure).getByText("1000.25")).toBeDefined()
  expect(within(figure).queryByText("250.25")).toBeNull()

  // Hidden from assistive technology on purpose. The table is the result; this is a second
  // reading of it, so a screen reader is sent to the rows rather than to a row of blocks.
  expect(figure.getAttribute("aria-hidden")).toBe("true")
  expect(screen.getByRole("table", {name: "Result rows"})).toBeVisible()
})

test("a result the figure cannot state honestly is shown as rows alone", async () => {
  render(
    <ResultPage
      client={client({
        ...result,
        row_count: 1,
        rows: [["North", "1000.25"]],
      })}
      requestId={result.request_id}
    />,
  )

  expect(await screen.findByRole("table", {name: "Result rows"})).toBeVisible()
  expect(screen.queryByRole("figure", {hidden: true})).toBeNull()
})
