import {useId} from "react"
import type {ReactNode} from "react"

/**
 * The surface a section of work sits on.
 *
 * The console had surfaces for the things beside the work -- the queue, the evidence rail --
 * and none for the work itself, so the centre of the most important screen was a column of
 * bare headings on the page background. A panel gives a section an edge, a header and one
 * padding, which is what makes a page read as an application rather than as a document.
 */
interface PanelProps {
  /**
   * The section's accessible name, when it must differ from its visible title -- or stand in
   * for one, on a panel drawn without a header. A panel with a visible title needs neither:
   * the title names it.
   */
  readonly ariaLabel?: string
  /** A control or status that belongs to this section, placed on the header's trailing edge. */
  readonly aside?: ReactNode
  readonly children: ReactNode
  readonly className?: string
  /** The sentence under the title, when the title alone does not say what this is for. */
  readonly description?: ReactNode
  /** The heading level. A panel inside a tab is one level below the tab's own page heading. */
  readonly headingLevel?: 2 | 3 | 4
  readonly id?: string
  /** Drops the padding, for a panel whose child draws to its own edges (a table, a chart). */
  readonly flush?: boolean
  readonly title?: ReactNode
}

export function Panel({
  ariaLabel,
  aside,
  children,
  className,
  description,
  flush = false,
  headingLevel = 3,
  id,
  title,
}: PanelProps) {
  const Heading = `h${headingLevel}` as const
  // A `section` is a landmark only once it is named, and an unnamed one is skipped entirely by
  // the rotor every screen reader offers -- so a panel with a visible title is named by it.
  const headingId = useId()
  const classes = ["panel", flush ? "panel--flush" : null, className]
    .filter((value): value is string => typeof value === "string" && value !== "")
    .join(" ")
  return (
    <section
      aria-label={ariaLabel}
      aria-labelledby={ariaLabel === undefined && title !== undefined ? headingId : undefined}
      className={classes}
      id={id}
    >
      {title === undefined ? null : (
        <header className="panel__header">
          <div className="panel__heading">
            <Heading className="panel__title" id={headingId}>
              {title}
            </Heading>
            {description === undefined ? null : <p className="panel__description">{description}</p>}
          </div>
          {aside === undefined ? null : <div className="panel__aside">{aside}</div>}
        </header>
      )}
      <div className="panel__body">{children}</div>
    </section>
  )
}

/**
 * What a panel says when it has nothing to show.
 *
 * Fourteen of these were loose sentences on one screen, each phrased differently, so the page's
 * dominant content was an inventory of what it did not have. One quiet, uniform treatment makes
 * an absence read as an absence rather than as a paragraph.
 */
export function Nothing({children}: {readonly children: ReactNode}) {
  return <p className="panel__nothing">{children}</p>
}
