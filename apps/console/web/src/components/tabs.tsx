import {useId, useRef, useState, type KeyboardEvent, type ReactNode} from "react"

/**
 * One job at a time.
 *
 * The decision workspace stacked five unrelated jobs in a single scrolling column -- read the
 * question, read the impact, read the proposal, hold a conversation, record a decision -- which
 * ran 2.6 screens and grouped nothing. Tabs are the plain answer: the reader is doing one of
 * those things at a time, and the rest are a click away rather than a scroll away.
 *
 * Keyboard behaviour follows the APG tabs pattern: one stop in the tab order, arrows move
 * between tabs, Home and End jump to the ends, and selection follows focus because every panel
 * here is already loaded.
 */
export interface TabDefinition {
  /** Rendered lazily, so a tab nobody opens costs nothing. */
  readonly content: () => ReactNode
  /** A count or a dot beside the label: what is waiting behind a tab the reader cannot see. */
  readonly badge?: ReactNode
  readonly id: string
  readonly label: string
}

interface TabsProps {
  readonly ariaLabel: string
  /** The tab to open first. Falls back to the first tab when it names none of them. */
  readonly initial?: string
  /**
   * The section the reader moved to, for a layout that depends on which one is open.
   *
   * Reported on a change and never on mount, so nothing outside has to be told what it can
   * work out for itself -- a caller that cares starts from the same `initial` this does.
   */
  readonly onSelect?: (id: string) => void
  readonly tabs: readonly TabDefinition[]
}

export function Tabs({ariaLabel, initial, onSelect, tabs}: TabsProps) {
  const base = useId()
  const first = tabs[0]
  const [selected, setSelected] = useState(() =>
    initial !== undefined && tabs.some((tab) => tab.id === initial) ? initial : (first?.id ?? ""),
  )
  const buttons = useRef(new Map<string, HTMLButtonElement>())

  // A tab that disappears -- an action completed, a section that no longer applies -- must not
  // leave the strip with nothing selected and the panel area empty.
  const current = tabs.find((tab) => tab.id === selected) ?? first
  if (current === undefined) {
    return null
  }

  function select(id: string): void {
    setSelected(id)
    onSelect?.(id)
  }

  function move(toIndex: number): void {
    const next = tabs[Math.max(0, Math.min(toIndex, tabs.length - 1))]
    if (next === undefined) {
      return
    }
    select(next.id)
    buttons.current.get(next.id)?.focus()
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    const index = tabs.findIndex((tab) => tab.id === current?.id)
    const keys: Record<string, number | undefined> = {
      ArrowRight: index + 1,
      ArrowLeft: index - 1,
      Home: 0,
      End: tabs.length - 1,
    }
    const target = keys[event.key]
    if (target === undefined) {
      return
    }
    event.preventDefault()
    move(target)
  }

  return (
    <div className="tabs">
      <div aria-label={ariaLabel} className="tabs__strip" onKeyDown={handleKeyDown} role="tablist">
        {tabs.map((tab) => {
          const active = tab.id === current.id
          return (
            <button
              aria-controls={`${base}-${tab.id}-panel`}
              aria-selected={active}
              className="tabs__tab"
              id={`${base}-${tab.id}-tab`}
              key={tab.id}
              onClick={() => select(tab.id)}
              ref={(element) => {
                if (element === null) {
                  buttons.current.delete(tab.id)
                  return
                }
                buttons.current.set(tab.id, element)
              }}
              role="tab"
              tabIndex={active ? 0 : -1}
              type="button"
            >
              <span>{tab.label}</span>
              {tab.badge === undefined ? null : <span className="tabs__badge">{tab.badge}</span>}
            </button>
          )
        })}
      </div>
      <div
        aria-labelledby={`${base}-${current.id}-tab`}
        className="tabs__panel"
        id={`${base}-${current.id}-panel`}
        role="tabpanel"
        tabIndex={0}
      >
        {current.content()}
      </div>
    </div>
  )
}
