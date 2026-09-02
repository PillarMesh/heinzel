import {useState, type ChangeEvent} from "react"

import {ConsoleMutationOutcomeUnknown} from "../../api/client"
import type {MutationRequestContext} from "../../api/client"
import type {
  MediaType,
  OperationView,
  ProcessPackageCommand,
  SessionView,
  SetupView,
} from "../../api/generated"
import {sha256File, type DigestFile} from "./file-digest"
import {OperationStatus, type PollTimer} from "./operation-status"
import type {IdempotencyKeyFactory, SetupClient} from "./setup-workbench"

const pdfMediaType = "application/pdf"
const docxMediaType = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

interface SelectedPackage {
  readonly digest: string
  readonly fileName: string
  readonly mediaType: MediaType
}

interface ProcessAttempt {
  readonly command: ProcessPackageCommand
  readonly context: MutationRequestContext
}

interface ProcessStageProps {
  readonly client: SetupClient
  readonly digestFile?: DigestFile | undefined
  readonly idempotencyKeyFactory: IdempotencyKeyFactory
  readonly pollTimer?: PollTimer | undefined
  readonly session: SessionView
  readonly setup: SetupView
}

export function ProcessStage({
  client,
  digestFile = sha256File,
  idempotencyKeyFactory,
  pollTimer,
  session,
  setup,
}: ProcessStageProps) {
  const [fileError, setFileError] = useState<string | null>(null)
  const [hashing, setHashing] = useState(false)
  const [selectedPackage, setSelectedPackage] = useState<SelectedPackage | null>(null)
  const [operation, setOperation] = useState<OperationView | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [ambiguousAttempt, setAmbiguousAttempt] = useState<ProcessAttempt | null>(null)

  async function selectFile(event: ChangeEvent<HTMLInputElement>): Promise<void> {
    const file = event.currentTarget.files?.[0]
    if (file === undefined) {
      setFileError(null)
      return
    }
    if (file.type !== pdfMediaType && file.type !== docxMediaType) {
      setFileError("Choose a PDF or DOCX file.")
      return
    }
    if (file.size === 0) {
      setFileError("Choose a non-empty PDF or DOCX file.")
      return
    }
    const fileName = file.name
    const mediaType = file.type as MediaType
    event.currentTarget.value = ""
    setFileError(null)
    setAmbiguousAttempt(null)
    setSelectedPackage(null)
    setHashing(true)
    try {
      setSelectedPackage({digest: await digestFile(file), fileName, mediaType})
    } catch {
      setFileError("The selected file could not be hashed safely.")
    } finally {
      setHashing(false)
    }
  }

  async function submitAttempt(attempt: ProcessAttempt): Promise<void> {
    setSubmitting(true)
    setFileError(null)
    try {
      const result = await client.submitProcessPackage(attempt.command, attempt.context)
      setAmbiguousAttempt(null)
      setOperation(result.envelope.data)
    } catch (error: unknown) {
      if (error instanceof ConsoleMutationOutcomeUnknown) {
        setAmbiguousAttempt(attempt)
        setFileError("The process submission outcome is unknown.")
      } else {
        setFileError("The process package submission could not be reconciled safely.")
      }
    } finally {
      setSubmitting(false)
    }
  }

  function submitPackage(): void {
    if (selectedPackage === null) {
      return
    }
    void submitAttempt({
      command: {
        expected_revision: setup.revision,
        package_digest: selectedPackage.digest,
        active_role: session.active_role,
        file_name: selectedPackage.fileName,
        media_type: selectedPackage.mediaType,
      },
      context: {
        csrfToken: session.csrf_token,
        idempotencyKey: idempotencyKeyFactory(),
      },
    })
  }

  return (
    <section aria-labelledby="process-title" className="setup-stage">
      <p className="eyebrow">Stage 4 · Business process</p>
      <h1 id="process-title">Describe the business process</h1>
      <p className="setup-stage__lead">
        PillarMesh records only package metadata and the SHA-256 identity of the original content.
      </p>
      <label className="process-file">
        <span>Process package</span>
        <input
          accept={`${pdfMediaType},${docxMediaType}`}
          onChange={(event) => void selectFile(event)}
          type="file"
        />
      </label>
      <p className="process-file__guidance">
        Accepted files: PDF or DOCX. Empty files are not accepted.
      </p>
      {hashing ? <p role="status">Computing original-content digest…</p> : null}
      {fileError === null ? null : <p role="alert">{fileError}</p>}
      {ambiguousAttempt === null ? null : (
        <button
          disabled={submitting}
          onClick={() => void submitAttempt(ambiguousAttempt)}
          type="button"
        >
          Reconcile process submission
        </button>
      )}
      {selectedPackage === null ? null : (
        <section aria-label="Selected process package" className="process-package">
          <h2>{selectedPackage.fileName}</h2>
          <dl>
            <div>
              <dt>Original-content SHA-256</dt>
              <dd>{selectedPackage.digest}</dd>
            </div>
            <div>
              <dt>Media type</dt>
              <dd>{selectedPackage.mediaType}</dd>
            </div>
          </dl>
          <p className="setup-warning">
            This material edit creates a new setup revision and invalidates downstream approvals
            whose exact input changes.
          </p>
          <button
            className="primary-action"
            disabled={submitting}
            onClick={submitPackage}
            type="button"
          >
            {submitting ? "Submitting…" : "Submit process package"}
          </button>
        </section>
      )}
      {setup.process_package === null || setup.process_package === undefined ? null : (
        <section aria-label="Current process package" className="process-package">
          <h2>Current process package</h2>
          <p>{setup.process_package.candidate_summary}</p>
          <dl>
            <div>
              <dt>Version</dt>
              <dd>{setup.process_package.version}</dd>
            </div>
            <div>
              <dt>Original-content SHA-256</dt>
              <dd>{setup.process_package.content_digest}</dd>
            </div>
          </dl>
        </section>
      )}
      {operation === null ? null : (
        <OperationStatus
          client={client}
          label="Process package operation"
          operation={operation}
          pollTimer={pollTimer}
        />
      )}
    </section>
  )
}
