import type {
  ApiMeta,
  ClarifiedOutcomeAcceptanceCommand,
  ConsoleApiSchema,
  ConsoleEnvelopeCatalogAssetView,
  ConsoleEnvelopeClarifiedOutcomeView,
  ConsoleEnvelopeConversationView,
  ConsoleEnvelopeDashboardView,
  ConsoleEnvelopeDataProductView,
  ConsoleEnvelopeEvidenceView,
  ConsoleEnvelopeInboxView,
  ConsoleEnvelopeJsonTuplePillarmeshConsoleContractsRequesterRequestView,
  ConsoleEnvelopeOperationView,
  ConsoleEnvelopeRequesterRequestView,
  ConsoleEnvelopeRequestDetailView,
  ConsoleEnvelopeReviewView,
  ConsoleEnvelopeAcquisitionReceiptsView,
  ConsoleEnvelopeRunsView,
  ConsoleEnvelopeSessionView,
  ConsoleEnvelopeSetupView,
  ConsoleEnvelopeWorkspaceView,
  ConversationMessageCommand,
  CreateRequestCommand,
  AdmissionCommand,
  DecisionCommand,
  ProcessPackageCommand,
  RecoveryAction,
  ResetCommand,
  RetryOperationCommand,
  WarehouseBindingCommand,
} from "./generated"
import {validateConsoleResponse} from "./schema"

type ResponseName = {
  [Name in keyof ConsoleApiSchema]: Name extends `${string}_response` ? Name : never
}[keyof ConsoleApiSchema]

export interface MutationRequestContext {
  readonly csrfToken: string
  readonly idempotencyKey: string
}

interface ConsoleApiClientOptions {
  readonly baseUrl?: string
  readonly transport?: typeof fetch
}

export interface ValidatedResponseMetadata {
  readonly correlationId: string
  readonly dataProvenance: ApiMeta["data_provenance"]
}

export type OperationSubmissionResult =
  | {
      readonly envelope: ConsoleEnvelopeOperationView
      readonly kind: "pending"
      readonly state: "accepted" | "running"
      readonly status: 202
    }
  | {
      readonly envelope: ConsoleEnvelopeOperationView
      readonly kind: "outcome_unknown"
      readonly state: "outcome_unknown"
      readonly status: 202
    }
  | {
      readonly envelope: ConsoleEnvelopeOperationView
      readonly kind: "terminal"
      readonly state: "succeeded" | "failed"
      readonly status: 200
    }

export class MalformedConsoleResponse extends Error {
  readonly metadata: ValidatedResponseMetadata | null
  readonly responseName: ResponseName
  readonly status: number

  constructor(
    responseName: ResponseName,
    status: number,
    metadata: ValidatedResponseMetadata | null = null,
  ) {
    super("The console received a response it could not safely display.")
    this.name = "MalformedConsoleResponse"
    this.responseName = responseName
    this.status = status
    this.metadata = metadata
  }
}

export class ConsoleReadTransportError extends Error {
  readonly responseName: ResponseName

  constructor(responseName: ResponseName) {
    super("The console could not reach the service for this read.")
    this.name = "ConsoleReadTransportError"
    this.responseName = responseName
  }
}

export class ConsoleMutationOutcomeUnknown extends Error {
  readonly idempotencyKey: string
  readonly metadata: ValidatedResponseMetadata | null
  readonly responseName: ResponseName
  readonly status: number | null

  constructor(
    responseName: ResponseName,
    idempotencyKey: string,
    status: number | null,
    metadata: ValidatedResponseMetadata | null,
  ) {
    super("The mutation outcome is unknown. Reconcile it using the same Idempotency-Key before retrying.")
    this.name = "ConsoleMutationOutcomeUnknown"
    this.responseName = responseName
    this.idempotencyKey = idempotencyKey
    this.status = status
    this.metadata = metadata
  }
}

export class ConsoleApiError extends Error {
  readonly status: number
  readonly code: string
  readonly recoveryAction: RecoveryAction
  readonly correlationId: string
  readonly dataProvenance: ApiMeta["data_provenance"]
  readonly field: string | null

  constructor(status: number, envelope: ConsoleApiSchema["error_response"]) {
    super(envelope.error.safe_message)
    this.name = "ConsoleApiError"
    this.status = status
    this.code = envelope.error.code
    this.recoveryAction = envelope.error.recovery_action
    this.correlationId = envelope.meta.correlation_id
    this.dataProvenance = envelope.meta.data_provenance
    this.field = envelope.error.field ?? null
  }
}

const publicIdPattern = /^[a-z][a-z0-9_-]{2,127}$/

function validatedResponseMetadata(response: Response): ValidatedResponseMetadata | null {
  const correlationId = response.headers.get("X-Correlation-ID")
  const dataProvenance = response.headers.get("X-PillarMesh-Data-Provenance")
  if (
    correlationId === null ||
    !publicIdPattern.test(correlationId) ||
    (dataProvenance !== "demo_fixture" && dataProvenance !== "governed_local")
  ) {
    return null
  }
  return {correlationId, dataProvenance}
}

interface RequestPolicy {
  readonly allowedStatuses: readonly number[]
  readonly idempotencyKey?: string
}

function encodePathSegment(value: string): string {
  return encodeURIComponent(value)
}

export class ConsoleApiClient {
  readonly #baseUrl: string
  readonly #transport: typeof fetch

  constructor(options: ConsoleApiClientOptions = {}) {
    this.#baseUrl = options.baseUrl?.replace(/\/$/, "") ?? ""
    this.#transport = options.transport ?? globalThis.fetch.bind(globalThis)
  }

  async #payload(
    path: string,
    init: RequestInit,
    responseName: ResponseName,
    policy: RequestPolicy,
  ): Promise<{
    metadata: ValidatedResponseMetadata | null
    payload: unknown
    status: number
  }> {
    let response: Response
    try {
      response = await this.#transport(`${this.#baseUrl}${path}`, {
        credentials: "same-origin",
        ...init,
      })
    } catch {
      if (policy.idempotencyKey !== undefined) {
        throw new ConsoleMutationOutcomeUnknown(
          responseName,
          policy.idempotencyKey,
          null,
          null,
        )
      }
      throw new ConsoleReadTransportError(responseName)
    }
    const metadata = validatedResponseMetadata(response)
    if (response.ok && !policy.allowedStatuses.includes(response.status)) {
      if (policy.idempotencyKey !== undefined) {
        throw new ConsoleMutationOutcomeUnknown(
          responseName,
          policy.idempotencyKey,
          response.status,
          metadata,
        )
      }
      throw new MalformedConsoleResponse(responseName, response.status, metadata)
    }
    let payload: unknown
    try {
      payload = await response.json()
    } catch {
      if (response.ok && policy.idempotencyKey !== undefined) {
        throw new ConsoleMutationOutcomeUnknown(
          responseName,
          policy.idempotencyKey,
          response.status,
          metadata,
        )
      }
      throw new MalformedConsoleResponse(
        response.ok ? responseName : "error_response",
        response.status,
        metadata,
      )
    }

    if (!response.ok) {
      let envelope: ConsoleApiSchema["error_response"]
      try {
        envelope = validateConsoleResponse("error_response", payload)
      } catch {
        throw new MalformedConsoleResponse("error_response", response.status, metadata)
      }
      throw new ConsoleApiError(response.status, envelope)
    }
    return {metadata, payload, status: response.status}
  }

  async #request<Name extends ResponseName>(
    path: string,
    responseName: Name,
    init: RequestInit = {},
    policy: RequestPolicy = {allowedStatuses: [200]},
  ): Promise<ConsoleApiSchema[Name]> {
    const {metadata, payload, status} = await this.#payload(
      path,
      init,
      responseName,
      policy,
    )
    try {
      return validateConsoleResponse(responseName, payload)
    } catch {
      if (policy.idempotencyKey !== undefined) {
        throw new ConsoleMutationOutcomeUnknown(
          responseName,
          policy.idempotencyKey,
          status,
          metadata,
        )
      }
      throw new MalformedConsoleResponse(responseName, status, metadata)
    }
  }

  #mutationInit(command: object, context: MutationRequestContext): RequestInit {
    return {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": context.idempotencyKey,
        "X-CSRF-Token": context.csrfToken,
      },
      body: JSON.stringify(command),
    }
  }

  async #mutation<Name extends ResponseName>(
    path: string,
    responseName: Name,
    command: object,
    context: MutationRequestContext,
    allowedStatuses: readonly number[] = [200],
  ): Promise<{
    readonly envelope: ConsoleApiSchema[Name]
    readonly metadata: ValidatedResponseMetadata | null
    readonly status: number
  }> {
    const policy = {allowedStatuses, idempotencyKey: context.idempotencyKey}
    const {metadata, payload, status} = await this.#payload(
      path,
      this.#mutationInit(command, context),
      responseName,
      policy,
    )
    try {
      return {envelope: validateConsoleResponse(responseName, payload), metadata, status}
    } catch {
      throw new ConsoleMutationOutcomeUnknown(
        responseName,
        context.idempotencyKey,
        status,
        metadata,
      )
    }
  }

  async #operationMutation(
    path: string,
    command: object,
    context: MutationRequestContext,
  ): Promise<OperationSubmissionResult> {
    const result = await this.#mutation(
      path,
      "operation_response",
      command,
      context,
      [200, 202],
    )
    const state = result.envelope.data.state
    if (result.status === 202 && (state === "accepted" || state === "running")) {
      return {envelope: result.envelope, kind: "pending", state, status: 202}
    }
    if (result.status === 202 && state === "outcome_unknown") {
      return {envelope: result.envelope, kind: "outcome_unknown", state, status: 202}
    }
    if (result.status === 200 && (state === "succeeded" || state === "failed")) {
      return {envelope: result.envelope, kind: "terminal", state, status: 200}
    }
    throw new ConsoleMutationOutcomeUnknown(
      "operation_response",
      context.idempotencyKey,
      result.status,
      result.metadata,
    )
  }

  getSession(): Promise<ConsoleEnvelopeSessionView> {
    return this.#request("/api/v1/session", "session_response")
  }

  getWorkspace(): Promise<ConsoleEnvelopeWorkspaceView> {
    return this.#request("/api/v1/workspace", "workspace_response")
  }

  getSetup(): Promise<ConsoleEnvelopeSetupView> {
    return this.#request("/api/v1/setup", "setup_response")
  }

  getReview(reviewId: string): Promise<ConsoleEnvelopeReviewView> {
    return this.#request(`/api/v1/reviews/${encodePathSegment(reviewId)}`, "review_response")
  }

  getInbox(): Promise<ConsoleEnvelopeInboxView> {
    return this.#request("/api/v1/inbox", "inbox_response")
  }

  getRequestDetail(requestId: string): Promise<ConsoleEnvelopeRequestDetailView> {
    return this.#request(
      `/api/v1/inbox/${encodePathSegment(requestId)}`,
      "request_detail_response",
    )
  }

  getRequesterRequests(): Promise<ConsoleEnvelopeJsonTuplePillarmeshConsoleContractsRequesterRequestView> {
    return this.#request("/api/v1/requests/mine", "requester_requests_response")
  }

  getConversation(requestId: string): Promise<ConsoleEnvelopeConversationView> {
    return this.#request(
      `/api/v1/requests/${encodePathSegment(requestId)}/conversation`,
      "conversation_response",
    )
  }

  getClarifiedOutcome(requestId: string): Promise<ConsoleEnvelopeClarifiedOutcomeView> {
    return this.#request(
      `/api/v1/requests/${encodePathSegment(requestId)}/clarified-outcome`,
      "clarified_outcome_response",
    )
  }

  getDataProduct(dataProductId: string): Promise<ConsoleEnvelopeDataProductView> {
    return this.#request(
      `/api/v1/data-products/${encodePathSegment(dataProductId)}`,
      "data_product_response",
    )
  }

  getRuns(): Promise<ConsoleEnvelopeRunsView> {
    return this.#request("/api/v1/runs", "runs_response")
  }

  getAcquisitionReceipts(): Promise<ConsoleEnvelopeAcquisitionReceiptsView> {
    return this.#request("/api/v1/acquisition-receipts", "acquisition_receipts_response")
  }

  getCatalogAsset(assetRef: string): Promise<ConsoleEnvelopeCatalogAssetView> {
    return this.#request(
      `/api/v1/catalog/${encodePathSegment(assetRef)}`,
      "catalog_asset_response",
    )
  }

  getDashboard(dashboardRef: string): Promise<ConsoleEnvelopeDashboardView> {
    return this.#request(
      `/api/v1/dashboards/${encodePathSegment(dashboardRef)}`,
      "dashboard_response",
    )
  }

  getEvidence(evidenceRef: string): Promise<ConsoleEnvelopeEvidenceView> {
    return this.#request(
      `/api/v1/evidence/${encodePathSegment(evidenceRef)}`,
      "evidence_response",
    )
  }

  getOperation(operationId: string): Promise<ConsoleEnvelopeOperationView> {
    return this.#request(
      `/api/v1/operations/${encodePathSegment(operationId)}`,
      "operation_response",
    )
  }

  confirmWarehouseBinding(
    command: WarehouseBindingCommand,
    context: MutationRequestContext,
  ): Promise<OperationSubmissionResult> {
    return this.#operationMutation("/api/v1/setup/warehouse-binding", command, context)
  }

  submitProcessPackage(
    command: ProcessPackageCommand,
    context: MutationRequestContext,
  ): Promise<OperationSubmissionResult> {
    return this.#operationMutation("/api/v1/setup/process-packages", command, context)
  }

  async decideReview(
    reviewId: string,
    command: DecisionCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeReviewView> {
    return (
      await this.#mutation(
        `/api/v1/reviews/${encodePathSegment(reviewId)}/decisions`,
        "review_response",
        command,
        context,
      )
    ).envelope
  }

  async decideRequest(
    requestId: string,
    command: DecisionCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeRequestDetailView> {
    return (
      await this.#mutation(
        `/api/v1/inbox/${encodePathSegment(requestId)}/decisions`,
        "request_detail_response",
        command,
        context,
      )
    ).envelope
  }

  async admitRequest(
    requestId: string,
    command: AdmissionCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeRequestDetailView> {
    return (
      await this.#mutation(
        `/api/v1/inbox/${encodePathSegment(requestId)}/admission`,
        "request_detail_response",
        command,
        context,
      )
    ).envelope
  }

  async createRequest(
    command: CreateRequestCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeRequesterRequestView> {
    return (
      await this.#mutation(
        "/api/v1/requests",
        "requester_request_response",
        command,
        context,
      )
    ).envelope
  }

  async appendConversationMessage(
    requestId: string,
    command: ConversationMessageCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeConversationView> {
    return (
      await this.#mutation(
        `/api/v1/requests/${encodePathSegment(requestId)}/conversation`,
        "conversation_response",
        command,
        context,
      )
    ).envelope
  }

  async acceptClarifiedOutcome(
    requestId: string,
    command: ClarifiedOutcomeAcceptanceCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeClarifiedOutcomeView> {
    return (
      await this.#mutation(
        `/api/v1/requests/${encodePathSegment(requestId)}/clarified-outcome/acceptance`,
        "clarified_outcome_response",
        command,
        context,
      )
    ).envelope
  }

  retryOperation(
    operationId: string,
    command: RetryOperationCommand,
    context: MutationRequestContext,
  ): Promise<OperationSubmissionResult> {
    return this.#operationMutation(
      `/api/v1/operations/${encodePathSegment(operationId)}/retry`,
      command,
      context,
    )
  }

  async resetDemo(
    command: ResetCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeSetupView> {
    return (
      await this.#mutation("/api/v1/demo/reset", "setup_response", command, context)
    ).envelope
  }
}
