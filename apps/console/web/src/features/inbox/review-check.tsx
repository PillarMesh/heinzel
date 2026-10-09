/**
 * One thing an approver has to be satisfied about, and whether it is satisfied.
 *
 * The proposal stated its grounds as a definition list: six labels in small caps against six
 * values in prose, every one weighted the same, none of them saying whether what it reported
 * was good or bad. An approver reading it had to know in advance which values were the ones
 * that could stop an approval. A check says so: the mark carries the state, the summary says
 * it in a few words, and what is only needed when the answer is unexpected sits underneath.
 */
import type {ReactNode} from "react"

/** `attention` is not a failure -- it is the row that has to be read before approving. */
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
  /** Shown under the summary: the detail that matters when the summary is not the expected one. */
  readonly detail?: ReactNode
  readonly label: string
  readonly summary: ReactNode
  readonly tone: CheckTone
}

export function ReviewCheck({detail, label, summary, tone}: ReviewCheckProps) {
  return (
    <li className={`review-check review-check--${tone}`}>
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
      <span className="review-check__summary">
        {summary}
        {/* The mark's meaning in words, because a shape and a colour are not a reading. */}
        <span className="visually-hidden"> — {SPOKEN[tone]}</span>
      </span>
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
