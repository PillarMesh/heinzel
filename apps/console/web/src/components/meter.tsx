/**
 * A part of a whole, as a bar rather than a sentence.
 *
 * `8 of 16 ready` is a fraction the reader has to divide before it means anything, and it sits
 * on every page of the console. The bar does the division. The sentence stays beside it --
 * the bar carries the proportion, the words carry the exact counts, and neither is the only
 * copy of the other.
 */
import type {CSSProperties} from "react"

interface MeterProps {
  /** What the bar is measuring, for the reader who gets the value without seeing the bar. */
  readonly label: string
  readonly of: number
  /**
   * Which ramp the fill is drawn from. `progress` is work advancing and carries no judgement;
   * `ready` says the whole is accounted for and nothing is outstanding.
   */
  readonly tone?: "progress" | "ready"
  readonly value: number
}

export function Meter({label, of, tone = "progress", value}: MeterProps) {
  const bounded = of <= 0 ? 0 : Math.max(0, Math.min(value / of, 1))
  return (
    <div
      aria-label={label}
      aria-valuemax={of}
      aria-valuemin={0}
      aria-valuenow={value}
      aria-valuetext={`${value} of ${of}`}
      className={`meter meter--${bounded >= 1 ? "ready" : tone}`}
      role="meter"
    >
      <span
        className="meter__fill"
        style={{"--meter-fill": `${bounded * 100}%`} as CSSProperties}
      />
    </div>
  )
}
