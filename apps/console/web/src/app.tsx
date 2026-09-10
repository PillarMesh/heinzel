import {useCallback, useEffect, useMemo, useState} from "react"
import {BrowserRouter} from "react-router-dom"

import {ConsoleApiClient, ConsoleApiError, MalformedConsoleResponse} from "./api/client"
import type {
  ConsoleEnvelopeSessionView,
  ConsoleEnvelopeSetupView,
  ConsoleEnvelopeWorkspaceView,
} from "./api/generated"
import {LoadingPage} from "./routes/loading-page"
import {RecoveryPage} from "./routes/recovery-page"
import type {InboxClient} from "./features/inbox/decision-workspace"
import type {RequesterClient} from "./features/requests/my-requests"
import type {RunsClient} from "./features/runs/runs-page"
import type {AcquisitionReceiptsClient} from "./features/acquisition/acquisition-receipts-page"
import type {CatalogClient} from "./features/catalog/catalog-page"
import type {DataProductsClient} from "./features/data-products/data-products-page"
import {ConsoleRoutes} from "./routes/router"
import type {SetupClient} from "./features/setup/setup-workbench"
import "./styles/global.css"

export interface ConsoleBootstrapClient
  extends SetupClient,
    InboxClient,
    RequesterClient,
    RunsClient,
    AcquisitionReceiptsClient,
    CatalogClient,
    DataProductsClient {
  getSession(): Promise<ConsoleEnvelopeSessionView>
  getSetup(): Promise<ConsoleEnvelopeSetupView>
  getWorkspace(): Promise<ConsoleEnvelopeWorkspaceView>
}

interface AppProps {
  readonly client?: ConsoleBootstrapClient
}

interface ReadyBootstrap {
  readonly sessionEnvelope: ConsoleEnvelopeSessionView
  readonly setupEnvelope: ConsoleEnvelopeSetupView | undefined
  readonly workspaceEnvelope: ConsoleEnvelopeWorkspaceView
}

type BootstrapState =
  | {readonly kind: "loading"}
  | {readonly kind: "ready"; readonly value: ReadyBootstrap}
  | {readonly error: unknown; readonly kind: "failed"}

function retryLabel(error: ConsoleApiError | null): string | undefined {
  if (error?.recoveryAction === "reload") {
    return "Reload workspace"
  }
  if (error?.recoveryAction === "retry") {
    return "Try again"
  }
  return undefined
}

export function App({client}: AppProps) {
  const selectedClient = useMemo(() => client ?? new ConsoleApiClient(), [client])
  const [attempt, setAttempt] = useState(0)
  const [state, setState] = useState<BootstrapState>({kind: "loading"})
  // A command settles against the owning services, and the surfaces it changed - the
  // workspace state, the governance spine, the stage list - are all read here. Re-run
  // the bootstrap rather than patching them, so the console never shows a projection
  // it did not receive from the server. The rendered page stays up meanwhile.
  const reReadProjections = useCallback(() => setAttempt((current) => current + 1), [])

  useEffect(() => {
    let active = true
    void Promise.all([selectedClient.getSession(), selectedClient.getWorkspace()])
      .then(async ([sessionEnvelope, workspaceEnvelope]) => {
        const needsSetup =
          sessionEnvelope.data.active_role === "data_architect" &&
          (workspaceEnvelope.data.state === "setup" ||
          workspaceEnvelope.data.state === "pending_activation")
        const setupEnvelope = needsSetup ? await selectedClient.getSetup() : undefined
        if (active) {
          setState({
            kind: "ready",
            value: {sessionEnvelope, setupEnvelope, workspaceEnvelope},
          })
        }
      })
      .catch((error: unknown) => {
        if (active) {
          setState({error, kind: "failed"})
        }
      })
    return () => {
      active = false
    }
  }, [attempt, selectedClient])

  if (state.kind === "loading") {
    return <LoadingPage />
  }
  if (state.kind === "failed") {
    const apiError = state.error instanceof ConsoleApiError ? state.error : null
    const actionLabel = retryLabel(apiError)
    const retry = () => {
      setState({kind: "loading"})
      setAttempt((current) => current + 1)
    }
    return (
      <main aria-label="PillarMesh console" className="standalone-state">
        <RecoveryPage
          actionLabel={actionLabel}
          correlationId={apiError?.correlationId}
          detail={apiError?.recoveryAction === "reauthenticate" ? undefined : apiError?.message}
          kind={
            apiError?.recoveryAction ??
            (state.error instanceof MalformedConsoleResponse ? "malformed" : "service")
          }
          onRetry={apiError === null || actionLabel !== undefined ? retry : undefined}
        />
      </main>
    )
  }

  return (
    <BrowserRouter>
      <ConsoleRoutes
        {...state.value}
        onProjectionsChanged={reReadProjections}
        setupClient={selectedClient}
      />
    </BrowserRouter>
  )
}
