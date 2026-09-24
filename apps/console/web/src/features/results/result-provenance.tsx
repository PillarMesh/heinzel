import type {ResultView} from "./result-api"

export function ResultProvenance({result}: {readonly result: ResultView}) {
  return (
    <section aria-labelledby="result-provenance-title" className="result-provenance">
      <p className="eyebrow">Governed context</p>
      <h2 id="result-provenance-title">What this result represents</h2>
      <p>
        This table comes from the governed warehouse result recorded for this request. Its column
        order and value types are preserved from the verified result snapshot.
      </p>
      <p>Freshness: {(result.freshness ?? "unknown").replaceAll("_", " ")}.</p>
    </section>
  )
}
