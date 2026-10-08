import {useEffect, useState} from "react"

import {ConsoleApiError} from "../../api/client"
import type {MutationRequestContext, OperationSubmissionResult} from "../../api/client"
import type {
  ConsoleEnvelopePublishableDashboardsView,
  DashboardPublicationCommand,
  OperationView,
  PublishableDashboardView,
} from "../../api/generated"

export interface DashboardPublicationClient {
  getPublishableDashboards(requestId: string): Promise<ConsoleEnvelopePublishableDashboardsView>
  publishDashboard(
    command: DashboardPublicationCommand,
    context: MutationRequestContext,
  ): Promise<OperationSubmissionResult>
}

interface DashboardPublicationProps {
  readonly client: DashboardPublicationClient
  readonly requestId: string
  readonly mutationContext: () => MutationRequestContext
}

// The offering is what the server composed for this request. A dashboard is never named by the
// browser from anything else, so there is no free-text field here: an architect chooses one of
// these or publishes nothing.
export function DashboardPublication({
  client,
  requestId,
  mutationContext,
}: DashboardPublicationProps) {
  const [offering, setOffering] = useState<{
    readonly answerTitle: string | null
    readonly dashboards: readonly PublishableDashboardView[]
    readonly publicationAvailable: boolean
  } | null>(null)
  const [outcome, setOutcome] = useState<OperationView | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [publishing, setPublishing] = useState<string | null>(null)

  useEffect(() => {
    let abandoned = false
    void client
      .getPublishableDashboards(requestId)
      .then((envelope) => {
        if (abandoned) return
        setOffering({
          answerTitle: envelope.data.answer_title ?? null,
          dashboards: envelope.data.dashboards ?? [],
          publicationAvailable: envelope.data.publication_available ?? false,
        })
      })
      .catch((error: unknown) => {
        if (abandoned) return
        setFailure(
          error instanceof ConsoleApiError
            ? error.message
            : "The publishable dashboards are unavailable.",
        )
      })
    return () => {
      abandoned = true
    }
  }, [client, requestId])

  function publish(dashboard: PublishableDashboardView) {
    const key = `${dashboard.dashboard_id}@${String(dashboard.dashboard_version)}`
    setPublishing(key)
    setFailure(null)
    void client
      .publishDashboard(
        {
          active_role: "data_architect",
          dashboard_id: dashboard.dashboard_id,
          dashboard_version: dashboard.dashboard_version,
          expected_revision: dashboard.next_revision,
          request_id: requestId,
        },
        mutationContext(),
      )
      .then((result) => {
        setOutcome(result.envelope.data)
      })
      .catch((error: unknown) => {
        setFailure(
          error instanceof ConsoleApiError ? error.message : "The publication outcome is unknown.",
        )
      })
      .finally(() => {
        setPublishing(null)
      })
  }

  if (failure !== null && offering === null) {
    return <p role="alert">{failure}</p>
  }
  if (offering === null) {
    return null
  }
  if (offering.dashboards.length === 0) {
    return (
      <p className="summary-page__guidance">
        No certified dashboard matches this answer, so there is nothing to publish from it.
      </p>
    )
  }

  return (
    <section aria-label="Publishable dashboards">
      {offering.answerTitle === null || offering.dashboards.length === 1 ? null : (
        <p className="summary-page__guidance">
          Publishing titles each of these &ldquo;{offering.answerTitle}&rdquo;.
        </p>
      )}
      {offering.publicationAvailable ? null : (
        <p className="summary-page__guidance">
          These dashboards match this answer. Publishing is not delivered in this deployment, so
          there is nothing to publish to yet.
        </p>
      )}
      {failure === null ? null : <p role="alert">{failure}</p>}
      {outcome === null ? null : (
        <p role="status">
          {outcome.state === "succeeded"
            ? "Dashboard published."
            : (outcome.failure?.safe_message ?? "The dashboard was not published.")}
        </p>
      )}
      <ul className="capability-ledger">
        {offering.dashboards.map((dashboard) => {
          const key = `${dashboard.dashboard_id}@${String(dashboard.dashboard_version)}`
          return (
            <li className="capability-ledger__item" key={key}>
              <div>
                {/*
                  Headed by what the published dashboard will be called. It used to be headed
                  by the governed identifier -- `dashboard-demo-revenue` -- while the sentence
                  directly above it already said the title, so the card named the one thing
                  the architect was not deciding about. The identifier stays, as a fact.
                */}
                <h4>
                  {offering.answerTitle === null || offering.dashboards.length > 1
                    ? dashboard.dashboard_id
                    : offering.answerTitle}
                </h4>
                <dl className="dashboard-record__facts">
                  <div>
                    <dt>Dashboard</dt>
                    <dd>
                      <code>{dashboard.dashboard_id}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>Contract version</dt>
                    <dd>{dashboard.dashboard_version}</dd>
                  </div>
                  <div>
                    <dt>Owner</dt>
                    <dd>{dashboard.owner}</dd>
                  </div>
                  <div>
                    <dt>Publishes revision</dt>
                    <dd>{dashboard.next_revision}</dd>
                  </div>
                </dl>
              </div>
              {offering.publicationAvailable ? (
                <button
                  disabled={publishing !== null}
                  onClick={() => {
                    publish(dashboard)
                  }}
                  type="button"
                >
                  {publishing === key ? "Publishing…" : "Publish"}
                </button>
              ) : null}
            </li>
          )
        })}
      </ul>
    </section>
  )
}
