import type {ArtifactReferenceView, ProposalApprovalView} from "../../api/generated"

/**
 * A digest reads as its ends, which is the part a person compares.
 *
 * Printed whole it is sixty-four characters that wrapped across four lines of a 256px column,
 * twice over on the decision workspace, and nobody verified an artifact by reading them: the
 * length crowded out the fields the reviewer was there to judge. The whole value is one
 * disclosure away and still selectable, so it can be copied and checked properly.
 */
function abbreviate(digest: string): string {
  return digest.length <= 20 ? digest : `${digest.slice(0, 8)}…${digest.slice(-8)}`
}

export function ArtifactDigest({digest}: {readonly digest: string}) {
  return (
    <details className="artifact-digest">
      <summary>
        SHA-256 <code>{abbreviate(digest)}</code>
      </summary>
      <code className="artifact-digest__full">{digest}</code>
    </details>
  )
}

export function ArtifactReference({reference}: {readonly reference: ArtifactReferenceView}) {
  return (
    <span className="artifact-reference">
      <code>{reference.artifact_id}</code> · version {reference.version}
      <ArtifactDigest digest={reference.digest} />
    </span>
  )
}

export function ArtifactReferences({label, references}: {readonly label: string; readonly references: readonly ArtifactReferenceView[]}) {
  if (references.length === 0) return null
  return <section aria-label={label}><h4>{label}</h4><ul>{references.map((reference) => <li key={JSON.stringify(reference)}><ArtifactReference reference={reference} /></li>)}</ul></section>
}

export function ProposalApprovals({approvals}: {readonly approvals: readonly ProposalApprovalView[]}) {
  if (approvals.length === 0) return null
  // The authority reference is what the owning service checks; the label is what a reviewer reads.
  return <section aria-label="Required approvals"><h4>Required approvals</h4><ul>{approvals.map((approval) => <li key={`${approval.authority_ref}:${approval.reason}`}>{approval.authority_label === null || approval.authority_label === undefined ? <code>{approval.authority_ref}</code> : approval.authority_label} · {approval.reason.replaceAll("_", " ")} · <strong>{approval.satisfied ? "Recorded" : "Not recorded"}</strong></li>)}</ul></section>
}
