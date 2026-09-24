from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Never

import pytest
from heinzel_console import create_app
from heinzel_console.auth import TrustedActorContext
from heinzel_console.governed_adapters import (
    AnswerDownloadReceipt,
    AnswerDownloadReceiptWriter,
    GovernedWorkspaceIdentity,
    SQLiteAnswerDownloadReceiptRepository,
)
from heinzel_console.governed_backend import GovernedConsoleBackend
from heinzel_console.operation_handles import InMemoryOperationHandleRepository
from heinzel_contract_model import ArtifactReference, canonical_bytes, digest
from heinzel_request_management import (
    GovernedAnswer,
    GovernedAnswerNotVisible,
    InboxRequest,
    RequestState,
    StakeholderAnswerDraft,
)
from heinzel_request_management import RequesterRequestView as ServiceRequesterRequestView
from heinzel_request_management.models import StakeholderQuestion
from heinzel_runtime import (
    AnswerExecutionReceipt,
    AnswerProductGenerationReference,
    AnswerQueryColumn,
    AnswerQueryReference,
    AnswerResultSnapshot,
    SQLiteAnswerResultStore,
)
from starlette.testclient import TestClient

NOW = datetime(2026, 9, 11, 20, 0, tzinfo=UTC)
TENANT = "tenant-alpha"
REQUEST = "request-answer"
DIGEST = "a" * 64


def _context(*, tenant_id: str = TENANT, actor_id: str = "requester-1") -> TrustedActorContext:
    return TrustedActorContext(
        tenant_id=tenant_id,
        actor_id=actor_id,
        roles=("requester",),
        active_role="requester",
        session_id="session-requester",
    )


def _request(*, state: RequestState = RequestState.DELIVERED) -> InboxRequest:
    return InboxRequest(
        title='Revenue / "North"\r\n.csv',
        request_id=REQUEST,
        tenant_id=TENANT,
        requester_id="requester-1",
        payload=StakeholderQuestion(purpose="operations", question="Revenue?"),
        state=state,
        revision=4,
        submitted_at=NOW - timedelta(hours=1),
        updated_at=NOW,
    )


class _Requests:
    def __init__(self, request: InboxRequest) -> None:
        self.request = request

    def list_inbox(self, tenant_id: str) -> tuple[InboxRequest, ...]:
        return (self.request,) if tenant_id == self.request.tenant_id else ()

    def get(self, tenant_id: str, request_id: str) -> InboxRequest:
        if tenant_id != self.request.tenant_id or request_id != self.request.request_id:
            raise KeyError(request_id)
        return self.request


class _Fulfillment:
    def __init__(self, request: InboxRequest) -> None:
        self.request = request

    def requester_view(
        self, *, tenant_id: str, request_id: str, actor_id: str
    ) -> ServiceRequesterRequestView:
        if (
            tenant_id != self.request.tenant_id
            or request_id != self.request.request_id
            or actor_id != self.request.requester_id
        ):
            raise KeyError(request_id)
        answer = StakeholderAnswerDraft(
            answer_text="Revenue is 42.50.",
            as_of=NOW,
            freshness_disposition="current",
            governed_dataset_refs=(
                ArtifactReference(artifact_id="product", version=1, digest=DIGEST),
            ),
            metric_refs=(ArtifactReference(artifact_id="revenue", version=1, digest=DIGEST),),
            lineage_refs=(ArtifactReference(artifact_id="lineage", version=1, digest=DIGEST),),
            material_quality_limitations=(),
            disclosure_classifications=(),
        )
        return ServiceRequesterRequestView(
            request_id=request_id,
            state=self.request.state,
            revision=self.request.revision,
            clarified_outcomes=(),
            own_decisions=(),
            fulfillment_status="closed",
            denial_explanation=None,
            no_valid_plan_explanation=None,
            delivered_answer=answer if self.request.state is RequestState.DELIVERED else None,
            delivery_id="delivery-1" if self.request.state is RequestState.DELIVERED else None,
        )

    def architect_view(self, *, tenant_id: str, request_id: str, actor_id: str) -> Never:
        raise AssertionError("not used")

    def reviewer_view(
        self,
        *,
        tenant_id: str,
        request_id: str,
        actor_id: str,
        authority_ref: str,
    ) -> Never:
        raise AssertionError("not used")


def _generation() -> AnswerProductGenerationReference:
    return AnswerProductGenerationReference(
        product_ref=AnswerQueryReference(artifact_id="product", version=1, digest=DIGEST),
        generation=1,
    )


def _record_result(
    store: SQLiteAnswerResultStore,
    *,
    rows: tuple[tuple[object, ...], ...],
    expires_at: datetime,
) -> None:
    columns = (
        AnswerQueryColumn(name="customer", value_type="string"),
        AnswerQueryColumn(name="revenue", value_type="decimal"),
    )
    typed_rows = tuple((str(row[0]), Decimal(str(row[1]))) for row in rows)
    snapshot = AnswerResultSnapshot(
        result_ref="result-1",
        tenant_id=TENANT,
        request_id=REQUEST,
        plan_digest="b" * 64,
        product_generation_refs=(_generation(),),
        columns=columns,
        rows=typed_rows,
        row_count=len(typed_rows),
        byte_count=len(canonical_bytes(typed_rows)),
        result_schema_digest=digest(columns),
        result_digest=digest({"columns": columns, "rows": typed_rows}),
        created_at=NOW - timedelta(minutes=1),
        expires_at=expires_at,
    )
    receipt = AnswerExecutionReceipt(
        receipt_id="receipt-1",
        tenant_id=TENANT,
        request_id=REQUEST,
        plan_digest=snapshot.plan_digest,
        product_generation_refs=(_generation(),),
        attempt=1,
        started_at=NOW - timedelta(minutes=2),
        completed_at=NOW - timedelta(minutes=1),
        outcome="succeeded",
        row_count=snapshot.row_count,
        byte_count=snapshot.byte_count,
        suppressed_group_count=0,
        result_schema_digest=snapshot.result_schema_digest,
        result_digest=snapshot.result_digest,
        result_ref=snapshot.result_ref,
        freshness_observation_ref="freshness-1",
        quality_observation_ref="quality-1",
    )
    store.record_attempt(input_digest="c" * 64, receipt=receipt, snapshot=snapshot)


class _VerifiedAnswers:
    def __init__(self, store: SQLiteAnswerResultStore) -> None:
        self.store = store
        self.revoked = False
        self.download_revoked = False

    def read_for_request(
        self, *, tenant_id: str, requester_id: str, request_id: str
    ) -> GovernedAnswer:
        if self.revoked or (tenant_id, requester_id, request_id) != (
            TENANT,
            "requester-1",
            REQUEST,
        ):
            raise GovernedAnswerNotVisible("answer is unavailable")
        execution = self.store.load_execution(tenant_id, request_id)
        assert execution is not None
        receipt = execution[1]
        return GovernedAnswer.model_validate(
            {
                "answer_id": "answer-1",
                "tenant_id": tenant_id,
                "request_id": request_id,
                "request_revision": 4,
                "restatement": "Revenue for this period",
                "admission_ref": "admission-1",
                "execution_receipt_ref": receipt.receipt_id,
                "metric_version_refs": (
                    ArtifactReference(artifact_id="revenue", version=1, digest=DIGEST),
                ),
                "product_generation_refs": tuple(
                    item.model_dump() for item in receipt.product_generation_refs
                ),
                "as_of": NOW,
                "freshness_disposition": "current",
                "material_quality_limitations": (),
                "lineage_refs": (),
                "narrative": "Revenue is 42.50.",
                "narrative_source": "template",
                "result_ref": receipt.result_ref,
                "result_digest": receipt.result_digest,
                "delivered_at": NOW,
            }
        )

    def read_for_download(
        self, *, tenant_id: str, requester_id: str, request_id: str
    ) -> GovernedAnswer:
        if self.download_revoked:
            raise GovernedAnswerNotVisible("answer download is unavailable")
        return self.read_for_request(
            tenant_id=tenant_id, requester_id=requester_id, request_id=request_id
        )


def _backend(
    store: SQLiteAnswerResultStore,
    downloads: AnswerDownloadReceiptWriter,
    *,
    request: InboxRequest | None = None,
    verified_answers: _VerifiedAnswers | None = None,
) -> GovernedConsoleBackend:
    selected = request or _request()
    return GovernedConsoleBackend(
        identity=GovernedWorkspaceIdentity(
            tenant_ref=TENANT,
            tenant_display_name="Alpha",
            workspace_ref="workspace-alpha",
            workspace_display_name="Alpha workspace",
        ),
        operation_handles=InMemoryOperationHandleRepository(),
        requests=_Requests(selected),
        fulfillment=_Fulfillment(selected),
        answer_results=store,
        verified_answers=verified_answers or _VerifiedAnswers(store),
        answer_downloads=downloads,
        clock=lambda: NOW,
        answer_download_identifiers=lambda: "download-1",
    )


def test_result_page_preserves_column_order_and_paginates_rows() -> None:
    store = SQLiteAnswerResultStore.in_memory(clock=lambda: NOW)
    _record_result(store, rows=(("A", "1.25"), ("B", "2.50")), expires_at=NOW + timedelta(hours=1))
    downloads = SQLiteAnswerDownloadReceiptRepository(sqlite3.connect(":memory:"))

    first = _backend(store, downloads).get_answer_result(_context(), REQUEST, page_size=1)
    second = _backend(store, downloads).get_answer_result(
        _context(), REQUEST, page_size=1, cursor=first.next_cursor
    )

    assert first.status == "available"
    assert [column.name for column in first.columns] == ["customer", "revenue"]
    assert all(column.allowed_operations == () for column in first.columns)
    assert first.rows == (("A", "1.25"),)
    assert second.rows == (("B", "2.50"),)
    assert second.next_cursor is None
    assert first.technical_details is None


def test_empty_result_is_available_without_a_cursor() -> None:
    store = SQLiteAnswerResultStore.in_memory(clock=lambda: NOW)
    _record_result(store, rows=(), expires_at=NOW + timedelta(hours=1))
    backend = _backend(store, SQLiteAnswerDownloadReceiptRepository(sqlite3.connect(":memory:")))

    page = backend.get_answer_result(_context(), REQUEST)

    assert page.status == "available"
    assert page.row_count == 0
    assert page.rows == ()
    assert page.next_cursor is None


def test_expired_result_exposes_no_rows_or_result_reference() -> None:
    store = SQLiteAnswerResultStore.in_memory(clock=lambda: NOW)
    _record_result(store, rows=(("A", "1.25"),), expires_at=NOW)
    backend = _backend(store, SQLiteAnswerDownloadReceiptRepository(sqlite3.connect(":memory:")))

    page = backend.get_answer_result(_context(), REQUEST)

    assert page.status == "expired"
    assert page.rows == ()
    assert page.columns == ()
    assert page.technical_details is None


def test_cross_tenant_and_wrong_requester_are_non_enumerating() -> None:
    from heinzel_console.errors import ConsoleNotFound

    store = SQLiteAnswerResultStore.in_memory(clock=lambda: NOW)
    _record_result(store, rows=(("A", "1.25"),), expires_at=NOW + timedelta(hours=1))
    backend = _backend(store, SQLiteAnswerDownloadReceiptRepository(sqlite3.connect(":memory:")))

    with pytest.raises(ConsoleNotFound):
        backend.get_answer_result(_context(tenant_id="tenant-other"), REQUEST)
    with pytest.raises(ConsoleNotFound):
        backend.get_answer_result(_context(actor_id="requester-other"), REQUEST)


def test_csv_download_uses_safe_filename_and_records_durable_receipt() -> None:
    store = SQLiteAnswerResultStore.in_memory(clock=lambda: NOW)
    _record_result(store, rows=(("A", "1.25"),), expires_at=NOW + timedelta(hours=1))
    downloads = SQLiteAnswerDownloadReceiptRepository(sqlite3.connect(":memory:"))
    backend = _backend(store, downloads)

    content = backend.download_answer_result(_context(), REQUEST)

    assert content.filename == "revenue-north-csv.csv"
    assert content.media_type == "text/csv"
    assert content.body == b"customer,revenue\r\nA,1.25\r\n"
    receipts = downloads.list_for_request(TENANT, REQUEST)
    assert len(receipts) == 1
    assert receipts[0].result_digest == content.result_digest


def test_csv_download_requires_current_download_permission_in_addition_to_view() -> None:
    from heinzel_console.errors import ConsoleNotFound

    store = SQLiteAnswerResultStore.in_memory(clock=lambda: NOW)
    _record_result(store, rows=(("A", "1.25"),), expires_at=NOW + timedelta(hours=1))
    authority = _VerifiedAnswers(store)
    authority.download_revoked = True
    downloads = SQLiteAnswerDownloadReceiptRepository(sqlite3.connect(":memory:"))
    backend = _backend(store, downloads, verified_answers=authority)

    assert backend.get_answer_result(_context(), REQUEST).status == "available"
    with pytest.raises(ConsoleNotFound):
        backend.download_answer_result(_context(), REQUEST)
    assert downloads.list_for_request(TENANT, REQUEST) == ()


def test_csv_download_fails_closed_when_receipt_cannot_be_recorded() -> None:
    from heinzel_console.errors import ConsoleUnavailable

    class _FailingDownloads:
        def record(self, receipt: AnswerDownloadReceipt) -> None:
            raise sqlite3.OperationalError("disk unavailable")

    store = SQLiteAnswerResultStore.in_memory(clock=lambda: NOW)
    _record_result(store, rows=(("A", "1.25"),), expires_at=NOW + timedelta(hours=1))
    backend = _backend(store, _FailingDownloads())

    with pytest.raises(ConsoleUnavailable, match="download"):
        backend.download_answer_result(_context(), REQUEST)


def test_result_routes_return_the_page_envelope_and_stream_csv() -> None:
    store = SQLiteAnswerResultStore(
        sqlite3.connect(":memory:", check_same_thread=False), clock=lambda: NOW
    )
    _record_result(store, rows=(("A", "1.25"),), expires_at=NOW + timedelta(hours=1))
    downloads = SQLiteAnswerDownloadReceiptRepository(
        sqlite3.connect(":memory:", check_same_thread=False)
    )
    app = create_app(
        backend=_backend(store, downloads),
        context_provider=lambda _: _context(),
    )

    with TestClient(app) as client:
        page = client.get(f"/api/v1/requests/{REQUEST}/result?page_size=1")
        download = client.get(f"/api/v1/requests/{REQUEST}/result.csv")

    assert page.status_code == 200
    assert page.json()["data"]["rows"] == [["A", "1.25"]]
    assert download.status_code == 200
    assert download.headers["content-type"] == "text/csv; charset=utf-8"
    assert download.headers["content-disposition"] == (
        'attachment; filename="revenue-north-csv.csv"'
    )
    assert download.content == b"customer,revenue\r\nA,1.25\r\n"


@pytest.mark.parametrize("download", [False, True])
def test_revoked_result_access_is_denied_on_every_read(download: bool) -> None:
    from heinzel_console.errors import ConsoleNotFound

    store = SQLiteAnswerResultStore.in_memory(clock=lambda: NOW)
    _record_result(store, rows=(("A", "1.25"),), expires_at=NOW + timedelta(hours=1))
    authority = _VerifiedAnswers(store)
    downloads = SQLiteAnswerDownloadReceiptRepository(sqlite3.connect(":memory:"))
    backend = _backend(store, downloads, verified_answers=authority)
    assert backend.get_answer_result(_context(), REQUEST).status == "available"

    authority.revoked = True

    with pytest.raises(ConsoleNotFound):
        if download:
            backend.download_answer_result(_context(), REQUEST)
        else:
            backend.get_answer_result(_context(), REQUEST)
    assert downloads.list_for_request(TENANT, REQUEST) == ()


def test_requester_result_link_disappears_when_authority_is_revoked() -> None:
    store = SQLiteAnswerResultStore.in_memory(clock=lambda: NOW)
    _record_result(store, rows=(("A", "1.25"),), expires_at=NOW + timedelta(hours=1))
    authority = _VerifiedAnswers(store)
    backend = _backend(
        store,
        SQLiteAnswerDownloadReceiptRepository(sqlite3.connect(":memory:")),
        verified_answers=authority,
    )

    assert backend.get_requester_requests(_context())[0].result_page_available

    authority.revoked = True

    assert not backend.get_requester_requests(_context())[0].result_page_available


@pytest.mark.parametrize("customer", ["=1+1", "+1+1", "-1+1", "@SUM(A1)", "\t=1+1", "  =1+1"])
def test_csv_treats_formula_like_text_as_data_and_preserves_negative_numbers(customer: str) -> None:
    import csv
    import io

    store = SQLiteAnswerResultStore.in_memory(clock=lambda: NOW)
    _record_result(store, rows=((customer, "-1.25"),), expires_at=NOW + timedelta(hours=1))
    backend = _backend(store, SQLiteAnswerDownloadReceiptRepository(sqlite3.connect(":memory:")))

    download = backend.download_answer_result(_context(), REQUEST)
    rows = tuple(csv.reader(io.StringIO(download.body.decode())))

    assert rows[1] == [f"'{customer}", "-1.25"]
    assert backend.get_answer_result(_context(), REQUEST).rows == ((customer, "-1.25"),)
