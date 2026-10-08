import {StatusPill} from "../../components/status-pill"
import {formatInstant} from "../../format/instant"
import {ArtifactReference, ArtifactReferences, ProposalApprovals} from "./artifact-reference"
import type {
  ActorRole,
  AuthorityStatusView,
  FreshnessState,
  StakeholderAnswerProposalView,
} from "../../api/generated"

const freshnessLabels = {
  current: "Current",
  stale: "Stale",
  unknown: "Unknown",
  not_applicable: "Not applicable",
} satisfies Record<FreshnessState, string>

function roleLabel(role: ActorRole): string {
  const words = role.replaceAll("_", " ")
  return `${words.charAt(0).toUpperCase()}${words.slice(1)}`
}

function AuthorityList({authorities}: {readonly authorities: readonly AuthorityStatusView[]}) {
  return (
    <ul aria-label="Required authorities" className="authority-list">
      {authorities.map((authority) => (
        <li key={`${authority.role}:${authority.reason}`}>
          <strong>{roleLabel(authority.role)}</strong>
          <span>{authority.reason}</span>
          <StatusPill tone={authority.satisfied ? "ready" : "neutral"}>
            {authority.satisfied ? "Recorded" : "Not recorded"}
          </StatusPill>
        </li>
      ))}
    </ul>
  )
}

interface StakeholderAnswerReviewProps {
  readonly proposal: StakeholderAnswerProposalView
}

export function StakeholderAnswerReview({proposal}: StakeholderAnswerReviewProps) {
  const limitations = proposal.quality_limitations ?? []
  const datasets = proposal.datasets ?? []

  return (
    <section aria-label="Stakeholder answer proposal" className="proposal-review">
      <h3>Proposed stakeholder answer</h3>
      <dl className="proposal-review__facts">
        <div>
          <dt>Requester purpose</dt>
          <dd>{proposal.purpose}</dd>
        </div>
        <div>
          <dt>Candidate answer</dt>
          <dd>{proposal.candidate}</dd>
        </div>
        <div>
          <dt>Metric version</dt>
          <dd>
            <code>{proposal.metric_version}</code>
          </dd>
        </div>
        <div>
          <dt>As of</dt>
          <dd>
            <time dateTime={proposal.as_of}>{formatInstant(proposal.as_of)}</time>
          </dd>
        </div>
        <div>
          <dt>Freshness</dt>
          <dd>{freshnessLabels[proposal.freshness]}</dd>
        </div>
        <div>
          <dt>Lineage</dt>
          <dd>{proposal.lineage_summary}</dd>
        </div>
        <div>
          <dt>Authorization</dt>
          <dd>{proposal.authorization_summary}</dd>
        </div>
      </dl>

      <h4>Quality limitations</h4>
      {(limitations.length === 0 && (proposal.quality_references ?? []).length === 0) ? (
        <p className="inbox-empty">No quality limitation was recorded.</p>
      ) : (
        <ul aria-label="Quality limitations">
          {limitations.map((limitation) => (
            <li key={limitation}>{limitation}</li>
          ))}
        </ul>
      )}

      <h4>Governed datasets</h4>
      {datasets.length === 0 ? (
        <p className="inbox-empty">No governed dataset was recorded.</p>
      ) : (
        <ul aria-label="Governed datasets">
          {datasets.map((dataset) => (
            <li key={dataset.dataset_ref}>
              {dataset.artifact_reference ? <ArtifactReference reference={dataset.artifact_reference} /> : <>{dataset.display_name} <code>{dataset.dataset_ref}</code></>}
            </li>
          ))}
        </ul>
      )}

      <ArtifactReferences label="Metric references" references={proposal.metric_references ?? []} />
      <ArtifactReferences label="Lineage references" references={proposal.lineage_references ?? []} />
      <ArtifactReferences label="Quality references" references={proposal.quality_references ?? []} />
      <ProposalApprovals approvals={proposal.required_approvals ?? []} />
      {(proposal.required_authorities ?? []).length === 0 ? null : <>
        <h4>Required roles</h4><AuthorityList authorities={proposal.required_authorities ?? []} />
      </>}
    </section>
  )
}
