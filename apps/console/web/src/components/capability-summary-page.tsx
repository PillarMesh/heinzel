import type {CapabilityView} from "../api/generated"
import {CapabilityState} from "./capability-state"

type CapabilitySummaryKind = "data-products" | "runs" | "catalog" | "dashboards" | "evidence"

const summaryContent = {
  "data-products": {
    title: "Data products",
    summary: "Review versioned products and their governed state.",
    guidance: "Product versions remain proposals until their owning services commit them.",
  },
  runs: {
    title: "Runs",
    summary: "Track accepted, running, and terminal product operations.",
    guidance: "Accepted work is shown separately from terminal success and evidence.",
  },
  catalog: {
    title: "Catalog",
    summary: "Inspect published meaning, ownership, and lineage.",
    guidance: "Catalog projections describe governed meaning; they do not grant authoring authority.",
  },
  dashboards: {
    title: "Dashboards",
    summary: "Review managed dashboard capability and delivery boundaries.",
    guidance: "Provider administration and unrestricted dashboard authoring remain outside this shell.",
  },
  evidence: {
    title: "Evidence",
    summary: "Trace decisions and outcomes to immutable evidence references.",
    guidance: "Evidence is recorded with the work it proves.",
  },
} satisfies Record<CapabilitySummaryKind, {title: string; summary: string; guidance: string}>

// A summary page shows the capabilities that concern it, not the whole workspace register: a page
// titled Evidence that lists the warehouse binding tells the reader nothing about evidence.
const relevantCapabilities = {
  "data-products": ["data-product-runs"],
  runs: ["data-product-runs", "source-acquisition", "operation-retry"],
  catalog: ["catalog-binding", "catalog-asset-preview"],
  dashboards: ["analyst-dashboard"],
  evidence: ["governed-evidence", "request-fulfillment", "acquisition-evidence"],
} satisfies Record<CapabilitySummaryKind, readonly string[]>

interface CapabilitySummaryPageProps {
  readonly capabilities: readonly CapabilityView[]
  readonly kind: CapabilitySummaryKind
}

export function CapabilitySummaryPage({capabilities, kind}: CapabilitySummaryPageProps) {
  const content = summaryContent[kind]
  const relevant: readonly string[] = relevantCapabilities[kind]
  const shown = capabilities.filter((capability) => relevant.includes(capability.capability_id))

  return (
    <section aria-labelledby={`${kind}-title`} className="summary-page">
      <p className="eyebrow">Governed capability</p>
      <h1 id={`${kind}-title`}>{content.title}</h1>
      <p className="summary-page__lead">{content.summary}</p>
      <p className="summary-page__guidance">{content.guidance}</p>
      {kind === "evidence" ? (
        <p className="summary-page__guidance">
          Open a request from the <a href="/inbox">decision queue</a> and choose Show evidence
          for its decision record, or review <a href="/acquisition-receipts">acquisition receipts</a>{" "}
          for the work an acquisition performed.
        </p>
      ) : null}
      {shown.length === 0 ? (
        <p className="summary-page__guidance">
          This workspace reports no capability for {content.title.toLowerCase()}.
        </p>
      ) : null}
      <ul aria-label={`${content.title} capability states`} className="capability-ledger">
        {shown.map((capability) => (
          <li className="capability-ledger__item" key={capability.capability_id}>
            <div>
              <h2>{capability.label}</h2>
              <p>{capability.detail}</p>
              {capability.dependency === null || capability.dependency === undefined ? null : (
                <p className="capability-ledger__dependency">
                  <span>Dependency</span> {capability.dependency}
                </p>
              )}
            </div>
            <CapabilityState state={capability.state} />
          </li>
        ))}
      </ul>
    </section>
  )
}
