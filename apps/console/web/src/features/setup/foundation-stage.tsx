import {useState, type FormEvent} from "react"

import {
  ConsoleMutationOutcomeUnknown,
  type MutationRequestContext,
} from "../../api/client"
import type {
  OperationView,
  SessionView,
  SetupView,
  WarehouseBindingCommand,
  WarehouseEngine,
} from "../../api/generated"
import {OperationStatus, type PollTimer} from "./operation-status"
import type {IdempotencyKeyFactory, SetupClient} from "./setup-workbench"

interface FoundationStageProps {
  readonly client: SetupClient
  readonly idempotencyKeyFactory: IdempotencyKeyFactory
  readonly pollTimer?: PollTimer | undefined
  readonly session: SessionView
  readonly setup: SetupView
}

interface WarehouseAttempt {
  readonly command: WarehouseBindingCommand
  readonly context: MutationRequestContext
}

export function FoundationStage({
  client,
  idempotencyKeyFactory,
  pollTimer,
  session,
  setup,
}: FoundationStageProps) {
  const [selectedEngine, setSelectedEngine] = useState<WarehouseEngine>(
    setup.warehouse_binding?.engine ?? setup.warehouse_options[0].engine,
  )
  const [confirmedImmutableEffect, setConfirmedImmutableEffect] = useState(false)
  const [operation, setOperation] = useState<OperationView | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [submissionError, setSubmissionError] = useState<string | null>(null)
  const [ambiguousAttempt, setAmbiguousAttempt] = useState<WarehouseAttempt | null>(null)
  const selectedOption = setup.warehouse_options.find((option) => option.engine === selectedEngine)
  const bindingOption = setup.warehouse_options.find(
    (option) => option.engine === setup.warehouse_binding?.engine,
  )

  async function submitAttempt(attempt: WarehouseAttempt): Promise<void> {
    setSubmitting(true)
    setSubmissionError(null)
    try {
      const result = await client.confirmWarehouseBinding(
        attempt.command,
        attempt.context,
      )
      setAmbiguousAttempt(null)
      setOperation(result.envelope.data)
    } catch (error: unknown) {
      if (error instanceof ConsoleMutationOutcomeUnknown) {
        setAmbiguousAttempt(attempt)
      } else {
        setAmbiguousAttempt(null)
        setSubmissionError("The warehouse confirmation could not be reconciled safely.")
      }
    } finally {
      setSubmitting(false)
    }
  }

  async function confirmBinding(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    if (!confirmedImmutableEffect || selectedOption === undefined || ambiguousAttempt !== null) {
      return
    }
    await submitAttempt({
      command: {
        expected_revision: setup.revision,
        reviewed_digest: setup.setup_digest,
        active_role: session.active_role,
        engine: selectedOption.engine,
        region: selectedOption.supported_region,
        capacity: selectedOption.fixed_capacity,
      },
      context: {
        csrfToken: session.csrf_token,
        idempotencyKey: idempotencyKeyFactory(),
      },
    })
  }

  return (
    <section aria-labelledby="foundation-title" className="setup-stage">
      <p className="eyebrow">Stage 1 · Foundation</p>
      <h1 id="foundation-title">Choose the managed warehouse</h1>
      <p className="setup-stage__lead">
        Review the server-supported engine, region, and fixed capacity before creating an immutable
        binding.
      </p>
      <form onSubmit={(event) => void confirmBinding(event)}>
        <fieldset className="warehouse-options">
          <legend>Warehouse engine</legend>
          {setup.warehouse_options.map((option) => (
            <label className="warehouse-option" key={option.engine}>
              <input
                checked={selectedEngine === option.engine}
                disabled={setup.warehouse_binding !== null && setup.warehouse_binding !== undefined}
                name="warehouse-engine"
                onChange={() => setSelectedEngine(option.engine)}
                type="radio"
                value={option.engine}
              />
              <span>
                <strong>{option.label}</strong>
                <small>
                  {option.supported_region} · {option.fixed_capacity}
                </small>
              </span>
            </label>
          ))}
        </fieldset>
        {setup.warehouse_binding === null || setup.warehouse_binding === undefined ? (
          <>
            <label className="immutable-confirmation">
              <input
                checked={confirmedImmutableEffect}
                onChange={(event) => setConfirmedImmutableEffect(event.currentTarget.checked)}
                type="checkbox"
              />
              <span>I understand that this warehouse binding is immutable after confirmation.</span>
            </label>
            <button
              className="primary-action"
              disabled={
                !confirmedImmutableEffect ||
                selectedOption === undefined ||
                submitting ||
                ambiguousAttempt !== null
              }
              type="submit"
            >
              {submitting ? "Confirming…" : "Confirm warehouse binding"}
            </button>
          </>
        ) : (
          <p className="immutable-binding">
            The {bindingOption?.label ?? setup.warehouse_binding.engine} warehouse binding is
            immutable. Engine changes are unavailable.
          </p>
        )}
      </form>
      {ambiguousAttempt === null ? null : (
        <div className="setup-warning" role="alert">
          <p>The confirmation outcome is unknown.</p>
          <button
            disabled={submitting}
            onClick={() => void submitAttempt(ambiguousAttempt)}
            type="button"
          >
            Reconcile confirmation
          </button>
        </div>
      )}
      {submissionError === null ? null : <p className="setup-error">{submissionError}</p>}
      {operation === null ? null : (
        <OperationStatus
          client={client}
          label="Warehouse operation"
          operation={operation}
          pollTimer={pollTimer}
        />
      )}
    </section>
  )
}
