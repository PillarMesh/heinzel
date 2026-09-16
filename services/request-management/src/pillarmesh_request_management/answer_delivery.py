from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from typing import Literal, Protocol

from pillarmesh_contract_model import canonical_bytes, digest

from .answer_errors import (
    GovernedAnswerConflict,
    GovernedAnswerNotVisible,
    GovernedAnswerStaleRevision,
    GovernedAnswerVerificationError,
)
from .answer_models import (
    AnswerAdmissionEvidence,
    AnswerDeliveryAuthorization,
    AnswerExecutionEvidence,
    AnswerNarrativeSource,
    AnswerPlanEvidence,
    AnswerProductGenerationReference,
    AnswerResultEvidence,
    DeliverGovernedAnswerCommand,
    GovernedAnswer,
)
from .models import RequestState
from .repository import SQLiteRequestRepository, StaleRevisionError

_NUMBER = re.compile(r"(?<![\w])[-+]?\d+(?:[.,]\d+)*(?![\w])")
_NAMED_ENTITY = re.compile(r"\b[A-Z][\w-]*(?:\s+[A-Z][\w-]*)*\b")
_SUPPRESSION_CLAUSE = " HAVING COUNT(DISTINCT "


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.commit()
    except BaseException:
        with suppress(sqlite3.Error):
            connection.rollback()
        raise


class AnswerAdmissionReader(Protocol):
    def read_admission(
        self, tenant_id: str, request_id: str, admission_ref: str
    ) -> AnswerAdmissionEvidence | None: ...


class AnswerExecutionReader(Protocol):
    def read_receipt(
        self,
        tenant_id: str,
        request_id: str,
        receipt_ref: str,
        plan_digest: str,
        product_generation_refs: tuple[AnswerProductGenerationReference, ...],
    ) -> AnswerExecutionEvidence | None: ...

    def read_result(
        self,
        tenant_id: str,
        request_id: str,
        result_ref: str,
        plan_digest: str,
        product_generation_refs: tuple[AnswerProductGenerationReference, ...],
    ) -> AnswerResultEvidence | None: ...


class AnswerPlanReader(Protocol):
    def read_plan(self, tenant_id: str, plan_digest: str) -> AnswerPlanEvidence | None: ...


class AnswerAuthorizationRechecker(Protocol):
    def recheck(
        self,
        *,
        tenant_id: str,
        request_id: str,
        requester_id: str,
        plan_digest: str,
        required_permission: Literal["view", "download"],
    ) -> AnswerDeliveryAuthorization: ...


class SQLiteGovernedAnswerRepository:
    def __init__(
        self,
        requests: SQLiteRequestRepository,
    ) -> None:
        self._requests = requests
        self._connection = requests.connection
        self._connection.executescript(
            "CREATE TABLE IF NOT EXISTS governed_answer_sequences ("
            "tenant_id TEXT PRIMARY KEY, next_sequence INTEGER NOT NULL);"
            "CREATE TABLE IF NOT EXISTS governed_answers ("
            "answer_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, request_id TEXT NOT NULL, "
            "request_revision INTEGER NOT NULL, command_digest TEXT NOT NULL, "
            "payload BLOB NOT NULL, "
            "UNIQUE (tenant_id, request_id, request_revision));"
        )

    def load_delivery(
        self, tenant_id: str, request_id: str, request_revision: int
    ) -> tuple[str, GovernedAnswer] | None:
        row = self._connection.execute(
            "SELECT command_digest, payload FROM governed_answers "
            "WHERE tenant_id = ? AND request_id = ? AND request_revision = ?",
            (tenant_id, request_id, request_revision),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), GovernedAnswer.model_validate_json(bytes(row[1]), strict=True)

    def store(
        self, answer: GovernedAnswer, *, command_digest: str, actor_id: str
    ) -> GovernedAnswer:
        with _transaction(self._connection):
            existing = self.load_delivery(
                answer.tenant_id, answer.request_id, answer.request_revision
            )
            if existing is not None:
                recorded_digest, recorded_answer = existing
                if recorded_digest != command_digest:
                    raise GovernedAnswerConflict(
                        "answer delivery replay conflicts with the recorded command"
                    )
                return recorded_answer
            request = self._requests.load_owned_request(answer.tenant_id, answer.request_id)
            if request.revision != answer.request_revision:
                raise StaleRevisionError("governed answer request revision is stale")
            if request.state is not RequestState.VERIFYING:
                raise GovernedAnswerVerificationError(
                    "governed answer delivery requires a verifying request"
                )
            row = self._connection.execute(
                "INSERT INTO governed_answer_sequences (tenant_id, next_sequence) VALUES (?, 2) "
                "ON CONFLICT(tenant_id) DO UPDATE SET next_sequence = next_sequence + 1 "
                "RETURNING next_sequence - 1",
                (answer.tenant_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("governed answer sequence allocation failed")
            answer_id = f"governed-answer-{digest(answer.tenant_id)[:12]}-{int(row[0]):08d}"
            stored = answer.model_copy(update={"answer_id": answer_id})
            transitioned = self._requests.transition_in_transaction(
                tenant_id=stored.tenant_id,
                request_id=stored.request_id,
                expected_revision=stored.request_revision,
                actor_id=actor_id,
                to_state=RequestState.DELIVERED,
                created_at=stored.delivered_at,
            )
            if transitioned.revision != stored.request_revision + 1:
                raise StaleRevisionError("governed answer resulting revision is stale")
            self._connection.execute(
                "INSERT INTO governed_answers "
                "(answer_id, tenant_id, request_id, request_revision, command_digest, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    stored.answer_id,
                    stored.tenant_id,
                    stored.request_id,
                    stored.request_revision,
                    command_digest,
                    canonical_bytes(stored),
                ),
            )
            return stored

    def read(self, tenant_id: str, answer_id: str) -> GovernedAnswer | None:
        row = self._connection.execute(
            "SELECT payload FROM governed_answers WHERE tenant_id = ? AND answer_id = ?",
            (tenant_id, answer_id),
        ).fetchone()
        if row is None:
            return None
        return GovernedAnswer.model_validate_json(bytes(row[0]), strict=True)

    def list_for_request(self, tenant_id: str, request_id: str) -> tuple[GovernedAnswer, ...]:
        rows = self._connection.execute(
            "SELECT payload FROM governed_answers WHERE tenant_id = ? AND request_id = ? "
            "ORDER BY request_revision",
            (tenant_id, request_id),
        ).fetchall()
        return tuple(GovernedAnswer.model_validate_json(bytes(row[0]), strict=True) for row in rows)


class GovernedAnswerService:
    def __init__(
        self,
        *,
        repository: SQLiteGovernedAnswerRepository,
        admission_reader: AnswerAdmissionReader,
        execution_reader: AnswerExecutionReader,
        plan_reader: AnswerPlanReader,
        authorization_rechecker: AnswerAuthorizationRechecker,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._admissions = admission_reader
        self._executions = execution_reader
        self._plans = plan_reader
        self._authorization = authorization_rechecker
        self._clock = clock or (lambda: datetime.now(UTC))

    def deliver(self, command: DeliverGovernedAnswerCommand) -> GovernedAnswer:
        command_digest = digest(command)
        self._verify_refresh_lineage(command)
        existing = self._repository.load_delivery(
            command.tenant_id, command.request_id, command.request_revision
        )
        if existing is not None:
            recorded_digest, answer = existing
            if recorded_digest != command_digest:
                raise GovernedAnswerConflict(
                    "answer delivery replay conflicts with the recorded command"
                )
            authorized = self._read_for_permission(
                command.tenant_id,
                command.requester_id,
                command.request_id,
                required_permission="view",
            )
            if authorized.answer_id != answer.answer_id:
                raise GovernedAnswerNotVisible("answer delivery authority is not visible")
            return authorized

        admission = self._admissions.read_admission(
            command.tenant_id, command.request_id, command.admission_ref
        )
        if admission is None:
            raise GovernedAnswerNotVisible("answer delivery authority is not visible")
        self._verify_admission(command, admission)

        plan = self._plans.read_plan(command.tenant_id, admission.plan_digest)
        if plan is None:
            raise GovernedAnswerNotVisible("answer delivery authority is not visible")

        receipt = self._executions.read_receipt(
            command.tenant_id,
            command.request_id,
            command.execution_receipt_ref,
            admission.plan_digest,
            plan.product_generation_refs,
        )
        if receipt is None:
            raise GovernedAnswerNotVisible("answer delivery authority is not visible")
        if receipt.outcome != "succeeded" or receipt.result_ref is None:
            raise GovernedAnswerVerificationError(
                "governed answer requires a successful execution receipt"
            )

        snapshot = self._executions.read_result(
            command.tenant_id,
            command.request_id,
            receipt.result_ref,
            admission.plan_digest,
            plan.product_generation_refs,
        )
        if snapshot is None:
            raise GovernedAnswerNotVisible("answer delivery authority is not visible")

        authorization = self._authorization.recheck(
            tenant_id=command.tenant_id,
            request_id=command.request_id,
            requester_id=command.requester_id,
            plan_digest=admission.plan_digest,
            required_permission="view",
        )
        delivered_at = self._clock()
        self._verify_execution(
            command=command,
            admission=admission,
            receipt=receipt,
            snapshot=snapshot,
            plan=plan,
            authorization=authorization,
            delivered_at=delivered_at,
        )

        narrative, narrative_source = _select_narrative(
            command.model_narrative, snapshot, authorization
        )
        answer = GovernedAnswer(
            answer_id="pending",
            tenant_id=command.tenant_id,
            request_id=command.request_id,
            request_revision=command.request_revision,
            restatement=authorization.restatement,
            admission_ref=admission.admission_ref,
            execution_receipt_ref=receipt.receipt_id,
            metric_version_refs=authorization.metric_version_refs,
            product_generation_refs=receipt.product_generation_refs,
            as_of=authorization.as_of,
            freshness_disposition=authorization.freshness_disposition,
            material_quality_limitations=authorization.material_quality_limitations,
            lineage_refs=authorization.lineage_refs,
            narrative=narrative,
            narrative_source=narrative_source,
            result_ref=snapshot.result_ref,
            result_digest=snapshot.result_digest,
            refreshes_answer_ref=command.refreshes_answer_ref,
            delivered_at=delivered_at,
        )
        try:
            return self._repository.store(
                answer, command_digest=command_digest, actor_id=command.actor_id
            )
        except StaleRevisionError:
            raise GovernedAnswerStaleRevision("governed answer request revision is stale") from None

    def _verify_refresh_lineage(self, command: DeliverGovernedAnswerCommand) -> None:
        if command.refreshes_answer_ref is None:
            return
        prior = self._repository.read(command.tenant_id, command.refreshes_answer_ref)
        if prior is None or prior.request_id != command.request_id:
            raise GovernedAnswerNotVisible("answer delivery authority is not visible")

    def read_for_request(
        self, tenant_id: str, requester_id: str, request_id: str
    ) -> GovernedAnswer:
        return self._read_for_permission(
            tenant_id, requester_id, request_id, required_permission="view"
        )

    def read_for_download(
        self, tenant_id: str, requester_id: str, request_id: str
    ) -> GovernedAnswer:
        return self._read_for_permission(
            tenant_id, requester_id, request_id, required_permission="download"
        )

    def _read_for_permission(
        self,
        tenant_id: str,
        requester_id: str,
        request_id: str,
        *,
        required_permission: Literal["view", "download"],
    ) -> GovernedAnswer:
        answers = self._repository.list_for_request(tenant_id, request_id)
        if not answers:
            raise GovernedAnswerNotVisible("answer delivery authority is not visible")
        answer = answers[-1]
        admission = self._admissions.read_admission(tenant_id, request_id, answer.admission_ref)
        if admission is None or (
            admission.tenant_id != answer.tenant_id
            or admission.request_id != answer.request_id
            or admission.verifying_request_revision != answer.request_revision
            or admission.admission_ref != answer.admission_ref
        ):
            raise GovernedAnswerNotVisible("answer delivery authority is not visible")
        authorization = self._authorization.recheck(
            tenant_id=tenant_id,
            request_id=request_id,
            requester_id=requester_id,
            plan_digest=admission.plan_digest,
            required_permission=required_permission,
        )
        if not self._read_authority_matches(
            answer,
            admission,
            authorization,
            requester_id=requester_id,
            read_at=self._clock(),
        ):
            raise GovernedAnswerNotVisible("answer delivery authority is not visible")
        return answer

    @staticmethod
    def _read_authority_matches(
        answer: GovernedAnswer,
        admission: AnswerAdmissionEvidence,
        authorization: AnswerDeliveryAuthorization,
        *,
        requester_id: str,
        read_at: datetime,
    ) -> bool:
        return (
            authorization.tenant_id == answer.tenant_id
            and authorization.request_id == answer.request_id
            and authorization.requester_id == requester_id
            and authorization.request_revision == answer.request_revision
            and authorization.plan_digest == admission.plan_digest
            and authorization.policy_id == admission.policy_id
            and authorization.policy_revision == admission.policy_revision
            and authorization.policy_snapshot_digest == admission.policy_snapshot_digest
            and authorization.entitlement_snapshot_digest == admission.entitlement_snapshot_digest
            and authorization.policy_current
            and authorization.entitlement_current
            and authorization.valid_until > read_at
            and authorization.restatement == answer.restatement
            and authorization.metric_version_refs == answer.metric_version_refs
            and authorization.as_of == answer.as_of
            and authorization.freshness_disposition == answer.freshness_disposition
            and authorization.material_quality_limitations == answer.material_quality_limitations
            and authorization.lineage_refs == answer.lineage_refs
        )

    @staticmethod
    def _verify_admission(
        command: DeliverGovernedAnswerCommand, admission: AnswerAdmissionEvidence
    ) -> None:
        if (
            admission.tenant_id != command.tenant_id
            or admission.request_id != command.request_id
            or admission.verifying_request_revision != command.request_revision
            or admission.admission_ref != command.admission_ref
        ):
            raise GovernedAnswerNotVisible("answer delivery authority is not visible")
        if admission.admission_kind == "reviewed":
            reviewed = admission.reviewed_answer
            if reviewed is None or reviewed.plan_digest != admission.plan_digest:
                raise GovernedAnswerVerificationError(
                    "reviewed answer must cite the admitted plan digest"
                )

    @staticmethod
    def _verify_execution(
        *,
        command: DeliverGovernedAnswerCommand,
        admission: AnswerAdmissionEvidence,
        receipt: AnswerExecutionEvidence,
        snapshot: AnswerResultEvidence,
        plan: AnswerPlanEvidence,
        authorization: AnswerDeliveryAuthorization,
        delivered_at: datetime,
    ) -> None:
        expected_identity = (command.tenant_id, command.request_id)
        if (
            (receipt.tenant_id, receipt.request_id) != expected_identity
            or (snapshot.tenant_id, snapshot.request_id) != expected_identity
            or authorization.tenant_id != command.tenant_id
            or authorization.request_id != command.request_id
            or authorization.requester_id != command.requester_id
            or authorization.request_revision != command.request_revision
        ):
            raise GovernedAnswerNotVisible("answer delivery authority is not visible")
        if not (
            receipt.plan_digest
            == snapshot.plan_digest
            == plan.plan_digest
            == authorization.plan_digest
            == admission.plan_digest
        ):
            raise GovernedAnswerVerificationError("answer plan digest does not match admission")
        if plan.validation_digest != admission.validation_digest:
            raise GovernedAnswerVerificationError(
                "answer plan validation digest does not match admission"
            )
        if plan.tenant_id != command.tenant_id:
            raise GovernedAnswerNotVisible("answer delivery authority is not visible")
        if not (
            receipt.product_generation_refs
            == snapshot.product_generation_refs
            == plan.product_generation_refs
        ):
            raise GovernedAnswerVerificationError("answer product generation does not match plan")
        if _SUPPRESSION_CLAUSE not in plan.statement:
            raise GovernedAnswerVerificationError("answer plan lacks SQL-level suppression")
        if plan.minimum_group_size != authorization.minimum_group_size:
            raise GovernedAnswerVerificationError("answer suppression policy does not match plan")
        if (
            receipt.row_count > plan.ceilings.row_limit
            or receipt.row_count > authorization.row_ceiling
        ):
            raise GovernedAnswerVerificationError("answer result exceeds its row ceiling")
        if receipt.byte_count > authorization.byte_ceiling:
            raise GovernedAnswerVerificationError("answer result exceeds its byte ceiling")
        if snapshot.expires_at <= delivered_at:
            raise GovernedAnswerVerificationError("answer result is no longer readable")
        verify_answer_result_contents(receipt, snapshot)
        if (
            receipt.freshness_observation_ref != authorization.freshness_observation_ref
            or receipt.quality_observation_ref != authorization.quality_observation_ref
        ):
            label = (
                "freshness"
                if receipt.freshness_observation_ref != authorization.freshness_observation_ref
                else "quality"
            )
            raise GovernedAnswerVerificationError(f"answer {label} observation is stale")
        if (
            not authorization.policy_current
            or not authorization.entitlement_current
            or authorization.valid_until <= delivered_at
            or authorization.policy_id != admission.policy_id
            or authorization.policy_revision != admission.policy_revision
            or authorization.policy_snapshot_digest != admission.policy_snapshot_digest
            or authorization.entitlement_snapshot_digest != admission.entitlement_snapshot_digest
        ):
            raise GovernedAnswerVerificationError("answer policy and entitlement are not current")
        reviewed = admission.reviewed_answer
        if reviewed is not None and (
            reviewed.metric_refs != authorization.metric_version_refs
            or reviewed.as_of != authorization.as_of
            or reviewed.freshness_disposition != authorization.freshness_disposition
            or reviewed.material_quality_limitations != authorization.material_quality_limitations
            or reviewed.lineage_refs != authorization.lineage_refs
        ):
            raise GovernedAnswerVerificationError(
                "reviewed answer does not match current freshness and quality evidence"
            )


def verify_answer_result_contents(
    receipt: AnswerExecutionEvidence, snapshot: AnswerResultEvidence
) -> None:
    if snapshot.expires_at <= snapshot.created_at:
        raise GovernedAnswerVerificationError("answer result retention window is invalid")
    if any(len(row) != len(snapshot.columns) for row in snapshot.rows):
        raise GovernedAnswerVerificationError("answer result row does not match its schema")
    if snapshot.row_count != len(snapshot.rows) or receipt.row_count != snapshot.row_count:
        raise GovernedAnswerVerificationError("answer result row count does not match receipt")
    actual_byte_count = len(canonical_bytes(snapshot.rows))
    if snapshot.byte_count != actual_byte_count or receipt.byte_count != snapshot.byte_count:
        raise GovernedAnswerVerificationError("answer result byte count does not match receipt")
    actual_schema_digest = digest(snapshot.columns)
    if (
        snapshot.result_schema_digest != actual_schema_digest
        or receipt.result_schema_digest != snapshot.result_schema_digest
    ):
        raise GovernedAnswerVerificationError("answer result schema digest does not match receipt")
    actual_result_digest = digest({"columns": snapshot.columns, "rows": snapshot.rows})
    if (
        snapshot.result_digest != actual_result_digest
        or receipt.result_digest != actual_result_digest
    ):
        raise GovernedAnswerVerificationError("answer result digest does not match receipt")
    if receipt.result_ref != snapshot.result_ref:
        raise GovernedAnswerVerificationError("answer result reference does not match receipt")


def _select_narrative(
    candidate: str | None,
    snapshot: AnswerResultEvidence,
    authorization: AnswerDeliveryAuthorization,
) -> tuple[str, AnswerNarrativeSource]:
    if candidate is not None and _narrative_is_supported(candidate, snapshot, authorization):
        return candidate, "model"
    noun = "row" if snapshot.row_count == 1 else "rows"
    verb = "is" if snapshot.row_count == 1 else "are"
    return f"{snapshot.row_count} verified result {noun} {verb} available.", "template"


def _narrative_is_supported(
    candidate: str,
    snapshot: AnswerResultEvidence,
    authorization: AnswerDeliveryAuthorization,
) -> bool:
    evidence_parts = [
        *(str(value) for row in snapshot.rows for value in row if value is not None),
        *authorization.approved_narrative_terms,
    ]
    evidence = "\n".join(evidence_parts)
    supported_numbers = set(_NUMBER.findall(evidence))
    if any(token not in supported_numbers for token in _NUMBER.findall(candidate)):
        return False
    evidence_folded = evidence.casefold()
    for match in _NAMED_ENTITY.finditer(candidate):
        entity = match.group(0)
        at_sentence_start = match.start() == 0 or candidate[: match.start()].rstrip().endswith(
            (".", "!", "?")
        )
        if at_sentence_start:
            words = entity.split(maxsplit=1)
            if len(words) == 1:
                continue
            entity = words[1]
        if entity.casefold() not in evidence_folded:
            return False
    return True
