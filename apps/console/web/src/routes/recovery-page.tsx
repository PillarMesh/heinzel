type RecoveryKind =
  | "workspace"
  | "projection"
  | "service"
  | "malformed"
  | "view"
  | "reauthenticate"
  | "reload"
  | "retry"
  | "contact_support"
  | "correct_input"
  | "none"

const recoveryContent = {
  workspace: {
    title: "Workspace unavailable",
    detail: "The server could not provide an authorized workspace projection.",
  },
  projection: {
    title: "Workspace projections do not match",
    detail: "The console stopped before displaying mixed workspace identity or provenance.",
  },
  service: {
    title: "The console service is unavailable",
    detail: "Keep the current authoritative state and try the connection again.",
  },
  malformed: {
    title: "This workspace response could not be displayed",
    // The second sentence is the one a reader needs. A bundle compiled against an earlier
    // contract fails every response this way, on every page, and the first sentence alone sends
    // them to look at a server that is answering correctly.
    detail:
      "The response failed contract validation, so no partial product state was rendered. " +
      "A compiled bundle older than the service it is served beside fails this way on every " +
      "page: rebuild it with npm run build.",
  },
  view: {
    title: "This view could not be displayed",
    detail: "The affected view was closed before untrusted content could be rendered.",
  },
  reauthenticate: {
    title: "Sign in again",
    detail: "The trusted session must be re-established outside this console.",
  },
  reload: {
    title: "Workspace state changed",
    detail: "Reload the server-issued workspace projection before continuing.",
  },
  retry: {
    title: "The console service is unavailable",
    detail: "Keep the current authoritative state and try the connection again.",
  },
  contact_support: {
    title: "Support is needed",
    detail: "Contact support with the safe correlation reference shown below.",
  },
  correct_input: {
    title: "The request needs correction",
    detail: "Return to the originating form and correct the server-identified input.",
  },
  none: {
    title: "No browser recovery is available",
    detail: "The server did not authorize a browser-side recovery action.",
  },
} satisfies Record<RecoveryKind, {title: string; detail: string}>

interface RecoveryPageProps {
  readonly detail?: string | null | undefined
  readonly kind: RecoveryKind
  readonly actionLabel?: string | undefined
  readonly correlationId?: string | null | undefined
  readonly onRetry?: (() => void) | undefined
}

export function RecoveryPage({actionLabel, correlationId, detail, kind, onRetry}: RecoveryPageProps) {
  const content = recoveryContent[kind]

  return (
    <section aria-labelledby={`recovery-${kind}`} className="recovery-page">
      <p className="eyebrow">Recovery boundary</p>
      <h1 id={`recovery-${kind}`}>{content.title}</h1>
      <p>{detail ?? content.detail}</p>
      {correlationId === undefined || correlationId === null ? null : (
        <p className="recovery-page__reference">Support reference: {correlationId}</p>
      )}
      {onRetry === undefined ? null : (
        <button className="primary-action" onClick={onRetry} type="button">
          {actionLabel ?? "Try again"}
        </button>
      )}
    </section>
  )
}
