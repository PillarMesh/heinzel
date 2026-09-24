import {useEffect, useState} from "react"

import {ConsoleApiError} from "../../api/client"
import type {ResultClient, ResultView} from "./result-api"
import {ResultProvenance} from "./result-provenance"
import {ResultTable} from "./result-table"
import "./results.css"

export type {ResultClient, ResultView} from "./result-api"

interface ResultPageProps {
  readonly client: ResultClient
  readonly requestId: string
}

export function ResultPage({client, requestId}: ResultPageProps) {
  const [result, setResult] = useState<ResultView | null>(null)
  const [rows, setRows] = useState<NonNullable<ResultView["rows"]>>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    void client
      .getResult(requestId)
      .then((value) => {
        if (active) {
          setResult(value)
          setRows(value.rows ?? [])
          setNextCursor(value.next_cursor ?? null)
        }
      })
      .catch((error: unknown) => {
        if (!active) return
        setFailure(
          error instanceof ConsoleApiError
            ? error.message
            : "This result could not be displayed safely.",
        )
      })
    return () => {
      active = false
    }
  }, [client, requestId])

  if (failure !== null) return <p role="alert">{failure}</p>
  if (result === null) return <p role="status">Loading your result…</p>
  if (result.status === "expired") return <p role="status">This result has expired.</p>
  if (result.status === "failed") return <p role="alert">This result could not be completed.</p>

  async function loadMore(): Promise<void> {
    if (nextCursor === null) return
    setLoadingMore(true)
    try {
      const page = await client.getResult(requestId, nextCursor)
      if (page.status !== "available") {
        setResult(page)
        setRows([])
        setNextCursor(null)
        return
      }
      setRows((existing) => [...existing, ...(page.rows ?? [])])
      setNextCursor(page.next_cursor ?? null)
    } catch (error: unknown) {
      setFailure(
        error instanceof ConsoleApiError
          ? error.message
          : "More result rows could not be displayed safely.",
      )
    } finally {
      setLoadingMore(false)
    }
  }

  return (
    <article className="result-page">
      <header className="result-hero">
        <p className="eyebrow">Governed result</p>
        <h1>{result.title}</h1>
        {result.answer_text === null ? null : <p className="result-hero__answer">{result.answer_text}</p>}
        <p className="result-hero__context">
          As of {result.as_of} · {(result.freshness ?? "unknown").replaceAll("_", " ")} · {result.row_count}{" "}
          {result.row_count === 1 ? "row" : "rows"}
        </p>
        <a href={`/api/v1/requests/${encodeURIComponent(requestId)}/result.csv`}>Download CSV</a>
      </header>

      {rows.length === 0 ? (
        <p className="result-empty">No rows matched this governed question.</p>
      ) : (
        <>
          <ResultTable columns={result.columns ?? []} rows={rows} />
          {nextCursor === null ? null : (
            <button disabled={loadingMore} onClick={() => void loadMore()} type="button">
              {loadingMore ? "Loading more…" : "Load more rows"}
            </button>
          )}
        </>
      )}

      <ResultProvenance result={result} />

      {result.technical_details == null ? null : <details className="technical-details">
        <summary>Technical details</summary>
        <dl>
          <dt>Request reference</dt>
          <dd>{result.request_id}</dd>
          <dt>Plan digest</dt>
          <dd>{result.technical_details.plan_digest}</dd>
          <dt>Execution receipt</dt>
          <dd>{result.technical_details.execution_receipt_id}</dd>
        </dl>
      </details>}
    </article>
  )
}
