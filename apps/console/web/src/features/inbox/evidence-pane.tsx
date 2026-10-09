/**
 * What was cited, and what has happened to this request.
 *
 * This was a standing rail beside the work: the narrowest column on the page, carrying the
 * longest values, repeating four of the five facts the proposal printed beside it -- `as of`,
 * `freshness`, the authorization summary and a count of the datasets the proposal listed in
 * full two inches to its left. A reader got the same sentence twice and a timestamp broken
 * over three lines.
 *
 * As a section it is a section: one job, the page's full width, and nothing in it said
 * anywhere else. The facts that were duplicated are gone rather than moved, because the
 * proposal is where an approver reads them.
 */
import {Nothing, Panel} from "../../components/panel"
import {ArtifactReference, ArtifactReferences} from "./artifact-reference"
import {LifecycleTimeline} from "./lifecycle-timeline"
import {formatInstant} from "../../format/instant"
import type {
  EvidenceContextView,
  FreshnessState,
  LifecycleEventView,
  RequestProposalView,
} from "../../api/generated"
import {Fragment, type ReactNode} from "react"

const freshnessLabels = {
  current: "Current",
  stale: "Stale",
  unknown: "Unknown",
  not_applicable: "Not applicable",
} satisfies Record<FreshnessState, string>

interface EvidencePaneProps {
  /** Catalog records and a dashboard candidate: read on demand, so the caller supplies them. */
  readonly children?: ReactNode
  readonly evidence: EvidenceContextView
  readonly lifecycle: readonly LifecycleEventView[]
  readonly proposal: RequestProposalView | null
}

/** The artifacts a stakeholder answer was built on, each at the digest it was cited at. */
function Citations({proposal}: {readonly proposal: RequestProposalView}) {
  if (proposal.kind !== "stakeholder_answer") {
    return null
  }
  const datasets = proposal.datasets ?? []
  const metricReferences = proposal.metric_references ?? []
  const lineageReferences = proposal.lineage_references ?? []
  const qualityReferences = proposal.quality_references ?? []
  if (
    datasets.length +
      metricReferences.length +
      lineageReferences.length +
      qualityReferences.length ===
    0
  ) {
    return (
      <Panel headingLevel={3} title="Cited for this answer">
        <Nothing>Nothing has been cited for this answer.</Nothing>
      </Panel>
    )
  }
  return (
    <Panel headingLevel={3} title="Cited for this answer">
      <div className="evidence-pane__citations">
        {datasets.length === 0 ? null : (
          <section>
            <h4>Governed datasets</h4>
            <ul aria-label="Governed datasets">
              {datasets.map((dataset) => (
                <li key={dataset.dataset_ref}>
                  {dataset.artifact_reference ? (
                    <ArtifactReference reference={dataset.artifact_reference} />
                  ) : (
                    <>
                      {dataset.display_name} <code>{dataset.dataset_ref}</code>
                    </>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}
        {metricReferences.length === 0 ? null : (
          <section>
            <ArtifactReferences label="Metric references" references={metricReferences} />
          </section>
        )}
        {lineageReferences.length === 0 ? null : (
          <section>
            <ArtifactReferences label="Lineage references" references={lineageReferences} />
          </section>
        )}
        {qualityReferences.length === 0 ? null : (
          <section>
            <ArtifactReferences label="Quality references" references={qualityReferences} />
          </section>
        )}
      </div>
    </Panel>
  )
}

/**
 * What the request's own evidence context states, for a request that has no proposal yet.
 *
 * These are the same facts a proposal states, which is why this is not shown beside one: the
 * rail that used to print them did so next to a proposal printing every one of them again. A
 * request with nothing proposed has no second copy, and then they are the only copy.
 */
function Context({evidence}: {readonly evidence: EvidenceContextView}) {
  const summaries = [
    ["Quality", evidence.quality_summary],
    ["Lineage", evidence.lineage_summary],
    ["Authorization", evidence.authorization_summary],
  ] as const
  return (
    <Panel headingLevel={3} title="Context">
      <dl className="record">
        {evidence.as_of === null || evidence.as_of === undefined ? null : (
          <>
            <dt>As of</dt>
            <dd>
              <time dateTime={evidence.as_of}>{formatInstant(evidence.as_of)}</time>
            </dd>
          </>
        )}
        <dt>Freshness</dt>
        <dd>{freshnessLabels[evidence.freshness]}</dd>
        {summaries.map(([label, summary]) =>
          (summary ?? "").trim() === "" ? null : (
            <Fragment key={label}>
              <dt className="record__wide">{label}</dt>
              <dd className="record__wide">{summary}</dd>
            </Fragment>
          ),
        )}
      </dl>
    </Panel>
  )
}

export function EvidencePane({children, evidence, lifecycle, proposal}: EvidencePaneProps) {
  const evidenceRefs = evidence.evidence_refs ?? []
  return (
    <div className="panel-stack evidence-pane">
      {proposal === null ? <Context evidence={evidence} /> : <Citations proposal={proposal} />}
      {children}
      {evidenceRefs.length === 0 ? null : (
        <Panel
          description="Written once and never rewritten, so a decision can be re-read exactly as it was taken."
          headingLevel={3}
          title="Immutable references"
        >
          <ul aria-label="Immutable references" className="evidence-pane__refs">
            {evidenceRefs.map((reference) => (
              <li key={reference}>
                <code>{reference}</code>
              </li>
            ))}
          </ul>
        </Panel>
      )}
      <Panel headingLevel={3} title="Lifecycle">
        {lifecycle.length === 0 ? (
          <Nothing>No lifecycle event has been recorded for this request.</Nothing>
        ) : (
          <LifecycleTimeline events={lifecycle} />
        )}
      </Panel>
    </div>
  )
}
