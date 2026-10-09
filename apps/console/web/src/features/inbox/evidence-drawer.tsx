import {Panel} from "../../components/panel"
import {formatInstant} from "../../format/instant"
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

  /*
    A companion to the work, not a second copy of it.

    The rail printed `as of`, `freshness`, `quality`, `lineage`, `authorization`, the governed
    datasets and the metric references -- every one of which the proposal beside it printed
    too, with the same values, on the same screen. Now that the work area carries one job at a
    time, the rail's job is the standing summary: whether the grounds are in order, and what
    has happened to the request. The detail is one tab away, and the references the proposal
    does not carry stay here.
  */
  const approvalsRecorded = (evidence.authorization_summary ?? "").trim()

  return (
    <div className="evidence-content">
      <dl className="record evidence-content__facts">
        <dt>As of</dt>
        <dd>
          {evidence.as_of === null || evidence.as_of === undefined ? (
            "Not recorded"
          ) : (
            <time dateTime={evidence.as_of}>{formatInstant(evidence.as_of)}</time>
          )}
        </dd>
        <dt>Freshness</dt>
        <dd>{freshnessLabels[evidence.freshness]}</dd>
        <dt>Grounds</dt>
        <dd>
          {datasets.length} dataset{datasets.length === 1 ? "" : "s"} ·{" "}
          {metricVersions.length} metric version{metricVersions.length === 1 ? "" : "s"}
        </dd>
        <dt>Authorization</dt>
        <dd>{approvalsRecorded === "" ? "Not recorded" : approvalsRecorded}</dd>
      </dl>

      <section className="evidence-content__group">
        <h3>Immutable references</h3>
        {evidenceRefs.length === 0 ? (
          <p className="panel__nothing">No immutable evidence reference exists yet.</p>
        ) : (
          <ul aria-label="Immutable references">
            {evidenceRefs.map((reference) => (
              <li key={reference}>
                <code>{reference}</code>
              </li>
            ))}
          </ul>
        )}
      </section>

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
      /* A panel like every other section, rather than a heading over loose content. */
      <div className="evidence-region">
        <Panel ariaLabel="Decision evidence" headingLevel={2} title="Decision evidence">
          <EvidenceContent evidence={evidence}>{children}</EvidenceContent>
        </Panel>
      </div>
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
