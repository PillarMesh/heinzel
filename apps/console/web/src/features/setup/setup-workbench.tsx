import {useState} from "react"

import type {
  ConsoleEnvelopeOperationView,
  ConsoleEnvelopeReviewView,
  ConsoleEnvelopeSetupView,
  DecisionCommand,
  ProcessPackageCommand,
  SessionView,
  SetupStage,
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
  /** Re-read the shell's own projections after a command changes them. */
  readonly onProjectionsChanged?: (() => void) | undefined
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
  onProjectionsChanged,
  pollTimer,
  requestedReviewRef,
  session,
  setupEnvelope,
}: SetupWorkbenchProps) {
  const setup = setupEnvelope.data
  const [stageSelection, setStageSelection] = useState<{
    readonly setupDigest: string
    readonly stage: SetupStage
  } | null>(null)
  const visibleStage =
    stageSelection?.setupDigest === setup.setup_digest
      ? stageSelection.stage
      : setup.active_stage
  const reviewRefs =
    requestedReviewRef === undefined ? (setup.pending_review_refs ?? []) : [requestedReviewRef]

  return (
    <div className="setup-workbench">
      <nav aria-label="Setup progress" className="setup-progress">
        <p className="setup-progress__title">Workspace foundation</p>
        <ol aria-label="Setup stages">
          {setup.stages.map((stage, index) => (
            <li
              aria-current={stage.stage === visibleStage ? "step" : undefined}
              className={`setup-progress__item setup-progress__item--${stage.state}`}
              key={stage.stage}
            >
              <button
                disabled={stage.stage !== setup.active_stage && stage.state === "not_started"}
                onClick={() =>
                  setStageSelection({setupDigest: setup.setup_digest, stage: stage.stage})
                }
                type="button"
              >
                <span className="setup-progress__number">{index + 1}</span>
                <span>
                  <strong>{stage.label}</strong>
                  <small>{stageStateLabel(stage.state)}</small>
                  {stage.detail === null || stage.detail === undefined ? null : <em>{stage.detail}</em>}
                </span>
              </button>
            </li>
          ))}
        </ol>
      </nav>
      <div className="setup-workbench__surface">
        {visibleStage === "foundation" ? (
          <FoundationStage
            client={client}
            idempotencyKeyFactory={idempotencyKeyFactory}
            onProjectionsChanged={onProjectionsChanged}
            pollTimer={pollTimer}
            session={session}
            setup={setup}
          />
        ) : visibleStage === "managed_services" ? (
          <ServicesStage setup={setup} />
        ) : visibleStage === "sources" ? (
          <SourcesStage setup={setup} />
        ) : visibleStage === "business_process" ? (
          <ProcessStage
            client={client}
            digestFile={digestFile}
            idempotencyKeyFactory={idempotencyKeyFactory}
            pollTimer={pollTimer}
            session={session}
            setup={setup}
          />
        ) : visibleStage === "meaning" ? (
          <ReviewProjection
            client={client}
            dataProvenance={setupEnvelope.meta.data_provenance}
            idempotencyKeyFactory={idempotencyKeyFactory}
            kind="meaning"
            reviewRefs={reviewRefs}
            session={session}
          />
        ) : visibleStage === "data_product" ? (
          <ReviewProjection
            client={client}
            dataProvenance={setupEnvelope.meta.data_provenance}
            idempotencyKeyFactory={idempotencyKeyFactory}
            kind="data_product"
            reviewRefs={reviewRefs}
            session={session}
          />
        ) : visibleStage === "activation" ? (
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
