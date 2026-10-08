import {formatInstant} from "../../format/instant"
import {useEffect, useState} from "react"

import {ConsoleApiError} from "../../api/client"
import type {
  ConsoleEnvelopeDashboardView,
  ConsoleEnvelopeDashboardsView,
  DashboardView,
} from "../../api/generated"
import {CapabilityState} from "../../components/capability-state"

export interface DashboardsClient {
  getDashboard(dashboardRef: string): Promise<ConsoleEnvelopeDashboardView>
  getDashboards(): Promise<ConsoleEnvelopeDashboardsView>
}

interface DashboardsPageProps {
  readonly client: DashboardsClient
  readonly dashboardRef?: string
}

const freshnessLabels: Readonly<Record<DashboardView["freshness"], string>> = {
  current: "Current",
  stale: "Stale",
  unknown: "Freshness unknown",
  not_applicable: "Not applicable",
}

const accessLabels: Readonly<Record<DashboardView["access_state"], string>> = {
  active: "Active",
  workspace_role: "Workspace role",
}

function DashboardRecord({
  dashboard,
  showTitle = true,
}: {
  readonly dashboard: DashboardView
  readonly showTitle?: boolean
}) {
  return (
    <>
      <div>
        {showTitle ? <h2>{dashboard.display_name}</h2> : null}
        <p>{dashboard.summary}</p>
        <dl className="dashboard-record__facts">
          <div>
            <dt>Published</dt>
            <dd>
              <time dateTime={dashboard.published_at}>
                {formatInstant(dashboard.published_at)}
              </time>
            </dd>
          </div>
          <div>
            <dt>Data as of</dt>
            <dd><time dateTime={dashboard.as_of}>{formatInstant(dashboard.as_of)}</time></dd>
          </div>
          <div>
            <dt>Freshness</dt>
            <dd>{freshnessLabels[dashboard.freshness]}</dd>
          </div>
          <div>
            <dt>Access</dt>
            <dd>{accessLabels[dashboard.access_state]}</dd>
          </div>
        </dl>
      </div>
      <CapabilityState state={dashboard.state} />
    </>
  )
}

export function DashboardsPage({client, dashboardRef}: DashboardsPageProps) {
  const [dashboards, setDashboards] = useState<readonly DashboardView[] | null>(null)
  const [failure, setFailure] = useState<string | null>(null)

  useEffect(() => {
    let abandoned = false
    const request = dashboardRef === undefined
      ? client.getDashboards().then((envelope) => envelope.data.dashboards ?? [])
      : client.getDashboard(dashboardRef).then((envelope) => [envelope.data])
    request
      .then((records) => {
        if (!abandoned) setDashboards(records)
      })
      .catch((error: unknown) => {
        if (!abandoned) {
          setFailure(
            error instanceof ConsoleApiError
              ? error.message
              : "The dashboard listing is unavailable.",
          )
        }
      })
    return () => {
      abandoned = true
    }
  }, [client, dashboardRef])

  if (dashboardRef !== undefined) {
    const dashboard = dashboards?.[0]
    return (
      <section aria-labelledby="dashboard-title" className="summary-page">
        <a href="/dashboards">Back to dashboards</a>
        <p className="eyebrow">Managed dashboard</p>
        {failure === null ? null : <p role="alert">{failure}</p>}
        {failure !== null || dashboard === undefined ? null : (
          <>
            <h1 id="dashboard-title">{dashboard.display_name}</h1>
            <section aria-label="Dashboard publication" className="capability-ledger__item">
              <DashboardRecord dashboard={dashboard} showTitle={false} />
            </section>
          </>
        )}
      </section>
    )
  }

  return (
    <section aria-labelledby="dashboards-title" className="summary-page">
      <p className="eyebrow">Managed analytics</p>
      <h1 id="dashboards-title">Dashboards</h1>
      <p className="summary-page__lead">
        Dashboards published from governed products by the managed BI provider.
      </p>
      <p className="summary-page__guidance">
        Provider administration and unrestricted dashboard authoring remain outside this shell.
      </p>
      {failure === null ? null : <p role="alert">{failure}</p>}
      {failure !== null || dashboards === null ? null : dashboards.length === 0 ? (
        <p className="summary-page__guidance">No dashboards have been published.</p>
      ) : (
        <ul aria-label="Published dashboards" className="capability-ledger">
          {dashboards.map((dashboard) => (
            <li className="capability-ledger__item" key={dashboard.dashboard_ref}>
              <div>
                <h2>
                  <a href={`/dashboards/${dashboard.dashboard_ref}`}>{dashboard.display_name}</a>
                </h2>
                <p>{dashboard.summary}</p>
                <p>
                  Published <time dateTime={dashboard.published_at}>{formatInstant(dashboard.published_at)}</time>
                </p>
                <dl className="dashboard-record__facts">
                  <div>
                    <dt>Data as of</dt>
                    <dd><time dateTime={dashboard.as_of}>{formatInstant(dashboard.as_of)}</time></dd>
                  </div>
                  <div>
                    <dt>Freshness</dt>
                    <dd>{freshnessLabels[dashboard.freshness]}</dd>
                  </div>
                  <div>
                    <dt>Access</dt>
                    <dd>{accessLabels[dashboard.access_state]}</dd>
                  </div>
                </dl>
              </div>
              <CapabilityState state={dashboard.state} />
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
