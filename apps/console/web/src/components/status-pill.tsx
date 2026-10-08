import "./status-pill.css"

/** What a pill is saying, in the product's lifecycle vocabulary rather than in colours. */
export type PillTone = "ready" | "attention" | "danger" | "neutral"

interface StatusPillProps {
  readonly children: React.ReactNode
  readonly tone: PillTone
}

/**
 * One badge, everywhere a state is shown.
 *
 * Approvals were reporting themselves as bold amber words in running text while capabilities a
 * few hundred pixels away used a filled pill, so the same kind of fact had two appearances and
 * neither was obviously a status. The mark is a shape as well as a colour: a reader who cannot
 * separate the tints still has the ring for an absence and the dot for a reading.
 */
export function StatusPill({children, tone}: StatusPillProps) {
  return (
    <span className={`pill pill--${tone}`}>
      <span aria-hidden="true" className="pill__mark" />
      {children}
    </span>
  )
}
