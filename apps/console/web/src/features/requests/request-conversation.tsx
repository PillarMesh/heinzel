import {useEffect, useState} from "react"

import {ConsoleApiError, ConsoleMutationOutcomeUnknown} from "../../api/client"
import type {
  ConversationMessageView,
  ConversationView,
  DataProvenance,
  SessionView,
} from "../../api/generated"
import type {DigestText, IdempotencyKeyFactory, RequesterClient} from "./my-requests"

interface RequestConversationProps {
  readonly client: RequesterClient
  readonly dataProvenance: DataProvenance
  readonly digestText: DigestText
  readonly idempotencyKeyFactory: IdempotencyKeyFactory
  readonly requestId: string
  readonly session: SessionView
}

type MessageOrigin = "pillarmesh" | "requester" | "architect" | "data_owner" | "policy_approver" | "budget_approver" | "unknown"

function messageOrigin(message: ConversationMessageView): MessageOrigin {
  if (message.author_role === null) {
    return "unknown"
  }
  return message.author_role === "data_architect" ? "architect" : message.author_role
}

// The label carries the distinction on its own, so colour is never the only signal.
const originLabels: Record<MessageOrigin, string> = {
  pillarmesh: "PillarMesh question",
  requester: "Your reply",
  architect: "Architect intervention",
  data_owner: "Data owner note",
  policy_approver: "Policy approver note",
  budget_approver: "Budget approver note",
  unknown: "Role not recorded",
}

// The conversation contract carries no digest field, so the browser binds the exact projection it
// replied to by hashing its canonical content.
function canonicalConversation(conversation: ConversationView): string {
  return JSON.stringify({
    conversation: {
      request_id: conversation.request_id,
      revision: conversation.revision,
      messages: (conversation.messages ?? []).map((message) => ({
        message_id: message.message_id,
        author_role: message.author_role,
        body: message.body,
        created_at: message.created_at,
      })),
    },
  })
}

export function RequestConversation({
  client,
  dataProvenance,
  digestText,
  idempotencyKeyFactory,
  requestId,
  session,
}: RequestConversationProps) {
  const [conversation, setConversation] = useState<ConversationView | null>(null)
  const [failed, setFailed] = useState(false)
  const [reply, setReply] = useState("")
  const [replyError, setReplyError] = useState<string | null>(null)
  const [sending, setSending] = useState(false)

  useEffect(() => {
    let active = true
    void client
      .getConversation(requestId)
      .then((envelope) => {
        if (!active) {
          return
        }
        if (
          envelope.meta.data_provenance !== dataProvenance ||
          envelope.data.request_id !== requestId
        ) {
          setFailed(true)
          return
        }
        setConversation(envelope.data)
      })
      .catch(() => {
        if (active) {
          setFailed(true)
        }
      })
    return () => {
      active = false
    }
  }, [client, dataProvenance, requestId])

  async function sendReply(): Promise<void> {
    if (conversation === null || reply.trim().length === 0) {
      setReplyError("A reply body is required.")
      return
    }
    setSending(true)
    setReplyError(null)
    try {
      const conversationDigest = await digestText(canonicalConversation(conversation))
      const envelope = await client.appendConversationMessage(
        requestId,
        {
          expected_revision: conversation.revision,
          conversation_digest: conversationDigest,
          active_role: session.active_role,
          body: reply.trim(),
        },
        {csrfToken: session.csrf_token, idempotencyKey: idempotencyKeyFactory()},
      )
      if (
        envelope.meta.data_provenance !== dataProvenance ||
        envelope.data.request_id !== requestId
      ) {
        setReplyError("The conversation could not be reconciled safely.")
        return
      }
      setConversation(envelope.data)
      setReply("")
    } catch (error: unknown) {
      if (error instanceof ConsoleMutationOutcomeUnknown) {
        setReplyError("The reply outcome is unknown. Reload before sending it again.")
      } else if (error instanceof ConsoleApiError) {
        setReplyError(error.message)
      } else {
        setReplyError("The reply could not be reconciled safely.")
      }
    } finally {
      setSending(false)
    }
  }

  if (failed) {
    return <p role="alert">The clarification conversation is unavailable.</p>
  }
  if (conversation === null) {
    return <p role="status">Loading the clarification conversation…</p>
  }

  return (
    <section aria-labelledby="conversation-title" className="request-conversation">
      <h2 id="conversation-title">Clarify</h2>
      {conversation.awaiting_role === "requester" ? (
        <p className="request-conversation__waiting">PillarMesh is waiting for your reply.</p>
      ) : null}
      <ul aria-label="Clarification conversation" className="conversation-thread">
        {(conversation.messages ?? []).map((message) => {
          const origin = messageOrigin(message)
          return (
            <li
              className={`conversation-message conversation-message--${origin}`}
              key={message.message_id}
            >
              <p className="conversation-message__label">{originLabels[origin]}</p>
              <p className="conversation-message__author">{message.author_label}</p>
              <p className="conversation-message__body">{message.body}</p>
              <p className="conversation-message__time">{message.created_at}</p>
            </li>
          )
        })}
      </ul>
      <label className="conversation-reply">
        <span>Your reply</span>
        <textarea onChange={(event) => setReply(event.currentTarget.value)} value={reply} />
      </label>
      {replyError === null ? null : <p role="alert">{replyError}</p>}
      <button
        className="primary-action"
        disabled={sending}
        onClick={() => void sendReply()}
        type="button"
      >
        {sending ? "Sending…" : "Send reply"}
      </button>
    </section>
  )
}
