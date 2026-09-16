import {useEffect, useRef, useState, type ReactNode} from "react"
import {NavLink, useLocation} from "react-router-dom"

import type {DataProvenance, SessionView, WorkspaceView} from "../api/generated"
import {CapabilityState} from "./capability-state"
import {ErrorBoundary} from "./error-boundary"
import {ModeBanner} from "./mode-banner"
import {SkipLink} from "./skip-link"

type LayoutMode = "wide" | "drawer" | "queue-detail"

const productNavigation = [
  {label: "Workspace setup", to: "/setup"},
  {label: "Inbox", to: "/inbox"},
  {label: "Data products", to: "/data-products"},
  {label: "Runs", to: "/runs"},
  {label: "Incidents", to: "/operations"},
  {label: "Acquisition", to: "/acquisition-receipts"},
  {label: "Catalog", to: "/catalog"},
  {label: "Dashboards", to: "/dashboards"},
  {label: "Evidence", to: "/evidence"},
] as const

function mediaMatches(query: string): boolean {
  return typeof globalThis.matchMedia === "function" && globalThis.matchMedia(query).matches
}

function currentLayout(): LayoutMode {
  if (mediaMatches("(max-width: 759px)")) {
    return "queue-detail"
  }
  return mediaMatches("(max-width: 1119px)") ? "drawer" : "wide"
}

function useMediaState() {
  const [layout, setLayout] = useState<LayoutMode>(currentLayout)
  const [reducedMotion, setReducedMotion] = useState(() =>
    mediaMatches("(prefers-reduced-motion: reduce)"),
  )

  useEffect(() => {
    if (typeof globalThis.matchMedia !== "function") {
      return undefined
    }
    const medium = globalThis.matchMedia("(max-width: 1119px)")
    const narrow = globalThis.matchMedia("(max-width: 759px)")
    const motion = globalThis.matchMedia("(prefers-reduced-motion: reduce)")
    const update = () => {
      setLayout(narrow.matches ? "queue-detail" : medium.matches ? "drawer" : "wide")
      setReducedMotion(motion.matches)
    }
    medium.addEventListener("change", update)
    narrow.addEventListener("change", update)
    motion.addEventListener("change", update)
    return () => {
      medium.removeEventListener("change", update)
      narrow.removeEventListener("change", update)
      motion.removeEventListener("change", update)
    }
  }, [])

  return {layout, reducedMotion}
}

interface AppShellProps {
  readonly children: ReactNode
  readonly dataProvenance: DataProvenance
  readonly evidence?: ReactNode
  readonly queue?: ReactNode
  readonly session: SessionView
  readonly workspace: WorkspaceView
}

export function AppShell({
  children,
  dataProvenance,
  evidence,
  queue,
  session,
  workspace,
}: AppShellProps) {
  const {layout, reducedMotion} = useMediaState()
  const location = useLocation()
  const mainRef = useRef<HTMLElement>(null)
  const routeIdentity = `${location.key}:${location.pathname}`
  const previousRoute = useRef(routeIdentity)
  const [routeAnnouncement, setRouteAnnouncement] = useState("")
  const hasQueue = queue !== undefined
  const hasEvidence = evidence !== undefined
  const slotClasses = [
    hasQueue ? "responsive-workspace--has-queue" : null,
    hasEvidence ? "responsive-workspace--has-evidence" : null,
    !hasQueue && !hasEvidence ? "responsive-workspace--no-slots" : null,
  ]
    .filter((className): className is string => className !== null)
    .join(" ")

  useEffect(() => {
    if (previousRoute.current === routeIdentity) {
      return
    }
    previousRoute.current = routeIdentity
    const main = mainRef.current
    if (main === null) {
      return
    }
    main.focus()
    setRouteAnnouncement(main.querySelector("h1")?.textContent ?? "Current work")
  }, [routeIdentity])

  return (
    <div className="app-shell">
      <SkipLink />
      <header className="app-shell__rail">
        <div className="app-shell__identity">
          <p className="app-shell__wordmark">PillarMesh</p>
          <p>{session.workspace.display_name}</p>
          <span>{session.active_role.replaceAll("_", " ")}</span>
        </div>
        <nav aria-label="Product" className="product-navigation">
          {(session.active_role === "requester" ? [{label: "My requests", to: "/requests"}] : productNavigation).map((item) => (
            <NavLink className="product-navigation__link" key={item.to} to={item.to}>
              {item.label}
            </NavLink>
          ))}
        </nav>
        {session.active_role === "requester" ? null : <aside aria-label="Governance spine" className="governance-spine">
          <p className="governance-spine__title">Governance spine</p>
          <ol>
            {(workspace.capabilities ?? []).map((capability) => (
              <li key={capability.capability_id}>
                <span>{capability.label}</span>
                <CapabilityState state={capability.state} />
                {capability.dependency === null || capability.dependency === undefined ? null : (
                  <small>{capability.dependency}</small>
                )}
              </li>
            ))}
          </ol>
        </aside>}
      </header>
      <div className="app-shell__surface">
        <ModeBanner dataProvenance={dataProvenance} />
        <main
          aria-label="PillarMesh console"
          data-motion={reducedMotion ? "reduced" : "full"}
          id="main-content"
          ref={mainRef}
          tabIndex={-1}
        >
          <div
            className={`responsive-workspace responsive-workspace--${layout} ${slotClasses}`}
            data-layout={layout}
            data-testid="responsive-workspace"
          >
            {queue === undefined ? null : layout === "queue-detail" ? (
              <details className="responsive-disclosure responsive-disclosure--queue">
                <summary>Show decision queue</summary>
                {queue}
              </details>
            ) : (
              <aside aria-label="Decision queue" className="responsive-workspace__queue">
                {queue}
              </aside>
            )}
            <ErrorBoundary key={routeIdentity}>
              <div className="responsive-workspace__content">{children}</div>
            </ErrorBoundary>
            {evidence === undefined ? null : layout === "wide" ? (
              <aside aria-label="Evidence" className="responsive-workspace__evidence">
                {evidence}
              </aside>
            ) : (
              <details className="responsive-disclosure responsive-disclosure--evidence">
                <summary>Show evidence</summary>
                {evidence}
              </details>
            )}
          </div>
          <p aria-label="Route change" aria-live="polite" className="visually-hidden" role="status">
            {routeAnnouncement}
          </p>
        </main>
      </div>
    </div>
  )
}
