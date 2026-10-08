import {formatInstant} from "../../format/instant"
import type {LifecycleEventView} from "../../api/generated"

interface LifecycleTimelineProps {
  readonly events: readonly LifecycleEventView[]
}

export function LifecycleTimeline({events}: LifecycleTimelineProps) {
  if (events.length === 0) {
    return <p className="inbox-empty">No lifecycle events have been recorded.</p>
  }
  return (
    <ol aria-label="Lifecycle history" className="lifecycle-timeline">
      {events.map((event) => (
        <li key={event.event_id}>
          <strong>{event.state.replaceAll("_", " ")}</strong>
          <span>{event.summary}</span>
          <time dateTime={event.occurred_at}>{formatInstant(event.occurred_at)}</time>
        </li>
      ))}
    </ol>
  )
}
