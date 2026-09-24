import {useEffect, useState} from "react"

import type {ConsoleEnvelopeDashboardView, DashboardView, DataProvenance} from "../../api/generated"
import {CapabilityState} from "../../components/capability-state"

// A preview is a still image served from the console's own origin under an opaque reference.
// The browser never receives or constructs a Superset origin.
const publicIdPattern = /^[a-z][a-z0-9_-]{2,127}$/

export interface DashboardPreviewClient {
  getDashboard(dashboardRef: string): Promise<ConsoleEnvelopeDashboardView>
}

interface DashboardPreviewProps {
  readonly client: DashboardPreviewClient
  readonly dashboardRef: string
  readonly dataProvenance: DataProvenance
  readonly onAvailabilityChange?: (available: boolean) => void
}

export function DashboardPreview({
  client,
  dashboardRef,
  dataProvenance,
  onAvailabilityChange,
}: DashboardPreviewProps) {
  const [dashboard, setDashboard] = useState<{
    readonly failed: boolean
    readonly value: DashboardView | null
  }>({failed: false, value: null})
  const [renderFailed, setRenderFailed] = useState(false)

  useEffect(() => {
    let active = true
    void client
      .getDashboard(dashboardRef)
      .then((envelope) => {
        if (!active) {
          return
        }
        if (
          envelope.meta.data_provenance !== dataProvenance ||
          envelope.data.dashboard_ref !== dashboardRef
        ) {
          setDashboard({failed: true, value: null})
          onAvailabilityChange?.(false)
          return
        }
        setRenderFailed(false)
        setDashboard({failed: false, value: envelope.data})
        const previewRef = envelope.data.preview_ref
        if (previewRef === null || previewRef === undefined || !publicIdPattern.test(previewRef)) {
          onAvailabilityChange?.(false)
        }
      })
      .catch(() => {
        if (!active) {
          return
        }
        setDashboard({failed: true, value: null})
        onAvailabilityChange?.(false)
      })
    return () => {
      active = false
    }
  }, [client, dashboardRef, dataProvenance, onAvailabilityChange])

  if (dashboard.failed) {
    return (
      <p className="inbox-unavailable" role="status">
        Dashboard preview unavailable. The surrounding review is unchanged.
      </p>
    )
  }
  if (dashboard.value === null) {
    return (
      <p className="inbox-loading" role="status">
        Loading the dashboard candidate…
      </p>
    )
  }

  const previewRef = dashboard.value.preview_ref
  const linkRef = dashboard.value.link_ref
  const previewUrl =
    previewRef !== null && previewRef !== undefined && publicIdPattern.test(previewRef)
      ? `/api/v1/previews/${encodeURIComponent(previewRef)}`
      : null

  return (
    <article className="dashboard-preview">
      <h4>{dashboard.value.display_name}</h4>
      <p>{dashboard.value.summary}</p>
      <CapabilityState state={dashboard.value.state} />
      {previewUrl !== null && !renderFailed ? (
        <img
          alt={`Rendered preview of ${dashboard.value.display_name}`}
          className="dashboard-preview__image"
          onError={() => {
            setRenderFailed(true)
            onAvailabilityChange?.(false)
          }}
          onLoad={() => onAvailabilityChange?.(true)}
          src={previewUrl}
        />
      ) : (
        <p className="inbox-unavailable" role="status">
          Dashboard preview unavailable. The surrounding review is unchanged.
        </p>
      )}
      {linkRef === null || linkRef === undefined || !publicIdPattern.test(linkRef) ? (
        <p className="inbox-empty">No authorized dashboard link was issued.</p>
      ) : (
        <a href={`/api/v1/links/${encodeURIComponent(linkRef)}`} rel="noreferrer">
          Open {dashboard.value.display_name} in Superset
        </a>
      )}
    </article>
  )
}
