import type {DataProvenance, SessionView} from "../../api/generated"
import {ReviewProjection} from "./review-stage"
import type {IdempotencyKeyFactory, SetupClient} from "./setup-workbench"

interface ActivationStageProps {
  readonly client: SetupClient
  readonly dataProvenance: DataProvenance
  readonly idempotencyKeyFactory: IdempotencyKeyFactory
  readonly reviewRefs: readonly string[]
  readonly session: SessionView
}

export function ActivationStage(props: ActivationStageProps) {
  return <ReviewProjection {...props} kind="activation" />
}
