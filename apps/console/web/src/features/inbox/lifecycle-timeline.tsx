import {formatInstant, formatTimeOfDay} from "../../format/instant"
import type {LifecycleEventView} from "../../api/generated"

interface LifecycleTimelineProps {
  readonly events: readonly LifecycleEventView[]
}

/** How many entries stand open before the rest are folded away. */
const SHOWN = 3

/**
 * The history, as a history rather than as a paragraph each.
 *
 * Six transitions, each three lines tall with a full date-and-time apiece, made the history
 * the longest thing on the page -- in its narrowest column. The server's account of each
 * transition stays, because the server is the authority on what happened; the state and the
 * time share one line, the date comes off (every entry on a request shares it), and all but
 * the last few fold away.
 */
export function LifecycleTimeline({events}: LifecycleTimelineProps) {
  if (events.length === 0) {
    return <p className="panel__nothing">No lifecycle events have been recorded.</p>
  }
  const latest = events.slice(-SHOWN)
  const earlier = events.slice(0, Math.max(0, events.length - SHOWN))
  return (
    <>
      {earlier.length === 0 ? null : (
        <details className="lifecycle-timeline__earlier">
          <summary>
            {earlier.length} earlier {earlier.length === 1 ? "event" : "events"}
          </summary>
          <Entries events={earlier} />
        </details>
      )}
      <Entries events={latest} />
    </>
  )
}

function Entries({events}: LifecycleTimelineProps) {
  return (
    <ol aria-label="Lifecycle history" className="lifecycle-timeline">
      {events.map((event) => (
        <li key={event.event_id}>
          <div className="lifecycle-timeline__head">
            <strong>{event.state.replaceAll("_", " ")}</strong>
            {/*
              The full instant stays on the element for anything that parses the page, and on
              hover for anyone who wants the date; the line shows the time of day, because
              every entry on a request shares its date and repeating it six times said nothing.
            */}
            <time dateTime={event.occurred_at} title={formatInstant(event.occurred_at)}>
              {formatTimeOfDay(event.occurred_at)}
            </time>
          </div>
          {/* The server's own account of the transition. It is the authority on what happened. */}
          <span>{event.summary}</span>
        </li>
      ))}
    </ol>
  )
}
