import type {ReactNode} from "react"

import type {PageFailureState} from "../format/failure"

/**
 * A page that has nothing to show, shown deliberately.
 *
 * Six of the nine navigation items lead here in governed-local mode, and each was a heading, a
 * subtitle and one sentence of apology on nine hundred pixels of blank canvas -- twenty-seven
 * words on a page. The capability is genuinely absent and the console should say so, but an
 * absence that is stated once, in a bounded card, with somewhere to go next, reads as a
 * deployment boundary rather than as a page that failed to load.
 */
interface UnavailableProps {
  /** Where the reader can go instead, when there is somewhere. */
  readonly children?: ReactNode
  /** What this surface would show if the capability were delivered. */
  readonly expects: string
  /** The service's own reason, shown as given. */
  readonly reason: string
}

export function Unavailable({children, expects, reason}: UnavailableProps) {
  return (
    <div className="unavailable">
      <div className="unavailable__card">
        <p className="unavailable__badge">Not delivered in this deployment</p>
        <p className="unavailable__expects">{expects}</p>
        <p className="unavailable__reason">{reason}</p>
        {children === undefined ? null : <div className="unavailable__onward">{children}</div>}
      </div>
    </div>
  )
}

/**
 * A page's failure, told apart from a page's boundary.
 *
 * `capability_not_delivered` is not an error: it is this deployment saying it does not run
 * that service. Rendered as `<p role="alert">` beside the heading it read as something having
 * gone wrong, on six of the nine pages the navigation offers.
 */
export function PageFailure({
  children,
  expects,
  failure,
}: {
  readonly children?: ReactNode
  readonly expects: string
  readonly failure: PageFailureState | null
}) {
  if (failure === null) {
    return null
  }
  if (failure.code === "capability_not_delivered") {
    return (
      <Unavailable expects={expects} reason={failure.message}>
        {children}
      </Unavailable>
    )
  }
  return <p role="alert">{failure.message}</p>
}
