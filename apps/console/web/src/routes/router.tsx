import {ResultPage, type ResultClient} from "../features/results/result-page"
import {Navigate, Route, Routes, useParams} from "react-router-dom"

import type {
  ConsoleEnvelopeSessionView,
  ConsoleEnvelopeSetupView,
  ConsoleEnvelopeWorkspaceView,
} from "../api/generated"
import {AppShell} from "../components/app-shell"
import {CapabilitySummaryPage} from "../components/capability-summary-page"
import {RunsPage, type RunsClient} from "../features/runs/runs-page"
import {OperationsPage, type OperationsClient} from "../features/operations/operations-page"
import {CatalogPage, type CatalogClient} from "../features/catalog/catalog-page"
import {
  DashboardsPage,
  type DashboardsClient,
} from "../features/dashboards/dashboards-page"
import {
  DataProductsPage,
  type DataProductsClient,
} from "../features/data-products/data-products-page"
import {
  AcquisitionReceiptsPage,
  type AcquisitionReceiptsClient,
} from "../features/acquisition/acquisition-receipts-page"
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
  readonly setupClient: SetupClient &
    InboxClient &
    RequesterClient &
    RunsClient &
    OperationsClient &
    AcquisitionReceiptsClient &
    CatalogClient &
    DashboardsClient &
    DataProductsClient & ResultClient
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
  readonly dataAccessAvailable: boolean
  readonly dataProvenance: ConsoleEnvelopeWorkspaceView["meta"]["data_provenance"]
  readonly sessionEnvelope: ConsoleEnvelopeSessionView
}

function MyRequestRoute({
  client,
  dataAccessAvailable,
  dataProvenance,
  sessionEnvelope,
}: RequesterRouteProps) {
  const {requestId} = useParams()
  if (requestId === undefined) {
    return <RecoveryPage kind="projection" />
  }
  return (
    <MyRequests
      client={client}
      dataAccessAvailable={dataAccessAvailable}
      dataProvenance={dataProvenance}
      requestedRequestRef={requestId}
      session={sessionEnvelope.data}
    />
  )
}

function RequestResultRoute({client}: {readonly client: ResultClient}) {
  const {requestId} = useParams()
  return requestId === undefined ? <RecoveryPage kind="projection" /> : <ResultPage key={requestId} client={client} requestId={requestId} />
}

function CatalogAssetRoute({client}: {readonly client: CatalogClient}) {
  const {assetRef} = useParams()
  return assetRef === undefined ? (
    <RecoveryPage kind="projection" />
  ) : (
    <CatalogPage assetRef={assetRef} client={client} />
  )
}

function DashboardRoute({client}: {readonly client: DashboardsClient}) {
  const {dashboardRef} = useParams()
  return dashboardRef === undefined ? (
    <RecoveryPage kind="projection" />
  ) : (
    <DashboardsPage client={client} dashboardRef={dashboardRef} />
  )
}

function DataProductRoute({client}: {readonly client: DataProductsClient}) {
  const {dataProductId} = useParams()
  return dataProductId === undefined ? (
    <RecoveryPage kind="projection" />
  ) : (
    <DataProductsPage client={client} dataProductId={dataProductId} />
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
      <main aria-label="Heinzel console" className="standalone-state">
        <RecoveryPage kind="projection" />
      </main>
    )
  }
  const landingRoute = sessionEnvelope.data.active_role === "requester" && workspaceEnvelope.data.state !== "unavailable"
    ? "/requests"
    : selectLandingRoute(workspaceEnvelope.data, setupEnvelope?.data)
  const capabilities = workspaceEnvelope.data.capabilities ?? []
  // Intake is offered only when the workspace says the server will accept it.
  const dataAccessAvailable = capabilities.some(
    (capability) =>
      capability.capability_id === "data-access-intake" && capability.state === "ready",
  )

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
              // Unless the landing route is this route, because redirecting a route to
              // itself does not terminate. `selectLandingRoute` sends a workspace with
              // no setup projection elsewhere, so this fails closed on a combination it
              // should never be handed rather than looping if one ever arrives.
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
              dataAccessAvailable={dataAccessAvailable}
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
              dataAccessAvailable={dataAccessAvailable}
              dataProvenance={workspaceEnvelope.meta.data_provenance}
              sessionEnvelope={sessionEnvelope}
            />
          }
          path="/requests/:requestId"
        />
        <Route element={<RequestResultRoute client={setupClient} />} path="/requests/:requestId/result" />
        <Route element={<DataProductsPage client={setupClient} />} path="/data-products" />
        <Route
          element={<DataProductRoute client={setupClient} />}
          path="/data-products/:dataProductId"
        />
        <Route element={<RunsPage client={setupClient} />} path="/runs" />
        <Route
          element={<OperationsPage client={setupClient} session={sessionEnvelope.data} />}
          path="/incidents"
        />
        {/*
          The route used to be `/operations` while the link to it, and the page it opened,
          both said Incidents -- three names for one place, one of them in the address bar
          and in every bookmark. The old address still resolves.
        */}
        <Route element={<Navigate replace to="/incidents" />} path="/operations" />
        <Route
          element={
            <AcquisitionReceiptsPage client={setupClient} session={sessionEnvelope.data} />
          }
          path="/acquisition-receipts"
        />
        <Route element={<CatalogPage client={setupClient} />} path="/catalog" />
        <Route
          element={<CatalogAssetRoute client={setupClient} />}
          path="/catalog/:assetRef"
        />
        <Route element={<DashboardsPage client={setupClient} />} path="/dashboards" />
        <Route element={<DashboardRoute client={setupClient} />} path="/dashboards/:dashboardRef" />
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
        <Route element={<RecoveryPage kind="not_found" />} path="*" />
      </Routes>
    </AppShell>
  )
}
