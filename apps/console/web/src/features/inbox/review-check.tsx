/**
 * One thing an approver has to be satisfied about, as a card rather than a line.
 *
 * The proposal stated its grounds as a definition list: six labels in small caps against six
 * values in prose, every one weighted the same, none of them saying whether what it reported
 * was good or bad. Turning them into checks said which were satisfied; turning the checks into
 * cards stops them being a column of sentences. Four cards fill the width the panel actually
 * has, in half the height, and each is a thing a reader can look at rather than read through.
 */
import type {ReactNode} from "react"

/** `attention` is not a failure -- it is the card that has to be read before approving. */
export type CheckTone = "ready" | "attention" | "neutral"

const SPOKEN: Readonly<Record<CheckTone, string>> = {
  ready: "satisfied",
  attention: "needs attention",
  neutral: "not applicable",
}

const ARTWORK: Readonly<Record<CheckTone, string>> = {
  ready: "M3.6 8.3l2.9 2.9 5.9-6",
  attention: "M8 3.4v5.2M8 11.6v.1",
  neutral: "M4 8h8",
}

interface ReviewCheckProps {
  /** Under the value: what matters when the value is not the expected one. Kept to a line. */
  readonly detail?: ReactNode
  readonly label: string
  readonly summary: ReactNode
  readonly tone: CheckTone
}

export function ReviewCheck({detail, label, summary, tone}: ReviewCheckProps) {
  return (
    <li className={`review-check review-check--${tone}`}>
      <p className="review-check__head">
        <span aria-hidden="true" className="review-check__mark">
          <svg
            fill="none"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={1.8}
            viewBox="0 0 16 16"
          >
            <path d={ARTWORK[tone]} />
          </svg>
        </span>
        <span className="review-check__label">{label}</span>
        {/* The mark's meaning in words, because a shape and a colour are not a reading. */}
        <span className="visually-hidden"> — {SPOKEN[tone]}</span>
      </p>
      <p className="review-check__summary">{summary}</p>
      {detail === undefined ? null : <div className="review-check__detail">{detail}</div>}
    </li>
  )
}

export function ReviewChecks({children}: {readonly children: ReactNode}) {
  return (
    <ul aria-label="Readiness" className="review-checks">
      {children}
    </ul>
  )
}
