import {useEffect, useRef, useState, type ReactNode} from "react"

import type {EvidenceContextView, FreshnessState} from "../../api/generated"

const freshnessLabels = {
  current: "Current",
  stale: "Stale",
  unknown: "Unknown",
  not_applicable: "Not applicable",
} satisfies Record<FreshnessState, string>

export type EvidenceLayout = "wide" | "medium" | "narrow"

interface EvidenceDrawerProps {
  readonly children?: ReactNode
  readonly evidence: EvidenceContextView
  readonly layout: EvidenceLayout
}

function EvidenceContent({
  children,
  evidence,
}: {
  readonly children?: ReactNode
  readonly evidence: EvidenceContextView
}) {
  const datasets = evidence.datasets ?? []
  const metricVersions = evidence.metric_versions ?? []
  const evidenceRefs = evidence.evidence_refs ?? []

  return (
    <div className="evidence-content">
      <dl className="evidence-content__facts">
        <div>
          <dt>As of</dt>
          <dd>
            {evidence.as_of === null || evidence.as_of === undefined ? (
              "Not recorded"
            ) : (
              <time dateTime={evidence.as_of}>{evidence.as_of}</time>
            )}
          </dd>
        </div>
        <div>
          <dt>Freshness</dt>
          <dd>{freshnessLabels[evidence.freshness]}</dd>
        </div>
        <div>
          <dt>Quality</dt>
          <dd>{evidence.quality_summary}</dd>
        </div>
        <div>
          <dt>Lineage</dt>
          <dd>{evidence.lineage_summary}</dd>
        </div>
        <div>
          <dt>Authorization</dt>
          <dd>{evidence.authorization_summary}</dd>
        </div>
      </dl>

      <h3>Governed datasets</h3>
      {datasets.length === 0 ? (
        <p className="inbox-empty">No governed dataset was recorded.</p>
      ) : (
        <ul aria-label="Evidence datasets">
          {datasets.map((dataset) => (
            <li key={dataset.dataset_ref}>
              {dataset.display_name} <code>{dataset.dataset_ref}</code>
            </li>
          ))}
        </ul>
      )}

      <h3>Metric versions</h3>
      {metricVersions.length === 0 ? (
        <p className="inbox-empty">No metric version was recorded.</p>
      ) : (
        <ul aria-label="Metric versions">
          {metricVersions.map((version) => (
            <li key={version}>
              <code>{version}</code>
            </li>
          ))}
        </ul>
      )}

      <h3>Immutable references</h3>
      {evidenceRefs.length === 0 ? (
        <p className="inbox-empty">No immutable evidence reference exists yet.</p>
      ) : (
        <ul aria-label="Immutable references">
          {evidenceRefs.map((reference) => (
            <li key={reference}>
              <code>{reference}</code>
            </li>
          ))}
        </ul>
      )}

      {children}
    </div>
  )
}

export function EvidenceDrawer({children, evidence, layout}: EvidenceDrawerProps) {
  const [open, setOpen] = useState(false)
  const drawerRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const restoreFocus = useRef(false)

  useEffect(() => {
    if (open) {
      drawerRef.current?.focus()
      return
    }
    if (restoreFocus.current) {
      restoreFocus.current = false
      triggerRef.current?.focus()
    }
  }, [open])

  if (layout === "wide") {
    return (
      <section aria-label="Decision evidence" className="evidence-region">
        <h2>Decision evidence</h2>
        <EvidenceContent evidence={evidence}>{children}</EvidenceContent>
      </section>
    )
  }

  return (
    <div className="evidence-region evidence-region--drawer">
      <button
        aria-expanded={open}
        onClick={() => {
          restoreFocus.current = true
          setOpen(!open)
        }}
        ref={triggerRef}
        type="button"
      >
        {open ? "Hide evidence" : "Show evidence"}
      </button>
      {!open ? null : (
        <div
          aria-label="Decision evidence"
          aria-modal="false"
          className="evidence-drawer"
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.stopPropagation()
              setOpen(false)
            }
          }}
          ref={drawerRef}
          role="dialog"
          tabIndex={-1}
        >
          <h2>Decision evidence</h2>
          <EvidenceContent evidence={evidence}>{children}</EvidenceContent>
          <button
            onClick={() => {
              setOpen(false)
            }}
            type="button"
          >
            Close evidence
          </button>
        </div>
      )}
    </div>
  )
}
