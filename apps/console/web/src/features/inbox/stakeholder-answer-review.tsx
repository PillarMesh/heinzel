import {Panel} from "../../components/panel"
import {StatusPill} from "../../components/status-pill"
import {formatInstant} from "../../format/instant"
import {ProposalApprovals} from "./artifact-reference"
import {ReviewCheck, ReviewChecks, type CheckTone} from "./review-check"
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

/*
  Stale data is the one freshness an approver must not approve past without meaning to.
  `unknown` is quieter but still unanswered; `not_applicable` is a settled fact about a
  question that has no freshness, and reads as neither good nor bad.
*/
const freshnessTone = {
  current: "ready",
  stale: "attention",
  unknown: "attention",
  not_applicable: "neutral",
} satisfies Record<FreshnessState, CheckTone>

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

/**
 * The case for approving this answer, and nothing else.
 *
 * This panel used to be the whole proposal record: the answer, six fact rows, four groups of
 * artifact references with their digests, the approvals and the roles -- about a screen and a
 * half in which every part was given the same weight, so the parts that decide an approval
 * were no easier to find than the parts that are only ever read in an audit. The citations
 * moved to `Evidence`, where they have room and where the request's own history already is.
 * What is left is what an approver acts on: what the answer says, what it answers, the four
 * things that could stop it, and who still has to sign.
 */
export function StakeholderAnswerReview({proposal}: StakeholderAnswerReviewProps) {
  const limitations = proposal.quality_limitations ?? []
  const datasets = proposal.datasets ?? []
  const metricReferences = proposal.metric_references ?? []
  const lineageReferences = proposal.lineage_references ?? []
  const approvals = proposal.required_approvals ?? []
  const authorities = proposal.required_authorities ?? []

  const cited = datasets.length + metricReferences.length + lineageReferences.length
  const recorded = approvals.filter((approval) => approval.satisfied).length

  return (
    <Panel
      ariaLabel="Stakeholder answer proposal"
      description="What the runtime composed from the governed scope, and what has to hold for it."
      title="Proposed stakeholder answer"
    >
      {/* The answer itself, read first and at reading size. */}
      <p className="proposal-review__candidate">{proposal.candidate}</p>
      <p className="proposal-review__purpose">
        Answers <strong>{proposal.purpose}</strong>
      </p>

      <ReviewChecks>
        <ReviewCheck
          detail={
            cited === 0 ? undefined : (
              <p>Listed in full under Evidence, with the digest each was cited at.</p>
            )
          }
          label="Grounds"
          summary={
            cited === 0
              ? "Nothing was cited for this answer."
              : `${datasets.length} governed dataset${datasets.length === 1 ? "" : "s"}, ` +
                `${metricReferences.length} metric reference${metricReferences.length === 1 ? "" : "s"}, ` +
                `${lineageReferences.length} lineage reference${lineageReferences.length === 1 ? "" : "s"}`
          }
          tone={cited === 0 ? "attention" : "ready"}
        />
        <ReviewCheck
          detail={
            <p>
              Composed <time dateTime={proposal.as_of}>{formatInstant(proposal.as_of)}</time>
            </p>
          }
          label="Freshness"
          summary={freshnessLabels[proposal.freshness]}
          tone={freshnessTone[proposal.freshness]}
        />
        <ReviewCheck
          detail={
            limitations.length === 0 ? undefined : (
              <ul aria-label="Quality limitations">
                {limitations.map((limitation) => (
                  <li key={limitation}>{limitation}</li>
                ))}
              </ul>
            )
          }
          label="Quality"
          summary={
            limitations.length === 0
              ? "No limitation was recorded."
              : `${limitations.length} limitation${limitations.length === 1 ? "" : "s"} recorded`
          }
          tone={limitations.length === 0 ? "ready" : "attention"}
        />
        <ReviewCheck
          detail={
            <>
              <ProposalApprovals approvals={approvals} />
              {authorities.length === 0 ? null : <AuthorityList authorities={authorities} />}
            </>
          }
          label="Authorization"
          summary={
            approvals.length === 0
              ? proposal.authorization_summary
              : `${recorded} of ${approvals.length} required approval${approvals.length === 1 ? "" : "s"} recorded`
          }
          tone={approvals.length > 0 && recorded === approvals.length ? "ready" : "attention"}
        />
      </ReviewChecks>
    </Panel>
  )
}
