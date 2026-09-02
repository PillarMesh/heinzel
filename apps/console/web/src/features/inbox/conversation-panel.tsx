import {useState} from "react"

import type {
  AuthorRole,
  ConsoleEnvelopeConversationView,
  ConversationMessageCommand,
  ConversationView,
  SessionView,
} from "../../api/generated"
import type {MutationRequestContext} from "../../api/client"

const authorLabels = {
  pillarmesh: "PillarMesh question",
  requester: "Requester reply",
  data_architect: "Architect intervention",
  data_owner: "Data owner note",
  policy_approver: "Policy approver note",
  budget_approver: "Budget approver note",
} satisfies Record<AuthorRole, string>

export interface ConversationPanelClient {
  appendConversationMessage(
    requestId: string,
    command: ConversationMessageCommand,
    context: MutationRequestContext,
  ): Promise<ConsoleEnvelopeConversationView>
}

interface ConversationPanelProps {
  readonly client?: ConversationPanelClient
  readonly conversation: ConversationView
  readonly conversationDigest?: string
  readonly idempotencyKeyFactory?: () => string
  readonly session: SessionView
}

function defaultIdempotencyKey(): string {
  return `conversation-${globalThis.crypto.randomUUID()}`
}

export function ConversationPanel({
  client,
  conversation,
  conversationDigest,
  idempotencyKeyFactory = defaultIdempotencyKey,
  session,
}: ConversationPanelProps) {
  // The authoritative projection returned by a command supersedes the prop until the parent
  // supplies a newer conversation, which is recognized by identity rather than by an effect.
  const [authoritative, setAuthoritative] = useState<{
    readonly source: ConversationView
    readonly value: ConversationView
  } | null>(null)
  const [body, setBody] = useState("")
  const [submitting, setSubmitting] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)

  const active =
    authoritative !== null && authoritative.source === conversation
      ? authoritative.value
      : conversation
  const messages = active.messages ?? []
  const canIntervene = client !== undefined && conversationDigest !== undefined

  async function sendMessage(): Promise<void> {
    if (client === undefined || conversationDigest === undefined || body.trim() === "") {
      return
    }
    setSubmitting(true)
    setFailure(null)
    try {
      const response = await client.appendConversationMessage(
        active.request_id,
        {
          active_role: session.active_role,
          body,
          conversation_digest: conversationDigest,
          expected_revision: active.revision,
        },
        {csrfToken: session.csrf_token, idempotencyKey: idempotencyKeyFactory()},
      )
      if (response.data.request_id !== active.request_id) {
        setFailure("The conversation could not be reconciled safely.")
        return
      }
      setAuthoritative({source: conversation, value: response.data})
      setBody("")
    } catch {
      setFailure("The message could not be recorded safely. The prior conversation is unchanged.")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <section aria-label="Clarification conversation" className="conversation-panel">
      <h3>Clarification conversation</h3>
      <p className="conversation-panel__awaiting">
        {active.awaiting_role === null || active.awaiting_role === undefined
          ? "No participant is awaited."
          : `Awaiting ${active.awaiting_role.replaceAll("_", " ")}`}
      </p>
      <ol aria-label="Conversation messages" className="conversation-panel__messages">
        {messages.map((message) => (
          <li
            className={`conversation-message conversation-message--${message.author_role}`}
            key={message.message_id}
          >
            <span className="conversation-message__kind">{authorLabels[message.author_role]}</span>
            <span className="conversation-message__author">{message.author_label}</span>
            <p>{message.body}</p>
            <time dateTime={message.created_at}>{message.created_at}</time>
          </li>
        ))}
      </ol>

      <label className="conversation-panel__compose">
        <span>Architect message</span>
        <textarea
          disabled={!canIntervene || submitting}
          onChange={(event) => setBody(event.currentTarget.value)}
          value={body}
        />
      </label>
      <button
        disabled={!canIntervene || submitting || body.trim() === ""}
        onClick={() => void sendMessage()}
        type="button"
      >
        Send architect message
      </button>
      {canIntervene ? null : (
        <p className="inbox-unavailable" role="status">
          Intervention is unavailable until the server supplies the conversation digest.
        </p>
      )}
      {failure === null ? null : <p role="alert">{failure}</p>}
    </section>
  )
}
