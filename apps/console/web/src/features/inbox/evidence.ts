import type {EvidenceContextView} from "../../api/generated"

/**
 * Whether this request has any evidence to show yet.
 *
 * On a submitted request every field in the evidence rail is empty, and the rail spent a whole
 * column of the page saying so six times: `Not recorded`, `Unknown`, `0 datasets`, `0 of 0
 * approvals`, no references, no lifecycle. A standing column whose content is an inventory of
 * absences is worse than no column -- it takes width from the work and tells the reader
 * nothing they could act on.
 */
export function hasEvidence(evidence: EvidenceContextView, lifecycle: number): boolean {
  return (
    lifecycle > 0 ||
    (evidence.as_of ?? "") !== "" ||
    (evidence.datasets ?? []).length > 0 ||
    (evidence.metric_versions ?? []).length > 0 ||
    (evidence.evidence_refs ?? []).length > 0 ||
    (evidence.metric_references ?? []).length > 0
  )
}
