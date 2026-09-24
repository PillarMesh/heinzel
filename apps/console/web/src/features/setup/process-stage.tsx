import {useState, type ChangeEvent} from "react"

import {ConsoleMutationOutcomeUnknown} from "../../api/client"
import type {MutationRequestContext} from "../../api/client"
import type {
  BusinessProcessManifestCommand,
  MediaType,
  OperationView,
  ProcessPackageCommand,
  SessionView,
  SetupView,
} from "../../api/generated"
import {sha256File, type DigestFile} from "./file-digest"
import {OperationStatus, type PollTimer} from "./operation-status"
import type {IdempotencyKeyFactory, SetupClient} from "./setup-workbench"

const markdownMediaType = "text/markdown; charset=utf-8" as const
const manifestArrayFields = [
  "participants",
  "outcomes",
  "entities",
  "events",
  "states",
  "rules",
  "source_references",
  "unresolved_questions",
] as const
const manifestFields = new Set(["schema_version", "process_name", "owner", ...manifestArrayFields])

interface SelectedPackage {
  readonly digest: string
  readonly fileName: string
  readonly mediaType: MediaType
  readonly narrativeMarkdown: string
}

function parseManifest(source: string): BusinessProcessManifestCommand {
  let parsed: unknown
  try {
    parsed = JSON.parse(source)
  } catch {
    throw new Error("Manifest must be valid JSON.")
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("Manifest must be a JSON object.")
  }
  const record = parsed as Record<string, unknown>
  const unknown = Object.keys(record).find((field) => !manifestFields.has(field))
  if (unknown !== undefined) {
    throw new Error(`manifest.${unknown} is not allowed.`)
  }
  if (record.schema_version !== undefined && record.schema_version !== "1") {
    throw new Error('manifest.schema_version must be "1".')
  }
  for (const field of ["process_name", "owner"] as const) {
    const value = record[field]
    if (typeof value !== "string" || value.length === 0 || value.length > 128) {
      throw new Error(`manifest.${field} must be a non-empty string of at most 128 characters.`)
    }
  }
  for (const field of manifestArrayFields) {
    const value = record[field]
    if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
      throw new Error(`manifest.${field} must be an array of strings.`)
    }
  }
  return parsed as BusinessProcessManifestCommand
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
  const activeRole = session.active_role === "data_architect" ? session.active_role : null
  const [fileError, setFileError] = useState<string | null>(null)
  const [manifest, setManifest] = useState<BusinessProcessManifestCommand | null>(null)
  const [manifestError, setManifestError] = useState<string | null>(null)
  const [manifestSource, setManifestSource] = useState("")
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
    if (!file.name.toLowerCase().endsWith(".md")) {
      setFileError("Choose a Markdown (.md) file.")
      return
    }
    if (file.size === 0) {
      setFileError("Choose a non-empty Markdown file.")
      return
    }
    const fileName = file.name
    const mediaType = markdownMediaType
    event.currentTarget.value = ""
    setFileError(null)
    setAmbiguousAttempt(null)
    setSelectedPackage(null)
    setHashing(true)
    try {
      const bytes = await file.arrayBuffer()
      const narrativeMarkdown = new TextDecoder("utf-8", {fatal: true}).decode(bytes)
      setSelectedPackage({
        digest: await digestFile(file),
        fileName,
        mediaType,
        narrativeMarkdown,
      })
    } catch {
      setFileError("The selected file must contain valid UTF-8 Markdown.")
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
    if (selectedPackage === null || manifest === null || activeRole === null) {
      return
    }
    void submitAttempt({
      command: {
        expected_revision: setup.revision,
        package_digest: selectedPackage.digest,
        active_role: activeRole,
        file_name: selectedPackage.fileName,
        media_type: selectedPackage.mediaType,
        narrative_markdown: selectedPackage.narrativeMarkdown,
        manifest,
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
        Heinzel stores the exact UTF-8 Markdown narrative and its validated process manifest.
      </p>
      <label className="process-file">
        <span>Process package</span>
        <input
          accept=".md,text/markdown"
          onChange={(event) => void selectFile(event)}
          type="file"
        />
      </label>
      <p className="process-file__guidance">
        Accepted narrative: UTF-8 Markdown (.md). Add the matching JSON manifest below.
      </p>
      <label className="process-file">
        <span>Business process manifest (JSON)</span>
        <textarea
          onChange={(event) => {
            const source = event.currentTarget.value
            setManifestSource(source)
            try {
              setManifest(parseManifest(source))
              setManifestError(null)
            } catch (error: unknown) {
              setManifest(null)
              setManifestError(error instanceof Error ? error.message : "Manifest is invalid.")
            }
          }}
          rows={12}
          spellCheck={false}
          value={manifestSource}
        />
      </label>
      {hashing ? <p role="status">Computing original-content digest…</p> : null}
      {fileError === null ? null : <p role="alert">{fileError}</p>}
      {manifestError === null ? null : <p role="alert">{manifestError}</p>}
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
            Saving changes creates a new process version. Approvals based on earlier content must
            be reviewed again.
          </p>
          <button
            className="primary-action"
            disabled={submitting || manifest === null || activeRole === null}
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
