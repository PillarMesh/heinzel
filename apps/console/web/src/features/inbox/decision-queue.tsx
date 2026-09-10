import {useEffect, useRef, useState, type KeyboardEvent} from "react"

import type {InboxItemView, RequestKind, RequestState, RiskLevel} from "../../api/generated"

const requestKindLabels = {
  stakeholder_question: "Stakeholder question",
  data_access: "Data access",
} satisfies Record<RequestKind, string>

const riskLabels = {
  low: "Low risk",
  medium: "Medium risk",
  high: "High risk",
  critical: "Critical risk",
} satisfies Record<RiskLevel, string>

const requestStates = [
  "submitted",
  "clarifying",
  "investigating",
  "proposed",
  "awaiting_approval",
  "execution_ready",
  "denied",
  "delivered",
  "no_valid_plan",
  "cancelled",
  "failed",
  "closed",
] as const satisfies readonly RequestState[]

// The queue can only mark a blocked item as blocked on the requester while the request is still
// in clarification, because that is the only lifecycle state whose outstanding authority is the
// requester's acceptance of the clarified outcome. Every other blocked reason stays unattributed.
function blockedLabel(item: InboxItemView): string | null {
  if (item.blocked_reason === null || item.blocked_reason === undefined) {
    return null
  }
  return item.state === "clarifying" ? "Blocked on requester" : "Blocked"
}

interface DecisionQueueProps {
  readonly focusRequestId?: string | null
  readonly focusToken?: number
  readonly items: readonly InboxItemView[]
  readonly onActivate: (requestId: string) => void
  readonly onSelect: (requestId: string) => void
  readonly selectedRequestId: string | null
}

export function DecisionQueue({
  focusRequestId = null,
  focusToken = 0,
  items,
  onActivate,
  onSelect,
  selectedRequestId,
}: DecisionQueueProps) {
  const [kindFilter, setKindFilter] = useState<RequestKind | "all">("all")
  const [stateFilter, setStateFilter] = useState<RequestState | "all">("all")
  const optionRefs = useRef(new Map<string, HTMLLIElement>())

  // Restores queue focus after a narrow-width detail route returns to the queue.
  useEffect(() => {
    if (focusToken === 0 || focusRequestId === null) {
      return
    }
    optionRefs.current.get(focusRequestId)?.focus()
  }, [focusRequestId, focusToken])

  // Server order is authoritative; filtering never reorders.
  const visibleItems = items.filter(
    (item) =>
      (kindFilter === "all" || item.kind === kindFilter) &&
      (stateFilter === "all" || item.state === stateFilter),
  )
  const selectedIndex = visibleItems.findIndex((item) => item.request_id === selectedRequestId)
  const rovingIndex = selectedIndex === -1 ? 0 : selectedIndex

  function moveSelection(nextIndex: number): void {
    const next = visibleItems[nextIndex]
    if (next === undefined) {
      return
    }
    onSelect(next.request_id)
    optionRefs.current.get(next.request_id)?.focus()
  }

  function handleKeyDown(event: KeyboardEvent<HTMLUListElement>): void {
    if (visibleItems.length === 0) {
      return
    }
    if (event.key === "ArrowDown") {
      event.preventDefault()
      moveSelection(Math.min(rovingIndex + 1, visibleItems.length - 1))
      return
    }
    if (event.key === "ArrowUp") {
      event.preventDefault()
      moveSelection(Math.max(rovingIndex - 1, 0))
      return
    }
    if (event.key === "Home") {
      event.preventDefault()
      moveSelection(0)
      return
    }
    if (event.key === "End") {
      event.preventDefault()
      moveSelection(visibleItems.length - 1)
      return
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault()
      const current = visibleItems[rovingIndex]
      if (current !== undefined) {
        onSelect(current.request_id)
        onActivate(current.request_id)
      }
    }
  }

  return (
    <section aria-label="Decision queue" className="decision-queue">
      <h2>Decision queue</h2>
      <div className="decision-queue__filters">
        <label>
          <span>Request type</span>
          <select
            onChange={(event) => setKindFilter(event.currentTarget.value as RequestKind | "all")}
            value={kindFilter}
          >
            <option value="all">All request types</option>
            <option value="stakeholder_question">Stakeholder question</option>
            <option value="data_access">Data access</option>
          </select>
        </label>
        <label>
          <span>Lifecycle state</span>
          <select
            onChange={(event) => setStateFilter(event.currentTarget.value as RequestState | "all")}
            value={stateFilter}
          >
            <option value="all">All lifecycle states</option>
            {requestStates.map((state) => (
              <option key={state} value={state}>
                {state.replaceAll("_", " ")}
              </option>
            ))}
          </select>
        </label>
      </div>
      {visibleItems.length === 0 ? (
        <p className="inbox-empty">No request matches the current filters.</p>
      ) : (
        <ul
          aria-label="Prioritized requests"
          className="decision-queue__items"
          onKeyDown={handleKeyDown}
          role="listbox"
        >
          {visibleItems.map((item, index) => {
            const blocked = blockedLabel(item)
            const selected = item.request_id === selectedRequestId
            return (
              <li
                aria-selected={selected}
                className={`decision-queue__item decision-queue__item--${item.risk}`}
                key={item.request_id}
                onClick={() => onSelect(item.request_id)}
                ref={(element) => {
                  if (element === null) {
                    optionRefs.current.delete(item.request_id)
                    return
                  }
                  optionRefs.current.set(item.request_id, element)
                }}
                role="option"
                tabIndex={index === rovingIndex ? 0 : -1}
              >
                <strong>{item.title}</strong>
                <span className="decision-queue__meta">
                  {requestKindLabels[item.kind]} · {riskLabels[item.risk]} ·{" "}
                  {item.state.replaceAll("_", " ")}
                </span>
                <span className="decision-queue__purpose">{item.purpose}</span>
                {item.deadline === null || item.deadline === undefined ? null : (
                  <span className="decision-queue__deadline">
                    Due <time dateTime={item.deadline}>{item.deadline}</time>
                  </span>
                )}
                {blocked === null ? null : (
                  <span className="decision-queue__blocked">
                    {blocked}: {item.blocked_reason}
                  </span>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
