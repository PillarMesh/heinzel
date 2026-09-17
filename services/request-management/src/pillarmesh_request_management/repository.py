from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, Self

from pillarmesh_contract_model import canonical_bytes, digest

from .models import (
    ConversationAuthorRole,
    ConversationEntry,
    DecisionBinding,
    DecisionKind,
    InboxRequest,
    RequestState,
    TransitionEvent,
)

if TYPE_CHECKING:
    from .product_intent import (
        ApprovedProductIntent,
        ProductIntent,
        ProductIntentAuthorityRefs,
        ProductIntentCandidate,
        ProductIntentConstraints,
        ProductIntentSourceCoverage,
    )


class RequestRepository(Protocol):
    def peek_next_sequence(self, tenant_id: str) -> int: ...

    def next_sequence(self, tenant_id: str) -> int: ...

    def save_prepared(self, request: InboxRequest) -> None: ...

    def save(self, request: InboxRequest) -> None: ...

    def transition(
        self,
        tenant_id: str,
        request_id: str,
        expected_revision: int,
        actor_id: str,
        to_state: RequestState,
        created_at: datetime,
    ) -> InboxRequest: ...

    def append_conversation(
        self,
        tenant_id: str,
        request_id: str,
        expected_revision: int,
        actor_id: str,
        body: str,
        created_at: datetime,
        *,
        author_role: ConversationAuthorRole | None = None,
    ) -> ConversationEntry: ...

    def record_decision(
        self,
        tenant_id: str,
        request_id: str,
        request_revision: int,
        actor_id: str,
        kind: DecisionKind,
        subject_digest: str,
        created_at: datetime,
    ) -> DecisionBinding: ...

    def load(self, tenant_id: str, request_id: str) -> InboxRequest | None: ...

    def list_inbox(self, tenant_id: str) -> tuple[InboxRequest, ...]: ...

    def list_transition_history(
        self, tenant_id: str, request_id: str
    ) -> tuple[TransitionEvent, ...]: ...

    def list_conversation(
        self, tenant_id: str, request_id: str
    ) -> tuple[ConversationEntry, ...]: ...

    def list_decisions(self, tenant_id: str, request_id: str) -> tuple[DecisionBinding, ...]: ...

    def has_fulfillment_proposal(self, tenant_id: str, request_id: str) -> bool: ...

    def discard_unapproved_semantic_request(
        self, tenant_id: str, request_id: str, review_bundle_digest: str
    ) -> None: ...

    def compensate_transition(self, prior: InboxRequest, attempted_state: RequestState) -> None: ...


class StaleRevisionError(Exception):
    pass


class SQLiteRequestRepository:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        _owns_connection: bool = False,
    ) -> None:
        self._connection = connection
        self._owns_connection = _owns_connection
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS request_revisions ("
            "request_id TEXT NOT NULL, "
            "revision INTEGER NOT NULL, "
            "tenant_id TEXT NOT NULL, "
            "submitted_at TEXT NOT NULL, "
            "payload BLOB NOT NULL, "
            "PRIMARY KEY (request_id, revision)"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS request_sequences ("
            "tenant_id TEXT PRIMARY KEY, "
            "next_sequence INTEGER NOT NULL"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS conversation_entries ("
            "entry_id TEXT PRIMARY KEY, "
            "request_id TEXT NOT NULL, "
            "request_revision INTEGER NOT NULL, "
            "tenant_id TEXT NOT NULL, "
            "created_at TEXT NOT NULL, "
            "payload BLOB NOT NULL"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS decision_bindings ("
            "decision_id TEXT PRIMARY KEY, "
            "request_id TEXT NOT NULL, "
            "request_revision INTEGER NOT NULL, "
            "tenant_id TEXT NOT NULL, "
            "created_at TEXT NOT NULL, "
            "payload BLOB NOT NULL"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS transition_events ("
            "event_id TEXT PRIMARY KEY, "
            "request_id TEXT NOT NULL, "
            "request_revision INTEGER NOT NULL, "
            "tenant_id TEXT NOT NULL, "
            "created_at TEXT NOT NULL, "
            "payload BLOB NOT NULL"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS artifact_sequences ("
            "tenant_id TEXT NOT NULL, "
            "artifact_kind TEXT NOT NULL, "
            "next_sequence INTEGER NOT NULL, "
            "PRIMARY KEY (tenant_id, artifact_kind)"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS product_intent_approvals ("
            "approval_id TEXT PRIMARY KEY, "
            "request_id TEXT NOT NULL, "
            "request_revision INTEGER NOT NULL, "
            "intent_revision INTEGER NOT NULL, "
            "tenant_id TEXT NOT NULL, "
            "approved_at TEXT NOT NULL, "
            "intent_digest TEXT NOT NULL, "
            "payload BLOB NOT NULL, "
            "UNIQUE (request_id, request_revision), "
            "UNIQUE (request_id, intent_revision)"
            ")"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS product_intent_candidates ("
            "candidate_id TEXT PRIMARY KEY, "
            "tenant_id TEXT NOT NULL, "
            "request_id TEXT NOT NULL, "
            "request_revision INTEGER NOT NULL, "
            "idempotency_key TEXT NOT NULL, "
            "input_digest TEXT NOT NULL, "
            "payload BLOB NOT NULL, "
            "UNIQUE (tenant_id, idempotency_key), "
            "UNIQUE (tenant_id, request_id, request_revision)"
            ")"
        )
        self._connection.commit()

    @classmethod
    def open(cls, database_path: str) -> Self:
        return cls(sqlite3.connect(database_path), _owns_connection=True)

    def close(self) -> None:
        if self._owns_connection:
            self._connection.close()

    def has_fulfillment_proposal(self, tenant_id: str, request_id: str) -> bool:
        """Report whether any fulfillment proposal was written for this request.

        Deliberately not scoped to one revision. A proposal is written at the
        `proposed` revision and the request then advances to `awaiting_approval`,
        so a revision-scoped check is inert for the whole approval window.
        """
        table = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fulfillment_proposals'"
        ).fetchone()
        if table is None:
            return False
        row = self._connection.execute(
            "SELECT 1 FROM fulfillment_proposals WHERE tenant_id = ? AND request_id = ? LIMIT 1",
            (tenant_id, request_id),
        ).fetchone()
        return row is not None

    def next_sequence(self, tenant_id: str) -> int:
        row = self._connection.execute(
            "INSERT INTO request_sequences (tenant_id, next_sequence) VALUES (?, 2) "
            "ON CONFLICT(tenant_id) DO UPDATE "
            "SET next_sequence = request_sequences.next_sequence + 1 "
            "RETURNING next_sequence - 1",
            (tenant_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("request sequence allocation did not return a sequence")
        self._connection.commit()
        return int(row[0])

    def peek_next_sequence(self, tenant_id: str) -> int:
        row = self._connection.execute(
            "SELECT next_sequence FROM request_sequences WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        return 1 if row is None else int(row[0])

    def save_prepared(self, request: InboxRequest) -> None:
        prepared_sequence = int(request.request_id.split("-", 2)[1])
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            if self.peek_next_sequence(request.tenant_id) != prepared_sequence:
                raise StaleRevisionError("prepared request sequence is stale")
            self._connection.execute(
                "INSERT INTO request_sequences (tenant_id, next_sequence) VALUES (?, ?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET next_sequence = excluded.next_sequence",
                (request.tenant_id, prepared_sequence + 1),
            )
            self._save_request_revision(request)
        except BaseException:
            self._connection.rollback()
            raise
        self._connection.commit()

    def save(self, request: InboxRequest) -> None:
        try:
            self._save_request_revision(request)
        except (sqlite3.IntegrityError, StaleRevisionError) as error:
            self._connection.rollback()
            raise StaleRevisionError("request revision was not advanced") from error
        except BaseException:
            # Any other failure must not leave the implicit transaction open: the next
            # successful call's commit would publish this partial write.
            self._connection.rollback()
            raise
        self._connection.commit()

    def transition(
        self,
        tenant_id: str,
        request_id: str,
        expected_revision: int,
        actor_id: str,
        to_state: RequestState,
        created_at: datetime,
    ) -> InboxRequest:
        try:
            with _transaction(self._connection):
                transitioned = self._transition_within_transaction(
                    tenant_id=tenant_id,
                    request_id=request_id,
                    expected_revision=expected_revision,
                    actor_id=actor_id,
                    to_state=to_state,
                    created_at=created_at,
                )
        except StaleRevisionError:
            raise
        except sqlite3.IntegrityError as error:
            raise StaleRevisionError("request revision was not advanced") from error
        return transitioned

    def append_conversation(
        self,
        tenant_id: str,
        request_id: str,
        expected_revision: int,
        actor_id: str,
        body: str,
        created_at: datetime,
        *,
        author_role: ConversationAuthorRole | None = None,
    ) -> ConversationEntry:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            request = self._load_owned_request(tenant_id, request_id)
            if request.revision != expected_revision:
                raise StaleRevisionError("request revision was not advanced")
            entry = ConversationEntry(
                author_role=author_role,
                entry_id="pending",
                request_id=request.request_id,
                request_revision=request.revision + 1,
                actor_id=actor_id,
                body=body,
                created_at=created_at,
            )
            sequence = self._allocate_artifact_sequence(tenant_id, "conversation")
            entry = entry.model_copy(
                update={"entry_id": self._artifact_id("conversation", tenant_id, sequence)}
            )
            advanced_request = request.model_copy(
                update={"revision": request.revision + 1, "updated_at": created_at}
            )
            self._save_request_revision(advanced_request)
            self._connection.execute(
                "INSERT INTO conversation_entries "
                "(entry_id, request_id, request_revision, tenant_id, created_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    entry.entry_id,
                    entry.request_id,
                    entry.request_revision,
                    tenant_id,
                    entry.created_at.isoformat(),
                    canonical_bytes(entry),
                ),
            )
        except StaleRevisionError:
            self._connection.rollback()
            raise
        except sqlite3.IntegrityError as error:
            self._connection.rollback()
            raise StaleRevisionError("request revision was not advanced") from error
        except Exception:
            self._connection.rollback()
            raise
        self._connection.commit()
        return entry

    def record_decision(
        self,
        tenant_id: str,
        request_id: str,
        request_revision: int,
        actor_id: str,
        kind: DecisionKind,
        subject_digest: str,
        created_at: datetime,
    ) -> DecisionBinding:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            request = self._load_owned_request(tenant_id, request_id)
            if request.revision != request_revision:
                raise StaleRevisionError("request revision was not advanced")
            decision = DecisionBinding(
                decision_id="pending",
                request_id=request.request_id,
                request_revision=request_revision,
                actor_id=actor_id,
                kind=kind,
                subject_digest=subject_digest,
                created_at=created_at,
            )
            sequence = self._allocate_artifact_sequence(tenant_id, "decision")
            decision = decision.model_copy(
                update={"decision_id": self._artifact_id("decision", tenant_id, sequence)}
            )
            result = self._connection.execute(
                "INSERT INTO decision_bindings "
                "(decision_id, request_id, request_revision, tenant_id, created_at, payload) "
                "SELECT ?, ?, ?, ?, ?, ? "
                "WHERE EXISTS ("
                "SELECT 1 FROM request_revisions "
                "WHERE tenant_id = ? AND request_id = ? AND revision = ?"
                ") AND NOT EXISTS ("
                "SELECT 1 FROM request_revisions WHERE request_id = ? AND revision > ?"
                ")",
                (
                    decision.decision_id,
                    request_id,
                    request_revision,
                    tenant_id,
                    decision.created_at.isoformat(),
                    canonical_bytes(decision),
                    tenant_id,
                    request_id,
                    request_revision,
                    request_id,
                    request_revision,
                ),
            )
            if result.rowcount != 1:
                raise StaleRevisionError("request revision was not advanced")
        except StaleRevisionError as error:
            self._connection.rollback()
            raise StaleRevisionError("request revision was not advanced") from error
        except sqlite3.IntegrityError as error:
            self._connection.rollback()
            raise StaleRevisionError("request revision was not advanced") from error
        except Exception:
            self._connection.rollback()
            raise
        self._connection.commit()
        return decision

    def load(self, tenant_id: str, request_id: str) -> InboxRequest | None:
        owner = self._connection.execute(
            "SELECT tenant_id FROM request_revisions "
            "WHERE request_id = ? ORDER BY revision DESC LIMIT 1",
            (request_id,),
        ).fetchone()
        if owner is None:
            return None
        if owner[0] != tenant_id:
            raise KeyError(f"request {request_id} belongs to another tenant")
        row = self._connection.execute(
            "SELECT payload FROM request_revisions "
            "WHERE tenant_id = ? AND request_id = ? ORDER BY revision DESC LIMIT 1",
            (tenant_id, request_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("request revision is missing after ownership validation")
        return InboxRequest.model_validate_json(row[0])

    def list_inbox(self, tenant_id: str) -> tuple[InboxRequest, ...]:
        rows = self._connection.execute(
            "SELECT current.payload FROM request_revisions AS current "
            "WHERE current.tenant_id = ? AND current.revision = ("
            "SELECT MAX(candidate.revision) FROM request_revisions AS candidate "
            "WHERE candidate.request_id = current.request_id"
            ") ORDER BY current.submitted_at, current.request_id",
            (tenant_id,),
        ).fetchall()
        return tuple(InboxRequest.model_validate_json(row[0]) for row in rows)

    def list_transition_history(
        self, tenant_id: str, request_id: str
    ) -> tuple[TransitionEvent, ...]:
        self._load_owned_request(tenant_id, request_id)
        rows = self._connection.execute(
            "SELECT payload FROM transition_events "
            "WHERE tenant_id = ? AND request_id = ? ORDER BY request_revision, event_id",
            (tenant_id, request_id),
        ).fetchall()
        return tuple(TransitionEvent.model_validate_json(row[0]) for row in rows)

    def list_conversation(self, tenant_id: str, request_id: str) -> tuple[ConversationEntry, ...]:
        """Read the thread in append order.

        The tenant-qualified load runs first so a leaked request identifier cannot
        be probed for existence: a foreign request and an absent one raise the
        same KeyError.
        """
        self._load_owned_request(tenant_id, request_id)
        rows = self._connection.execute(
            "SELECT payload FROM conversation_entries WHERE tenant_id = ? AND request_id = ? "
            "ORDER BY created_at, entry_id",
            (tenant_id, request_id),
        ).fetchall()
        return tuple(ConversationEntry.model_validate_json(row[0]) for row in rows)

    def list_decisions(self, tenant_id: str, request_id: str) -> tuple[DecisionBinding, ...]:
        self._load_owned_request(tenant_id, request_id)
        rows = self._connection.execute(
            "SELECT payload FROM decision_bindings WHERE tenant_id = ? AND request_id = ? "
            "ORDER BY created_at, decision_id",
            (tenant_id, request_id),
        ).fetchall()
        return tuple(DecisionBinding.model_validate_json(row[0]) for row in rows)

    def record_product_intent_candidate(
        self,
        *,
        tenant_id: str,
        request_id: str,
        request_revision: int,
        idempotency_key: str,
        input_digest: str,
        proposed_by: str,
        proposed_at: datetime,
        intent: ProductIntent,
        constraints: ProductIntentConstraints,
        source_coverage: tuple[ProductIntentSourceCoverage, ...],
        unresolved_constraints: tuple[str, ...],
        authority_refs: ProductIntentAuthorityRefs | None = None,
    ) -> ProductIntentCandidate:
        from .product_intent import (
            ProductIntentCandidate,
            ProductIntentCandidateConflictError,
            ProductIntentCandidateStaleRevisionError,
        )

        with _transaction(self._connection):
            existing_row = self._connection.execute(
                "SELECT input_digest, payload FROM product_intent_candidates "
                "WHERE tenant_id = ? AND idempotency_key = ?",
                (tenant_id, idempotency_key),
            ).fetchone()
            if existing_row is not None:
                if existing_row[0] != input_digest:
                    raise ProductIntentCandidateConflictError(
                        "idempotency key already proposed a different product intent candidate"
                    )
                return ProductIntentCandidate.model_validate_json(existing_row[1])

            try:
                request = self._load_owned_request(tenant_id, request_id)
            except KeyError:
                raise KeyError("request is unavailable") from None
            if request.revision != request_revision:
                raise ProductIntentCandidateStaleRevisionError("request revision is stale")
            existing_revision = self._connection.execute(
                "SELECT input_digest, payload FROM product_intent_candidates "
                "WHERE tenant_id = ? AND request_id = ? AND request_revision = ?",
                (tenant_id, request_id, request_revision),
            ).fetchone()
            if existing_revision is not None:
                if existing_revision[0] == input_digest:
                    return ProductIntentCandidate.model_validate_json(existing_revision[1])
                raise ProductIntentCandidateConflictError(
                    "request revision already has a different product intent candidate"
                )
            sequence = self._allocate_artifact_sequence(tenant_id, "product-intent-candidate")
            candidate = ProductIntentCandidate(
                candidate_id=self._artifact_id("product-intent-candidate", tenant_id, sequence),
                tenant_id=tenant_id,
                request_id=request_id,
                request_revision=request_revision,
                intent=intent,
                constraints=constraints,
                source_coverage=source_coverage,
                unresolved_constraints=unresolved_constraints,
                proposed_by=proposed_by,
                proposed_at=proposed_at,
                authority_refs=authority_refs,
            )
            self._connection.execute(
                "INSERT INTO product_intent_candidates "
                "(candidate_id, tenant_id, request_id, request_revision, idempotency_key, "
                "input_digest, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate.candidate_id,
                    tenant_id,
                    request_id,
                    request_revision,
                    idempotency_key,
                    input_digest,
                    canonical_bytes(candidate),
                ),
            )
        return candidate

    def load_current_product_intent_candidate(
        self, tenant_id: str, request_id: str
    ) -> ProductIntentCandidate | None:
        from .product_intent import ProductIntentCandidate

        try:
            request = self._load_owned_request(tenant_id, request_id)
        except KeyError:
            raise KeyError("request is unavailable") from None
        row = self._connection.execute(
            "SELECT payload FROM product_intent_candidates "
            "WHERE tenant_id = ? AND request_id = ? AND request_revision = ?",
            (tenant_id, request_id, request.revision),
        ).fetchone()
        return None if row is None else ProductIntentCandidate.model_validate_json(row[0])

    def record_product_intent_approval(
        self,
        *,
        tenant_id: str,
        request_id: str,
        request_revision: int,
        approved_by: str,
        approved_at: datetime,
        intent: ProductIntent,
        authority_refs: ProductIntentAuthorityRefs,
        constraints: ProductIntentConstraints,
    ) -> ApprovedProductIntent:
        from .product_intent import ApprovedProductIntent, ProductIntent

        approved_intent = ProductIntent.model_validate(intent)
        intent_digest = digest(approved_intent)
        try:
            with _transaction(self._connection):
                request = self._load_owned_request(tenant_id, request_id)
                if request.revision != request_revision:
                    raise StaleRevisionError("request revision was not advanced")
                existing_row = self._connection.execute(
                    "SELECT payload FROM product_intent_approvals "
                    "WHERE tenant_id = ? AND request_id = ? AND request_revision = ?",
                    (tenant_id, request_id, request_revision),
                ).fetchone()
                if existing_row is not None:
                    existing = ApprovedProductIntent.model_validate_json(existing_row[0])
                    if existing.intent_digest != intent_digest:
                        raise ValueError("request revision already has a different approved intent")
                    if existing.authority_refs != authority_refs:
                        raise ValueError(
                            "request revision was already approved against different authority"
                        )
                    return existing
                revision_row = self._connection.execute(
                    "SELECT COALESCE(MAX(intent_revision), 0) + 1 "
                    "FROM product_intent_approvals WHERE tenant_id = ? AND request_id = ?",
                    (tenant_id, request_id),
                ).fetchone()
                if revision_row is None:
                    raise RuntimeError("intent revision allocation failed")
                intent_revision = int(revision_row[0])
                sequence = self._allocate_artifact_sequence(tenant_id, "product-intent")
                approval = ApprovedProductIntent(
                    approval_id=self._artifact_id("product-intent", tenant_id, sequence),
                    intent_revision=intent_revision,
                    tenant_id=tenant_id,
                    request_id=request_id,
                    request_revision=request_revision,
                    intent=approved_intent,
                    intent_digest=intent_digest,
                    approved_by=approved_by,
                    approved_at=approved_at,
                    authority_refs=authority_refs,
                    constraints=constraints,
                )
                self._connection.execute(
                    "INSERT INTO product_intent_approvals "
                    "(approval_id, request_id, request_revision, intent_revision, tenant_id, "
                    "approved_at, intent_digest, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        approval.approval_id,
                        request_id,
                        request_revision,
                        intent_revision,
                        tenant_id,
                        approved_at.isoformat(),
                        intent_digest,
                        canonical_bytes(approval),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise StaleRevisionError("product intent approval conflicted") from error
        return approval

    def load_product_intent_approval(
        self, tenant_id: str, approval_id: str
    ) -> ApprovedProductIntent | None:
        from .product_intent import ApprovedProductIntent

        row = self._connection.execute(
            "SELECT payload FROM product_intent_approvals WHERE tenant_id = ? AND approval_id = ?",
            (tenant_id, approval_id),
        ).fetchone()
        return None if row is None else ApprovedProductIntent.model_validate_json(row[0])

    def list_product_intent_approvals(
        self, tenant_id: str, request_id: str
    ) -> tuple[ApprovedProductIntent, ...]:
        from .product_intent import ApprovedProductIntent

        self._load_owned_request(tenant_id, request_id)
        rows = self._connection.execute(
            "SELECT payload FROM product_intent_approvals "
            "WHERE tenant_id = ? AND request_id = ? ORDER BY intent_revision",
            (tenant_id, request_id),
        ).fetchall()
        return tuple(ApprovedProductIntent.model_validate_json(row[0]) for row in rows)

    def discard_unapproved_semantic_request(
        self, tenant_id: str, request_id: str, review_bundle_digest: str
    ) -> None:
        from .models import SchemaSemanticChangeRequest

        with _transaction(self._connection):
            request = self._load_owned_request(tenant_id, request_id)
            if (
                not isinstance(request.payload, SchemaSemanticChangeRequest)
                or request.payload.review_bundle_digest != review_bundle_digest
                or self.list_decisions(tenant_id, request_id)
            ):
                raise ValueError("request is not a compensable semantic submission")
            revisions = tuple(
                InboxRequest.model_validate_json(row[0])
                for row in self._connection.execute(
                    "SELECT payload FROM request_revisions WHERE tenant_id = ? AND request_id = ? "
                    "ORDER BY revision",
                    (tenant_id, request_id),
                ).fetchall()
            )
            expected_states = (
                RequestState.SUBMITTED,
                RequestState.INVESTIGATING,
                RequestState.PROPOSED,
                RequestState.AWAITING_APPROVAL,
            )
            if tuple(value.state for value in revisions) not in tuple(
                expected_states[:length] for length in range(1, len(expected_states) + 1)
            ):
                raise ValueError("request is not a compensable semantic submission")
            events = self.list_transition_history(tenant_id, request_id)
            if tuple(event.to_state for event in events) != expected_states[1 : len(revisions)]:
                raise ValueError("request is not a compensable semantic submission")
            request_sequence = int(request_id.split("-", 2)[1])
            if self.peek_next_sequence(tenant_id) != request_sequence + 1:
                raise ValueError("request sequence advanced beyond compensable submission")
            transition_sequences = tuple(int(event.event_id.split("-", 2)[1]) for event in events)
            transition_row = self._connection.execute(
                "SELECT next_sequence FROM artifact_sequences "
                "WHERE tenant_id = ? AND artifact_kind = 'transition'",
                (tenant_id,),
            ).fetchone()
            if transition_sequences and (
                transition_row is None
                or int(transition_row[0]) != transition_sequences[-1] + 1
                or transition_sequences
                != tuple(range(transition_sequences[0], transition_sequences[0] + len(events)))
            ):
                raise ValueError("transition sequence advanced beyond compensable submission")
            self._connection.execute(
                "DELETE FROM transition_events WHERE request_id = ?", (request_id,)
            )
            self._connection.execute(
                "DELETE FROM request_revisions WHERE request_id = ?", (request_id,)
            )
            if request_sequence == 1:
                self._connection.execute(
                    "DELETE FROM request_sequences WHERE tenant_id = ?", (tenant_id,)
                )
            else:
                self._connection.execute(
                    "UPDATE request_sequences SET next_sequence = ? WHERE tenant_id = ?",
                    (request_sequence, tenant_id),
                )
            if transition_sequences:
                if transition_sequences[0] == 1:
                    self._connection.execute(
                        "DELETE FROM artifact_sequences "
                        "WHERE tenant_id = ? AND artifact_kind = 'transition'",
                        (tenant_id,),
                    )
                else:
                    self._connection.execute(
                        "UPDATE artifact_sequences SET next_sequence = ? "
                        "WHERE tenant_id = ? AND artifact_kind = 'transition'",
                        (transition_sequences[0], tenant_id),
                    )

    def compensate_transition(self, prior: InboxRequest, attempted_state: RequestState) -> None:
        with _transaction(self._connection):
            current = self._load_owned_request(prior.tenant_id, prior.request_id)
            if canonical_bytes(current) == canonical_bytes(prior):
                return
            if current.revision != prior.revision + 1 or current.state is not attempted_state:
                raise ValueError("request transition is not compensable")
            rows = self._connection.execute(
                "SELECT payload FROM transition_events "
                "WHERE tenant_id = ? AND request_id = ? AND request_revision = ?",
                (prior.tenant_id, prior.request_id, current.revision),
            ).fetchall()
            if len(rows) != 1:
                raise ValueError("request transition is not compensable")
            event = TransitionEvent.model_validate_json(rows[0][0])
            if event.from_state is not prior.state or event.to_state is not attempted_state:
                raise ValueError("request transition is not compensable")
            transition_sequence = int(event.event_id.split("-", 2)[1])
            sequence_row = self._connection.execute(
                "SELECT next_sequence FROM artifact_sequences "
                "WHERE tenant_id = ? AND artifact_kind = 'transition'",
                (prior.tenant_id,),
            ).fetchone()
            if sequence_row is None or int(sequence_row[0]) != transition_sequence + 1:
                raise ValueError("transition sequence advanced beyond compensable transition")
            self._connection.execute(
                "DELETE FROM transition_events WHERE event_id = ?", (event.event_id,)
            )
            self._connection.execute(
                "DELETE FROM request_revisions WHERE request_id = ? AND revision = ?",
                (prior.request_id, current.revision),
            )
            if transition_sequence == 1:
                self._connection.execute(
                    "DELETE FROM artifact_sequences "
                    "WHERE tenant_id = ? AND artifact_kind = 'transition'",
                    (prior.tenant_id,),
                )
            else:
                self._connection.execute(
                    "UPDATE artifact_sequences SET next_sequence = ? "
                    "WHERE tenant_id = ? AND artifact_kind = 'transition'",
                    (transition_sequence, prior.tenant_id),
                )

    # ------------------------------------------------------------------
    # In-transaction surface.
    #
    # A composing repository that shares this connection must use these, not the
    # ordinary public methods: each of those opens its own `BEGIN IMMEDIATE`, and
    # SQLite raises "cannot start a transaction within a transaction". These do
    # not open, commit, or roll back anything. The writes additionally refuse to
    # run outside an open transaction, so the contract fails loudly rather than
    # autocommitting a half-written outcome.
    # ------------------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        """The shared connection, for a composing repository's transaction scope."""
        return self._connection

    def _require_open_transaction(self, operation: str) -> None:
        if not self._connection.in_transaction:
            raise RuntimeError(f"{operation} must run inside an open transaction")

    def load_owned_request(self, tenant_id: str, request_id: str) -> InboxRequest:
        """Load a request the tenant owns, raising KeyError otherwise.

        A read, so it carries no transaction requirement; the write helpers below
        do.
        """
        return self._load_owned_request(tenant_id, request_id)

    def allocate_artifact_sequence_in_transaction(self, tenant_id: str, artifact_kind: str) -> int:
        self._require_open_transaction("allocating an artifact sequence")
        return self._allocate_artifact_sequence(tenant_id, artifact_kind)

    def save_request_revision_in_transaction(self, request: InboxRequest) -> None:
        self._require_open_transaction("saving a request revision")
        self._save_request_revision(request)

    def transition_in_transaction(
        self,
        *,
        tenant_id: str,
        request_id: str,
        expected_revision: int,
        actor_id: str,
        to_state: RequestState,
        created_at: datetime,
    ) -> InboxRequest:
        self._require_open_transaction("transitioning a request")
        return self._transition_within_transaction(
            tenant_id=tenant_id,
            request_id=request_id,
            expected_revision=expected_revision,
            actor_id=actor_id,
            to_state=to_state,
            created_at=created_at,
        )

    def _load_owned_request(self, tenant_id: str, request_id: str) -> InboxRequest:
        request = self.load(tenant_id, request_id)
        if request is None:
            raise KeyError(f"request {request_id} belongs to another tenant")
        return request

    def _transition_within_transaction(
        self,
        *,
        tenant_id: str,
        request_id: str,
        expected_revision: int,
        actor_id: str,
        to_state: RequestState,
        created_at: datetime,
    ) -> InboxRequest:
        request = self._load_owned_request(tenant_id, request_id)
        if request.revision != expected_revision:
            raise StaleRevisionError("request revision was not advanced")
        transitioned = request.model_copy(
            update={
                "state": to_state,
                "revision": request.revision + 1,
                "updated_at": created_at,
            }
        )
        sequence = self._allocate_artifact_sequence(tenant_id, "transition")
        event = TransitionEvent(
            event_id=self._artifact_id("transition", tenant_id, sequence),
            request_id=request.request_id,
            request_revision=transitioned.revision,
            actor_id=actor_id,
            from_state=request.state,
            to_state=to_state,
            created_at=created_at,
        )
        self._save_request_revision(transitioned)
        self._connection.execute(
            "INSERT INTO transition_events "
            "(event_id, request_id, request_revision, tenant_id, created_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.request_id,
                event.request_revision,
                tenant_id,
                event.created_at.isoformat(),
                canonical_bytes(event),
            ),
        )
        return transitioned

    def _allocate_artifact_sequence(self, tenant_id: str, artifact_kind: str) -> int:
        row = self._connection.execute(
            "INSERT INTO artifact_sequences (tenant_id, artifact_kind, next_sequence) "
            "VALUES (?, ?, 2) "
            "ON CONFLICT(tenant_id, artifact_kind) DO UPDATE "
            "SET next_sequence = artifact_sequences.next_sequence + 1 "
            "RETURNING next_sequence - 1",
            (tenant_id, artifact_kind),
        ).fetchone()
        if row is None:
            raise RuntimeError("artifact sequence allocation did not return a sequence")
        return int(row[0])

    @staticmethod
    def _artifact_id(artifact_kind: str, tenant_id: str, sequence: int) -> str:
        domain = f"pillarmesh-{artifact_kind}-v1"
        identity_digest = digest({"domain": domain, "tenant_id": tenant_id, "sequence": sequence})[
            :24
        ]
        prefix = {
            "conversation": "con",
            "decision": "dec",
            "product-intent": "pin",
            "product-intent-candidate": "pic",
            "transition": "trn",
        }[artifact_kind]
        return f"{prefix}-{sequence:020d}-{identity_digest}"

    def _save_request_revision(self, request: InboxRequest) -> None:
        payload = canonical_bytes(request)
        if request.revision == 1:
            self._connection.execute(
                "INSERT INTO request_revisions "
                "(request_id, revision, tenant_id, submitted_at, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    request.request_id,
                    request.revision,
                    request.tenant_id,
                    request.submitted_at.isoformat(),
                    payload,
                ),
            )
            return
        result = self._connection.execute(
            "INSERT INTO request_revisions "
            "(request_id, revision, tenant_id, submitted_at, payload) "
            "SELECT ?, ?, ?, ?, ? "
            "WHERE EXISTS ("
            "SELECT 1 FROM request_revisions "
            "WHERE request_id = ? AND tenant_id = ? AND revision = ?"
            ") AND NOT EXISTS ("
            "SELECT 1 FROM request_revisions WHERE request_id = ? AND revision = ?"
            ")",
            (
                request.request_id,
                request.revision,
                request.tenant_id,
                request.submitted_at.isoformat(),
                payload,
                request.request_id,
                request.tenant_id,
                request.revision - 1,
                request.request_id,
                request.revision,
            ),
        )
        if result.rowcount != 1:
            raise StaleRevisionError("request revision was not advanced")


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Hold the write lock across the guard reads as well as the writes.

    `with connection:` only commits or rolls back at exit; the transaction would not
    begin until the first write, leaving the compensation guards -- no decisions
    recorded, revision states match, sequence is still the latest -- readable while
    another connection changed the very rows they had just checked.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
