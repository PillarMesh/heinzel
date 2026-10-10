import {useEffect, useRef, useState} from "react"

import type {OperationView} from "../../api/generated"
import type {SetupClient} from "./setup-workbench"

const pollIntervals = [1_000, 2_000, 4_000, 8_000, 15_000] as const

export interface PollTimer {
  clear(handle: PollTimerHandle): void
  set(callback: () => void, delay: number): PollTimerHandle
}

type PollTimerHandle = number | ReturnType<typeof globalThis.setTimeout>

const browserPollTimer: PollTimer = {
  clear: (handle) => globalThis.clearTimeout(handle),
  set: (callback, delay) => globalThis.setTimeout(callback, delay),
}

interface OperationStatusProps {
  readonly client: Pick<SetupClient, "getOperation">
  readonly label: string
  readonly onSettled?: ((operation: OperationView) => void) | undefined
  readonly operation: OperationView
  readonly pollTimer?: PollTimer | undefined
}

function isPending(operation: OperationView): boolean {
  return (
    operation.state === "accepted" ||
    operation.state === "running" ||
    operation.state === "outcome_unknown"
  )
}

function operationTitle(operation: OperationView): string {
  if (operation.state === "accepted") {
    return "Request accepted"
  }
  if (operation.state === "running") {
    return "In progress"
  }
  if (operation.state === "outcome_unknown") {
    return "Operation outcome unknown"
  }
  if (operation.state === "succeeded") {
    return "Completed"
  }
  return "Action failed"
}

function technicalLabel(value: string | undefined): string {
  return value?.replaceAll("_", " ") ?? "Not reported"
}

export function OperationStatus({
  client,
  label,
  onSettled,
  operation,
  pollTimer = browserPollTimer,
}: OperationStatusProps) {
  const [currentOperation, setCurrentOperation] = useState(operation)
  // Held in a ref rather than named as a dependency below. A caller that passes an inline
  // arrow - which every caller does - hands this a new function on every render, and the
  // announcement re-reads the projections, which re-renders, which hands it another one.
  // That is a loop with a network call in it: measured at five thousand reads in ten
  // seconds, each one cancelling the last, so the page that asked for fresh data never
  // received any.
  const announce = useRef(onSettled)
  useEffect(() => {
    announce.current = onSettled
  })
  // And announced once per settled outcome, not once per render that happens to see one.
  const announced = useRef<string | null>(null)

  // The surfaces this operation changed - the stage list, the governance spine, the
  // workspace state - belong to the shell, not to this component. Announcing the
  // settlement lets the shell re-read them; without it the page kept offering the
  // command it had just completed.
  useEffect(() => {
    if (isPending(currentOperation)) {
      return
    }
    const settlement = `${currentOperation.operation_id}:${currentOperation.state}:${currentOperation.revision}`
    if (announced.current === settlement) {
      return
    }
    announced.current = settlement
    announce.current?.(currentOperation)
  }, [currentOperation])

  useEffect(() => {
    if (!isPending(operation)) {
      return undefined
    }
    let active = true
    let intervalIndex = 0
    let timerHandle: PollTimerHandle | undefined

    const scheduleNext = () => {
      const delay = pollIntervals[Math.min(intervalIndex, pollIntervals.length - 1)] ?? 15_000
      intervalIndex += 1
      timerHandle = pollTimer.set(() => void refresh(), delay)
    }
    const refresh = async () => {
      if (!active) {
        return
      }
      try {
        const response = await client.getOperation(operation.operation_id)
        if (!active) {
          return
        }
        setCurrentOperation(response.data)
        if (isPending(response.data)) {
          scheduleNext()
        }
      } catch {
        if (active) {
          scheduleNext()
        }
      }
    }

    scheduleNext()
    return () => {
      active = false
      if (timerHandle !== undefined) {
        pollTimer.clear(timerHandle)
      }
    }
  }, [client, operation, pollTimer])

  return (
    <section
      aria-label={label}
      className={`operation-status operation-status--${currentOperation.state}`}
      role="status"
    >
      <p className="operation-status__title">{operationTitle(currentOperation)}</p>
      <p>{currentOperation.summary}</p>
      <details>
        <summary>Technical details</summary>
        <dl>
          <div>
            <dt>Operation reference</dt>
            <dd>{currentOperation.operation_id}</dd>
          </div>
          <div>
            <dt>Phase</dt>
            <dd>{technicalLabel(currentOperation.phase)}</dd>
          </div>
        </dl>
      </details>
    </section>
  )
}
