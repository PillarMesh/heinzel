import {useState} from "react"

import {ConsoleMutationOutcomeUnknown} from "../../api/client"
import type {MutationRequestContext} from "../../api/client"
import type {
  OperationView,
  SessionView,
  SetupView,
  SourceRegistrationCommand,
} from "../../api/generated"
import {CapabilityState} from "../../components/capability-state"
import {OperationStatus, type PollTimer} from "./operation-status"
import type {IdempotencyKeyFactory, SetupClient} from "./setup-workbench"

interface RegistrationAttempt {
  readonly command: SourceRegistrationCommand
  readonly context: MutationRequestContext
}

interface SourcesStageProps {
  readonly client: SetupClient
  readonly idempotencyKeyFactory: IdempotencyKeyFactory
  /** Re-read the stage after a registration changes what is registered. */
  readonly onProjectionsChanged?: (() => void) | undefined
  readonly pollTimer?: PollTimer | undefined
  readonly session: SessionView
  readonly setup: SetupView
}

function lifecycleLabel(value: string | null | undefined): string {
  return value === null || value === undefined ? "Not reported" : value.replaceAll("_", " ")
}

export function SourcesStage({
  client,
  idempotencyKeyFactory,
  onProjectionsChanged,
  pollTimer,
  session,
  setup,
}: SourcesStageProps) {
  const activeRole = session.active_role === "data_architect" ? session.active_role : null
  const [operation, setOperation] = useState<OperationView | null>(null)
  const [submittingHandle, setSubmittingHandle] = useState<string | null>(null)
  const [registrationError, setRegistrationError] = useState<string | null>(null)
  const [ambiguousAttempt, setAmbiguousAttempt] = useState<RegistrationAttempt | null>(null)
  const sources = setup.sources ?? []
  const enrollable = setup.enrollable_sources ?? []

  async function submitAttempt(attempt: RegistrationAttempt): Promise<void> {
    setSubmittingHandle(attempt.command.connection_handle)
    setRegistrationError(null)
    try {
      const result = await client.registerSource(attempt.command, attempt.context)
      setAmbiguousAttempt(null)
      setOperation(result.envelope.data)
    } catch (error: unknown) {
      if (error instanceof ConsoleMutationOutcomeUnknown) {
        // The registration may or may not have reached the broker. Offering the same
        // attempt again rather than a fresh one keeps the idempotency key, so the
        // retry cannot become a second registration of the same handle.
        setAmbiguousAttempt(attempt)
        setRegistrationError("The source registration outcome is unknown.")
      } else {
        setRegistrationError("The source could not be registered.")
      }
    } finally {
      setSubmittingHandle(null)
    }
  }

  function registerHandle(connectionHandle: string): void {
    if (activeRole === null) {
      return
    }
    void submitAttempt({
      command: {
        expected_revision: setup.revision,
        active_role: activeRole,
        connection_handle: connectionHandle,
      },
      context: {
        csrfToken: session.csrf_token,
        idempotencyKey: idempotencyKeyFactory(),
      },
    })
  }

  return (
    <section aria-labelledby="sources-title" className="setup-stage">
      <p className="eyebrow">Stage 3 · Sources</p>
      <h1 id="sources-title">Registered sources</h1>
      <p className="setup-stage__lead">
        Registering validates an enrolled connection against the source itself: the connecting
        role must be refused everything outside its declaration before anything is read.
      </p>
      <p className="setup-warning">
        This console never holds a connection string. An operator enrols each connection in this
        deployment&apos;s secret custody before it appears here, and registering names only the
        handle it was enrolled under.
      </p>
      <section aria-labelledby="registered-sources-title">
        <h2 id="registered-sources-title">Registered</h2>
        {sources.length === 0 ? (
          <p>No source is registered in this workspace yet.</p>
        ) : (
          <div className="source-grid">
            {sources.map((source) => (
              <article
                aria-label={source.display_name}
                className="source-card"
                key={source.source_ref}
              >
                <header>
                  <div>
                    <p className="source-card__type">{source.source_type}</p>
                    <h3>{source.display_name}</h3>
                  </div>
                  <CapabilityState state={source.state} />
                </header>
                <dl>
                  <div>
                    <dt>Lifecycle state</dt>
                    <dd>{lifecycleLabel(source.lifecycle_state)}</dd>
                  </div>
                  <div>
                    <dt>Account mode</dt>
                    <dd>{lifecycleLabel(source.account_mode)}</dd>
                  </div>
                  <div>
                    <dt>Approved objects</dt>
                    <dd>{(source.approved_object_refs ?? []).join(", ")}</dd>
                  </div>
                  <div>
                    <dt>Probed capability profile</dt>
                    <dd>
                      {source.capability_authority_digest === null ||
                      source.capability_authority_digest === undefined
                        ? "Not validated"
                        : source.capability_authority_digest}
                    </dd>
                  </div>
                </dl>
                {(source.intended_checks ?? []).length === 0 ? null : (
                  <section>
                    <h4>Intended probes</h4>
                    <ul>
                      {(source.intended_checks ?? []).map((check) => (
                        <li key={check}>{check}</li>
                      ))}
                    </ul>
                  </section>
                )}
                {(source.denied_checks ?? []).length === 0 ? null : (
                  <section>
                    <h4>Denial probes</h4>
                    <ul>
                      {(source.denied_checks ?? []).map((check) => (
                        <li key={check}>{check}</li>
                      ))}
                    </ul>
                  </section>
                )}
              </article>
            ))}
          </div>
        )}
      </section>
      <section aria-labelledby="enrollable-sources-title">
        <h2 id="enrollable-sources-title">Enrolled and not registered</h2>
        {enrollable.length === 0 ? (
          <p>
            No enrolled connection is waiting to be registered. An operator enrols one in this
            deployment&apos;s secret custody; the console cannot.
          </p>
        ) : (
          <ul aria-label="Enrolled connections" className="source-handle-list">
            {enrollable.map((handle) => (
              <li className="source-handle" key={handle.connection_handle}>
                <div>
                  <h3>{handle.connection_handle}</h3>
                  <p className="source-card__type">{handle.source_type}</p>
                  <dl>
                    <div>
                      <dt>Account mode</dt>
                      <dd>{lifecycleLabel(handle.account_mode)}</dd>
                    </div>
                    <div>
                      <dt>Declared objects</dt>
                      <dd>{handle.declared_object_refs.join(", ")}</dd>
                    </div>
                  </dl>
                </div>
                <button
                  className="primary-action"
                  disabled={submittingHandle !== null || activeRole === null}
                  onClick={() => registerHandle(handle.connection_handle)}
                  type="button"
                >
                  {submittingHandle === handle.connection_handle
                    ? "Registering…"
                    : "Register source"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
      {registrationError === null ? null : <p role="alert">{registrationError}</p>}
      {ambiguousAttempt === null ? null : (
        <button
          disabled={submittingHandle !== null}
          onClick={() => void submitAttempt(ambiguousAttempt)}
          type="button"
        >
          Reconcile source registration
        </button>
      )}
      {operation === null ? null : (
        <OperationStatus
          client={client}
          label="Source registration operation"
          onSettled={onProjectionsChanged === undefined ? undefined : () => onProjectionsChanged()}
          operation={operation}
          pollTimer={pollTimer}
        />
      )}
    </section>
  )
}
