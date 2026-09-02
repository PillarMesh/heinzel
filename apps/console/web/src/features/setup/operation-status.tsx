import {useEffect, useState} from "react"

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
    return "Provisioning accepted"
  }
  if (operation.state === "running") {
    return "Provisioning in progress"
  }
  if (operation.state === "outcome_unknown") {
    return "Operation outcome unknown"
  }
  if (operation.state === "succeeded") {
    return "Provisioning succeeded"
  }
  return "Provisioning failed"
}

export function OperationStatus({
  client,
  label,
  operation,
  pollTimer = browserPollTimer,
}: OperationStatusProps) {
  const [currentOperation, setCurrentOperation] = useState(operation)

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
      <dl>
        <div>
          <dt>Operation</dt>
          <dd>{currentOperation.operation_id}</dd>
        </div>
        <div>
          <dt>Phase</dt>
          <dd>{currentOperation.phase}</dd>
        </div>
      </dl>
    </section>
  )
}
