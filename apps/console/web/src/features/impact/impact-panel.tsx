import {useEffect, useState} from "react"

import {ConsoleApiError} from "../../api/client"
import type {
  ConsoleEnvelopeImpactView,
  DataProvenance,
  ImpactItemView,
  ImpactView,
} from "../../api/generated"
import "./impact.css"

export interface ImpactClient {
  getRequestImpact(requestId: string): Promise<ConsoleEnvelopeImpactView>
}

interface ImpactPanelProps {
  readonly client: ImpactClient
  readonly dataProvenance: DataProvenance
  readonly requestId: string
}

const changeLabels = {
  source_drift: "Source change",
  metric_version_change: "Metric version change",
  contract_supersession: "Contract replacement",
  generation_failure: "Data generation failure",
  policy_change: "Policy change",
  grant_change: "Access change",
  retirement: "Retirement",
} as const

function ImpactList({items}: {readonly items: readonly ImpactItemView[]}) {
  if (items.length === 0) {
    return <p className="impact-panel__empty">No visible assets in this category.</p>
  }
  return (
    <ul className="impact-panel__list">
      {items.map((item) => (
        <li key={item.impact_handle}>
          <strong>{item.label}</strong>
          <span>{item.asset_type}</span>
          <span>Owned by {item.owner_label}</span>
        </li>
      ))}
    </ul>
  )
}

export function ImpactPanel({client, dataProvenance, requestId}: ImpactPanelProps) {
  const [state, setState] = useState<
    | {readonly kind: "loading"}
    | {readonly kind: "none"}
    | {readonly kind: "unavailable"}
    | {readonly kind: "ready"; readonly impact: ImpactView}
  >({kind: "loading"})

  useEffect(() => {
    let active = true
    void client
      .getRequestImpact(requestId)
      .then((response) => {
        if (!active) {
          return
        }
        if (
          response.meta.data_provenance !== dataProvenance ||
          response.data.request_id !== requestId
        ) {
          setState({kind: "unavailable"})
          return
        }
        setState({kind: "ready", impact: response.data})
      })
      .catch((error: unknown) => {
        if (active) {
          setState(
            error instanceof ConsoleApiError && error.status === 404
              ? {kind: "none"}
              : {kind: "unavailable"},
          )
        }
      })
    return () => {
      active = false
    }
  }, [client, dataProvenance, requestId])

  if (state.kind === "loading") {
    return <p role="status">Loading impact analysis…</p>
  }
  if (state.kind === "none") {
    return null
  }
  if (state.kind === "unavailable") {
    return (
      <section aria-label="Impact analysis" className="impact-panel">
        <h3>Impact analysis</h3>
        <p role="status">Impact analysis is unavailable for this request.</p>
      </section>
    )
  }

  const validated = state.impact.validated_impacts ?? []
  const possible = state.impact.possible_impacts ?? []
  const owners = state.impact.affected_owners ?? []
  const approvers = state.impact.added_approvers ?? []
  return (
    <section aria-label="Impact analysis" className="impact-panel">
      <header className="impact-panel__header">
        <div>
          <p className="eyebrow">{changeLabels[state.impact.change_type]}</p>
          <h3>Impact analysis</h3>
        </div>
        <strong>{state.impact.subject_label}</strong>
      </header>

      <div className="impact-panel__group impact-panel__group--validated">
        <h4>Validated impacts</h4>
        <p>Confirmed dependencies that contribute required approvals.</p>
        <ImpactList items={validated} />
      </div>

      <div className="impact-panel__group impact-panel__group--possible">
        <h4>Possible impacts</h4>
        <p>These are advisory until their dependency is validated.</p>
        <ImpactList items={possible} />
      </div>

      <div className="impact-panel__summary">
        <div>
          <h4>Affected owners</h4>
          <ul>{owners.map((owner) => <li key={owner}>{owner}</li>)}</ul>
        </div>
        <div>
          <h4>Added approvers</h4>
          <ul>
            {approvers.map((approver, index) => (
              <li key={`${approver.authority_label}:${index}`}>
                <strong>{approver.authority_label}</strong>
                <span>{approver.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  )
}
