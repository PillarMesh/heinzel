import {Navigate, Route, Routes, useParams} from "react-router-dom"

import type {
  ConsoleEnvelopeSessionView,
  ConsoleEnvelopeSetupView,
  ConsoleEnvelopeWorkspaceView,
} from "../api/generated"
import {AppShell} from "../components/app-shell"
import {CapabilitySummaryPage} from "../components/capability-summary-page"
import {RunsPage, type RunsClient} from "../features/runs/runs-page"
import DecisionWorkspaceRoute, {
  DecisionWorkspace,
  type InboxClient,
} from "../features/inbox/decision-workspace"
import {MyRequests, type RequesterClient} from "../features/requests/my-requests"
import {SetupWorkbench, type SetupClient} from "../features/setup/setup-workbench"
import {selectLandingRoute} from "./landing-route"
import {RecoveryPage} from "./recovery-page"

interface ConsoleRoutesProps {
  /** Re-read session, workspace and setup after a command changes them. */
  readonly onProjectionsChanged?: () => void
  readonly sessionEnvelope: ConsoleEnvelopeSessionView
  // One memoized client instance satisfies every feature protocol structurally,
  // so the feature read effects do not re-fire on each render.
  readonly setupClient: SetupClient & InboxClient & RequesterClient & RunsClient
  readonly setupEnvelope: ConsoleEnvelopeSetupView | undefined
  readonly workspaceEnvelope: ConsoleEnvelopeWorkspaceView
}

interface ReviewWorkbenchRouteProps {
  readonly sessionEnvelope: ConsoleEnvelopeSessionView
  readonly setupClient: SetupClient
  readonly setupEnvelope: ConsoleEnvelopeSetupView | undefined
}

interface RequesterRouteProps {
  readonly client: RequesterClient
  readonly dataProvenance: ConsoleEnvelopeWorkspaceView["meta"]["data_provenance"]
  readonly sessionEnvelope: ConsoleEnvelopeSessionView
}

function MyRequestRoute({client, dataProvenance, sessionEnvelope}: RequesterRouteProps) {
  const {requestId} = useParams()
  if (requestId === undefined) {
    return <RecoveryPage kind="projection" />
  }
  return (
    <MyRequests
      client={client}
      dataProvenance={dataProvenance}
      requestedRequestRef={requestId}
      session={sessionEnvelope.data}
    />
  )
}

function ReviewWorkbenchRoute({
  sessionEnvelope,
  setupClient,
  setupEnvelope,
}: ReviewWorkbenchRouteProps) {
  const {reviewId} = useParams()
  if (setupEnvelope === undefined || reviewId === undefined) {
    return <RecoveryPage kind="projection" />
  }
  return (
    <SetupWorkbench
      client={setupClient}
      requestedReviewRef={reviewId}
      session={sessionEnvelope.data}
      setupEnvelope={setupEnvelope}
    />
  )
}

export function ConsoleRoutes({
  onProjectionsChanged,
  sessionEnvelope,
  setupClient,
  setupEnvelope,
  workspaceEnvelope,
}: ConsoleRoutesProps) {
  const provenanceMatches =
    sessionEnvelope.meta.data_provenance === workspaceEnvelope.meta.data_provenance &&
    (setupEnvelope === undefined ||
      setupEnvelope.meta.data_provenance === workspaceEnvelope.meta.data_provenance)
  const workspaceReference = workspaceEnvelope.data.workspace.ref
  const identityMatches =
    sessionEnvelope.data.workspace.ref === workspaceReference &&
    (setupEnvelope === undefined || setupEnvelope.data.workspace_ref === workspaceReference)
  if (!provenanceMatches || !identityMatches) {
    return (
      <main aria-label="PillarMesh console" className="standalone-state">
        <RecoveryPage kind="projection" />
      </main>
    )
  }
  const landingRoute = selectLandingRoute(workspaceEnvelope.data, setupEnvelope?.data)
  const capabilities = workspaceEnvelope.data.capabilities ?? []

  return (
    <AppShell
      dataProvenance={workspaceEnvelope.meta.data_provenance}
      session={sessionEnvelope.data}
      workspace={workspaceEnvelope.data}
    >
      <Routes>
        <Route element={<Navigate replace to={landingRoute} />} path="/" />
        <Route
          element={
            setupEnvelope === undefined ? (
              // The app stops fetching the setup projection once the workspace
              // leaves `setup`, so an absent envelope here means the stage is over,
              // not that two projections disagree. Reaching `/setup` afterwards is
              // ordinary -- a bookmark, a back button, a reload -- and the recovery
              // boundary would state a mismatch that did not happen.
              //
              // Unless the landing route is this route. `selectLandingRoute` maps the
              // `setup` state back to `/setup`, and redirecting a route to itself does
              // not terminate, so that combination fails closed instead.
              landingRoute === "/setup" ? (
                <RecoveryPage kind="projection" />
              ) : (
                <Navigate replace to={landingRoute} />
              )
            ) : (
              <SetupWorkbench
                client={setupClient}
                onProjectionsChanged={onProjectionsChanged}
                session={sessionEnvelope.data}
                setupEnvelope={setupEnvelope}
              />
            )
          }
          path="/setup"
        />
        <Route
          element={
            <ReviewWorkbenchRoute
              sessionEnvelope={sessionEnvelope}
              setupClient={setupClient}
              setupEnvelope={setupEnvelope}
            />
          }
          path="/reviews/:reviewId"
        />
        <Route
          element={
            <DecisionWorkspace
              client={setupClient}
              dataProvenance={workspaceEnvelope.meta.data_provenance}
              session={sessionEnvelope.data}
            />
          }
          path="/inbox"
        />
        <Route
          element={
            <DecisionWorkspaceRoute
              client={setupClient}
              dataProvenance={workspaceEnvelope.meta.data_provenance}
              session={sessionEnvelope.data}
            />
          }
          path="/inbox/:requestId"
        />
        <Route
          element={
            <MyRequests
              client={setupClient}
              dataProvenance={workspaceEnvelope.meta.data_provenance}
              session={sessionEnvelope.data}
            />
          }
          path="/requests"
        />
        <Route
          element={
            <MyRequestRoute
              client={setupClient}
              dataProvenance={workspaceEnvelope.meta.data_provenance}
              sessionEnvelope={sessionEnvelope}
            />
          }
          path="/requests/:requestId"
        />
        <Route
          element={<CapabilitySummaryPage capabilities={capabilities} kind="data-products" />}
          path="/data-products"
        />
        <Route element={<RunsPage client={setupClient} />} path="/runs" />
        <Route
          element={<CapabilitySummaryPage capabilities={capabilities} kind="catalog" />}
          path="/catalog"
        />
        <Route
          element={<CapabilitySummaryPage capabilities={capabilities} kind="dashboards" />}
          path="/dashboards"
        />
        <Route
          element={<CapabilitySummaryPage capabilities={capabilities} kind="evidence" />}
          path="/evidence"
        />
        <Route
          element={
            <RecoveryPage
              detail={workspaceEnvelope.data.recovery_message ?? null}
              kind="workspace"
            />
          }
          path="/recovery"
        />
        <Route element={<RecoveryPage kind="workspace" />} path="*" />
      </Routes>
    </AppShell>
  )
}
