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
    guidance: "Fixture projections never represent authoritative managed evidence.",
  },
} satisfies Record<CapabilitySummaryKind, {title: string; summary: string; guidance: string}>

interface CapabilitySummaryPageProps {
  readonly capabilities: readonly CapabilityView[]
  readonly kind: CapabilitySummaryKind
}

export function CapabilitySummaryPage({capabilities, kind}: CapabilitySummaryPageProps) {
  const content = summaryContent[kind]

  return (
    <section aria-labelledby={`${kind}-title`} className="summary-page">
      <p className="eyebrow">Governed capability</p>
      <h1 id={`${kind}-title`}>{content.title}</h1>
      <p className="summary-page__lead">{content.summary}</p>
      <p className="summary-page__guidance">{content.guidance}</p>
      <ul aria-label={`${content.title} capability states`} className="capability-ledger">
        {capabilities.map((capability) => (
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
