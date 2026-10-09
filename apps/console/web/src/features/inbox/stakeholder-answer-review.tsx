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

  // Only the kinds that were actually cited. `0 metric references` is a fact about nothing,
  // and three of them made the card's own value the longest line in the panel.
  const citedSummary = (
    [
      [datasets.length, "governed dataset"],
      [metricReferences.length, "metric reference"],
      [lineageReferences.length, "lineage reference"],
    ] as const
  )
    .filter(([count]) => count > 0)
    .map(([count, noun]) => `${count} ${noun}${count === 1 ? "" : "s"}`)
    .join(", ")
  const cited = datasets.length + metricReferences.length + lineageReferences.length
  const recorded = approvals.filter((approval) => approval.satisfied).length

  // The verdict the four checks add up to, so the panel says at its head what it takes four
  // readings to work out. Counted rather than written down twice: a card and this cannot
  // disagree, because this is derived from the same conditions the cards are.
  const unsettled =
    (cited === 0 ? 1 : 0) +
    (freshnessTone[proposal.freshness] === "attention" ? 1 : 0) +
    (limitations.length > 0 ? 1 : 0) +
    (approvals.length > 0 && recorded === approvals.length ? 0 : 1)

  return (
    <Panel
      ariaLabel="Stakeholder answer proposal"
      aside={
        <StatusPill tone={unsettled === 0 ? "ready" : "attention"}>
          {unsettled === 0 ? "All checks clear" : `${unsettled} to read`}
        </StatusPill>
      }
      description="What the runtime composed from the governed scope, and what has to hold for it."
      title="Proposed stakeholder answer"
    >
      {/*
        The answer and the question it answers, on their own surface. This is the thing being
        decided, and it was previously a paragraph of the same weight as the grounds under it.
      */}
      <div className="proposal-review__subject">
        <p className="proposal-review__candidate">{proposal.candidate}</p>
        <p className="proposal-review__purpose">
          <span>Answers</span> {proposal.purpose}
        </p>
      </div>

      <ReviewChecks>
        <ReviewCheck
          detail={
            cited === 0 ? undefined : (
              <p>Listed in full under Evidence, with the digest each was cited at.</p>
            )
          }
          label="Grounds"
          summary={cited === 0 ? "Nothing was cited for this answer." : citedSummary}
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
          label="Authorization"
          summary={
            approvals.length === 0
              ? proposal.authorization_summary
              : `${recorded} of ${approvals.length} required approval${approvals.length === 1 ? "" : "s"} recorded`
          }
          tone={approvals.length > 0 && recorded === approvals.length ? "ready" : "attention"}
        />
      </ReviewChecks>

      {/*
        Who still has to sign, at full width below the cards. A list that grows with the
        request does not belong in one cell of a grid of four: one tall card drags the row
        it sits in, and the grid stops reading as a grid.
      */}
      {approvals.length === 0 && authorities.length === 0 ? null : (
        <div className="proposal-review__signatures">
          <ProposalApprovals approvals={approvals} />
          {authorities.length === 0 ? null : (
            <section aria-label="Required roles">
              <h4>Required roles</h4>
              <AuthorityList authorities={authorities} />
            </section>
          )}
        </div>
      )}
    </Panel>
  )
}
