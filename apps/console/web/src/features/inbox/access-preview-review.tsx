import {formatInstant} from "../../format/instant"
import {StatusPill} from "../../components/status-pill"
import {ArtifactReference, ArtifactReferences, ProposalApprovals} from "./artifact-reference"
import type {
  AccessMode,
  AccessPreviewProposalView,
  ActorRole,
  AuthorityStatusView,
} from "../../api/generated"

const accessModeLabels = {
  query: "Query",
  dashboard: "Dashboard",
  export: "Export",
} satisfies Record<AccessMode, string>

function roleLabel(role: ActorRole): string {
  const words = role.replaceAll("_", " ")
  return `${words.charAt(0).toUpperCase()}${words.slice(1)}`
}

interface TextListProps {
  readonly emptyMessage: string
  readonly label: string
  readonly values: readonly string[]
}

function TextList({emptyMessage, label, values}: TextListProps) {
  if (values.length === 0) {
    return <p className="inbox-empty">{emptyMessage}</p>
  }
  return (
    <ul aria-label={label}>
      {values.map((value) => (
        <li key={value}>{value}</li>
      ))}
    </ul>
  )
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

interface AccessPreviewReviewProps {
  readonly proposal: AccessPreviewProposalView
}

export function AccessPreviewReview({proposal}: AccessPreviewReviewProps) {
  return (
    <section aria-label="Effective access preview" className="proposal-review">
      <h3>Proposed effective access</h3>
      <dl className="proposal-review__facts">
        <div>
          <dt>Requester purpose</dt>
          <dd>{proposal.purpose}</dd>
        </div>
        <div>
          <dt>Data product</dt>
          <dd>
            {proposal.data_product_reference ? <ArtifactReference reference={proposal.data_product_reference} /> : <code>{proposal.data_product_ref}</code>}
          </dd>
        </div>
        <div>
          <dt>Access mode</dt>
          <dd>{accessModeLabels[proposal.access_mode]}</dd>
        </div>
        <div>
          <dt>Expires at</dt>
          <dd>
            <time dateTime={proposal.expires_at}>{formatInstant(proposal.expires_at)}</time>
          </dd>
        </div>
        <div>
          <dt>Authority</dt>
          <dd>{proposal.authority_summary}</dd>
        </div>
      </dl>

      <h4>Requested fields</h4>
      <TextList
        emptyMessage="No field was requested."
        label="Requested fields"
        values={proposal.requested_fields}
      />

      <h4>Effective scope</h4>
      <TextList
        emptyMessage="No field is in the effective scope."
        label="Effective scope"
        values={proposal.effective_scope ?? []}
      />

      <h4>Exclusions</h4>
      <TextList
        emptyMessage="No field is excluded."
        label="Exclusions"
        values={proposal.exclusions ?? []}
      />

      <h4>Intended checks</h4>
      <TextList
        emptyMessage="No intended check was recorded."
        label="Intended checks"
        values={proposal.intended_checks ?? []}
      />

      <h4>Denied checks</h4>
      <TextList
        emptyMessage="No denied check was recorded."
        label="Denied checks"
        values={proposal.denied_checks ?? []}
      />

      <h4>Required roles</h4>
      <ArtifactReferences label="Effective objects" references={proposal.effective_object_references ?? []} />
      <ProposalApprovals approvals={proposal.required_approvals ?? []} />
      <AuthorityList authorities={proposal.required_authorities ?? []} />
    </section>
  )
}
