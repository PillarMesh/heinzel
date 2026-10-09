import {Nothing, Panel} from "../../components/panel"
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
    <Panel
      ariaLabel="Stakeholder answer proposal"
      description="What the runtime composed from the governed scope, and the grounds for it."
      title="Proposed stakeholder answer"
    >
      {/*
        The answer itself, read first and at reading size. It used to be the second row of a
        definition list, in the same type as the metric identifier beside it.
      */}
      <p className="proposal-review__candidate">{proposal.candidate}</p>
      <dl className="record">
        <dt>Requester purpose</dt>
        <dd>{proposal.purpose}</dd>
        {/*
          The server sends "See exact metric references" when the version is carried by the
          references rather than by a name. Printed as a value it was a row that pointed at
          another row on the same screen.
        */}
        {proposal.metric_version === "See exact metric references" ? null : (
          <>
            <dt>Metric version</dt>
            <dd>
              <code>{proposal.metric_version}</code>
            </dd>
          </>
        )}
        <dt>As of</dt>
        <dd>
          <time dateTime={proposal.as_of}>{formatInstant(proposal.as_of)}</time>
        </dd>
        <dt>Freshness</dt>
        <dd>{freshnessLabels[proposal.freshness]}</dd>
        <dt>Lineage</dt>
        <dd>{proposal.lineage_summary}</dd>
        <dt>Authorization</dt>
        <dd>{proposal.authorization_summary}</dd>
      </dl>

      {/*
        The grounds, in columns. As one stacked list of eight headings -- four of which were
        usually an apology for having nothing -- they were most of the page's height and none
        of its meaning.
      */}
      <div className="proposal-review__grounds">
        <section className="proposal-review__ground">
          <h4>Quality limitations</h4>
          {limitations.length === 0 && (proposal.quality_references ?? []).length === 0 ? (
            <Nothing>No quality limitation was recorded.</Nothing>
          ) : (
            <ul aria-label="Quality limitations">
              {limitations.map((limitation) => (
                <li key={limitation}>{limitation}</li>
              ))}
            </ul>
          )}
          <ArtifactReferences
            label="Quality references"
            references={proposal.quality_references ?? []}
          />
        </section>
        <section className="proposal-review__ground">
          <h4>Governed datasets</h4>
          {datasets.length === 0 ? (
            <Nothing>No governed dataset was recorded.</Nothing>
          ) : (
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
          )}
        </section>
        <section className="proposal-review__ground">
          <ArtifactReferences
            label="Metric references"
            references={proposal.metric_references ?? []}
          />
        </section>
        <section className="proposal-review__ground">
          <ArtifactReferences
            label="Lineage references"
            references={proposal.lineage_references ?? []}
          />
        </section>
      </div>

      <ProposalApprovals approvals={proposal.required_approvals ?? []} />
      {(proposal.required_authorities ?? []).length === 0 ? null : (
        <section className="proposal-review__ground">
          <h4>Required roles</h4>
          <AuthorityList authorities={proposal.required_authorities ?? []} />
        </section>
      )}
    </Panel>
  )
}
