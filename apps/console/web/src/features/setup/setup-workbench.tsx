import type {
  ConsoleEnvelopeOperationView,
  ConsoleEnvelopeReviewView,
  ConsoleEnvelopeSetupView,
  DecisionCommand,
  ProcessPackageCommand,
  SessionView,
  WarehouseBindingCommand,
} from "../../api/generated"
import type {MutationRequestContext, OperationSubmissionResult} from "../../api/client"
import {ActivationStage} from "./activation-stage"
import type {DigestFile} from "./file-digest"
import {FoundationStage} from "./foundation-stage"
import type {PollTimer} from "./operation-status"
import {ProcessStage} from "./process-stage"
import {ReviewProjection} from "./review-stage"
import {ServicesStage} from "./services-stage"
import {SourcesStage} from "./sources-stage"
import "./setup.css"

export interface SetupClient {
  confirmWarehouseBinding(
    command: WarehouseBindingCommand,
    context: MutationRequestContext,
  ): Promise<OperationSubmissionResult>
  decideReview(
    reviewId: string,
    command: DecisionCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeReviewView>
  getOperation(operationId: string): Promise<ConsoleEnvelopeOperationView>
  getReview(reviewId: string): Promise<ConsoleEnvelopeReviewView>
  getSetup(): Promise<ConsoleEnvelopeSetupView>
  submitProcessPackage(
    command: ProcessPackageCommand,
    context: MutationRequestContext,
  ): Promise<OperationSubmissionResult>
}

export type IdempotencyKeyFactory = () => string

interface SetupWorkbenchProps {
  readonly client: SetupClient
  readonly digestFile?: DigestFile
  readonly idempotencyKeyFactory?: IdempotencyKeyFactory
  readonly pollTimer?: PollTimer
  readonly requestedReviewRef?: string
  readonly session: SessionView
  readonly setupEnvelope: ConsoleEnvelopeSetupView
}

function stageStateLabel(state: string): string {
  return state.replaceAll("_", " ")
}

function defaultIdempotencyKey(): string {
  return `setup-${globalThis.crypto.randomUUID()}`
}

export function SetupWorkbench({
  client,
  digestFile,
  idempotencyKeyFactory = defaultIdempotencyKey,
  pollTimer,
  requestedReviewRef,
  session,
  setupEnvelope,
}: SetupWorkbenchProps) {
  const setup = setupEnvelope.data
  const reviewRefs =
    requestedReviewRef === undefined ? (setup.pending_review_refs ?? []) : [requestedReviewRef]

  return (
    <div className="setup-workbench">
      <nav aria-label="Setup progress" className="setup-progress">
        <p className="setup-progress__title">Workspace foundation</p>
        <ol aria-label="Setup stages">
          {setup.stages.map((stage, index) => (
            <li
              aria-current={stage.stage === setup.active_stage ? "step" : undefined}
              className={`setup-progress__item setup-progress__item--${stage.state}`}
              key={stage.stage}
            >
              <span className="setup-progress__number">{index + 1}</span>
              <span>
                <strong>{stage.label}</strong>
                <small>{stageStateLabel(stage.state)}</small>
                {stage.detail === null || stage.detail === undefined ? null : <em>{stage.detail}</em>}
              </span>
            </li>
          ))}
        </ol>
      </nav>
      <div className="setup-workbench__surface">
        {setup.active_stage === "foundation" ? (
          <FoundationStage
            client={client}
            idempotencyKeyFactory={idempotencyKeyFactory}
            pollTimer={pollTimer}
            session={session}
            setup={setup}
          />
        ) : setup.active_stage === "managed_services" ? (
          <ServicesStage setup={setup} />
        ) : setup.active_stage === "sources" ? (
          <SourcesStage setup={setup} />
        ) : setup.active_stage === "business_process" ? (
          <ProcessStage
            client={client}
            digestFile={digestFile}
            idempotencyKeyFactory={idempotencyKeyFactory}
            pollTimer={pollTimer}
            session={session}
            setup={setup}
          />
        ) : setup.active_stage === "meaning" ? (
          <ReviewProjection
            client={client}
            dataProvenance={setupEnvelope.meta.data_provenance}
            idempotencyKeyFactory={idempotencyKeyFactory}
            kind="meaning"
            reviewRefs={reviewRefs}
            session={session}
          />
        ) : setup.active_stage === "data_product" ? (
          <ReviewProjection
            client={client}
            dataProvenance={setupEnvelope.meta.data_provenance}
            idempotencyKeyFactory={idempotencyKeyFactory}
            kind="data_product"
            reviewRefs={reviewRefs}
            session={session}
          />
        ) : setup.active_stage === "activation" ? (
          <ActivationStage
            client={client}
            dataProvenance={setupEnvelope.meta.data_provenance}
            idempotencyKeyFactory={idempotencyKeyFactory}
            reviewRefs={reviewRefs}
            session={session}
          />
        ) : null}
      </div>
    </div>
  )
}
