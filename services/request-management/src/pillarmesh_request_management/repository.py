import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Protocol

from pillarmesh_contract_model import canonical_bytes, digest

from .models import (
    ConversationEntry,
    DecisionBinding,
    DecisionKind,
    InboxRequest,
    RequestState,
    TransitionEvent,
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

    def list_decisions(self, tenant_id: str, request_id: str) -> tuple[DecisionBinding, ...]: ...

    def discard_unapproved_semantic_request(
        self, tenant_id: str, request_id: str, review_bundle_digest: str
    ) -> None: ...

    def compensate_transition(self, prior: InboxRequest, attempted_state: RequestState) -> None: ...


class StaleRevisionError(Exception):
    pass


class SQLiteRequestRepository:
    def __init__(self, database_path: str) -> None:
        self._connection = sqlite3.connect(database_path)
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
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

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
            self._connection.execute("BEGIN IMMEDIATE")
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
        return transitioned

    def append_conversation(
        self,
        tenant_id: str,
        request_id: str,
        expected_revision: int,
        actor_id: str,
        body: str,
        created_at: datetime,
    ) -> ConversationEntry:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            request = self._load_owned_request(tenant_id, request_id)
            if request.revision != expected_revision:
                raise StaleRevisionError("request revision was not advanced")
            entry = ConversationEntry(
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

    def list_decisions(self, tenant_id: str, request_id: str) -> tuple[DecisionBinding, ...]:
        self._load_owned_request(tenant_id, request_id)
        rows = self._connection.execute(
            "SELECT payload FROM decision_bindings WHERE tenant_id = ? AND request_id = ? "
            "ORDER BY created_at, decision_id",
            (tenant_id, request_id),
        ).fetchall()
        return tuple(DecisionBinding.model_validate_json(row[0]) for row in rows)

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

    def _load_owned_request(self, tenant_id: str, request_id: str) -> InboxRequest:
        request = self.load(tenant_id, request_id)
        if request is None:
            raise KeyError(f"request {request_id} belongs to another tenant")
        return request

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
        prefix = {"conversation": "con", "decision": "dec", "transition": "trn"}[artifact_kind]
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
