import type {ArtifactReferenceView, ProposalApprovalView} from "../../api/generated"

export function ArtifactReference({reference}: {readonly reference: ArtifactReferenceView}) {
  return <span className="artifact-reference"><code>{reference.artifact_id}</code> · version {reference.version}<br /><small>SHA-256 <code>{reference.digest}</code></small></span>
}

export function ArtifactReferences({label, references}: {readonly label: string; readonly references: readonly ArtifactReferenceView[]}) {
  if (references.length === 0) return null
  return <section aria-label={label}><h4>{label}</h4><ul>{references.map((reference) => <li key={JSON.stringify(reference)}><ArtifactReference reference={reference} /></li>)}</ul></section>
}

export function ProposalApprovals({approvals}: {readonly approvals: readonly ProposalApprovalView[]}) {
  if (approvals.length === 0) return null
  return <section aria-label="Required approvals"><h4>Required approvals</h4><ul>{approvals.map((approval) => <li key={`${approval.authority_ref}:${approval.reason}`}><code>{approval.authority_ref}</code> · {approval.reason.replaceAll("_", " ")} · <strong>{approval.satisfied ? "Recorded" : "Not recorded"}</strong></li>)}</ul></section>
}
