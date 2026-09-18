from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Literal

from heinzel_contract_model import canonical_bytes, digest
from heinzel_provider_sdk import (
    AcquisitionBoundary,
    AcquisitionCeilingExceeded,
    AcquisitionIntent,
    AcquisitionObjectObservation,
    AcquisitionObjectSchema,
    AcquisitionProviderError,
    AcquisitionRecord,
    AcquisitionSession,
    AcquisitionSessionIncomplete,
    AcquisitionSourceObservation,
    ColumnObservation,
    CompletedAcquisition,
    ProviderObservation,
    SourceObservationRequest,
)
from heinzel_provider_sdk.errors import (
    AcquisitionProviderErrorClassification,
    AcquisitionProviderReasonCode,
)

from .client import StripeClient
from .codecs import StripeCodecError, normalize_stripe_event, normalize_stripe_object
from .models import StripeEventCursor
from .settings import STRIPE_API_VERSION, StripeObjectDeclaration, StripeSettings

type _PrivateBoundaryWriter = Callable[[str, str, bytes], None]
type _PrivateBoundaryReferenceFactory = Callable[[str, str], str]
type _LogicalObjectRef = Literal["charges", "customers", "invoices", "refunds"]

_SNAPSHOT_CURSOR_VERSION = "stripe-event-v1"
_BOOTSTRAP_EVENT_ID = "evt_heinzel_internal_bootstrap_v1"
_UNSET_OVERLAP = object()
_OBJECT_REFS: tuple[_LogicalObjectRef, ...] = (
    "charges",
    "customers",
    "invoices",
    "refunds",
)
_OBJECT_REF_BY_KIND: dict[str, _LogicalObjectRef] = {
    "charge": "charges",
    "customer": "customers",
    "invoice": "invoices",
    "refund": "refunds",
}
_EVENT_PREFIX_BY_REF: dict[_LogicalObjectRef, str] = {
    "charges": "charge.",
    "customers": "customer.",
    "invoices": "invoice.",
    "refunds": "refund.",
}


def _provider_error(reason_code: AcquisitionProviderReasonCode) -> AcquisitionProviderError:
    classifications: dict[AcquisitionProviderReasonCode, AcquisitionProviderErrorClassification] = {
        "integrity_failure": "integrity_failure",
        "invalid_provider_response": "invalid_provider_response",
        "permanent_configuration": "permanent_configuration",
        "stripe_event_cursor_expired": "resynchronization_required",
        "stripe_event_overlap_gap": "resynchronization_required",
    }
    classification = classifications.get(reason_code)
    if classification is None:
        return AcquisitionProviderError(
            provider_kind="stripe",
            classification="integrity_failure",
            reason_code="integrity_failure",
        )
    return AcquisitionProviderError(
        provider_kind="stripe",
        classification=classification,
        reason_code=reason_code,
    )


class StripeAcquisitionProvider:
    def __init__(
        self,
        settings: StripeSettings,
        *,
        client: StripeClient,
        clock: Callable[[], datetime] | None = None,
        private_boundary_reference_factory: _PrivateBoundaryReferenceFactory,
        private_boundary_writer: _PrivateBoundaryWriter,
        event_overlap_window: timedelta | object | None = _UNSET_OVERLAP,
    ) -> None:
        if event_overlap_window is not _UNSET_OVERLAP and (
            not isinstance(event_overlap_window, timedelta) or event_overlap_window <= timedelta(0)
        ):
            raise ValueError("Stripe event overlap window must be positive")
        self._settings = settings
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))
        self._private_boundary_reference_factory = private_boundary_reference_factory
        self._private_boundary_writer = private_boundary_writer
        self._event_overlap_window = (
            None if event_overlap_window is _UNSET_OVERLAP else event_overlap_window
        )
        self._declarations = {
            declaration.logical_object_ref: declaration for declaration in settings.objects
        }

    def observe_source(self, request: SourceObservationRequest) -> AcquisitionSourceObservation:
        try:
            return self._observe_source(request)
        except AcquisitionProviderError:
            raise
        except Exception:
            raise _provider_error("integrity_failure") from None

    def _observe_source(self, request: SourceObservationRequest) -> AcquisitionSourceObservation:
        declarations = self._resolve_declarations(request.object_refs)
        observed_at = self._utc_now()
        upper_epoch = int(observed_at.timestamp())
        for declaration in declarations:
            self._probe_pages(
                self._client.iter_object_pages(
                    object_kind=declaration.object_kind,
                    created_lte=upper_epoch,
                )
            )
        observed_event_types = tuple(
            event_type
            for event_type in self._settings.event_types
            if any(
                event_type.startswith(_EVENT_PREFIX_BY_REF[_logical_object_ref(declaration)])
                for declaration in declarations
            )
        )
        self._probe_pages(
            self._client.iter_event_pages(
                created_gte=upper_epoch,
                created_lte=upper_epoch,
                event_types=observed_event_types,
            )
        )
        observations = tuple(
            AcquisitionObjectObservation(
                logical_object_ref=declaration.logical_object_ref,
                provider_observation=ProviderObservation(
                    provider="stripe",
                    connection_handle=self._settings.connection_handle,
                    object_identity=digest(
                        {
                            "connection_handle": self._settings.connection_handle,
                            "account_mode": self._settings.account_mode,
                            "api_version": self._settings.api_version,
                            "supported_creation_versions": (
                                self._settings.supported_creation_versions
                            ),
                            "logical_object_ref": declaration.logical_object_ref,
                            "object_kind": declaration.object_kind,
                        }
                    ),
                    object_kind="unknown",
                    schema_digest=digest(declaration.normalized_fields),
                    columns=tuple(
                        ColumnObservation(
                            name=field.name,
                            type_name=field.value_type.upper(),
                            nullable=field.nullable,
                        )
                        for field in declaration.normalized_fields
                    ),
                    key_name=declaration.normalized_fields[0].name,
                    key_type="STRING",
                    key_nullable=False,
                    key_constraint="unique",
                    stable_key_order=True,
                    read_only=True,
                    capabilities=("incremental", "reconciliation", "snapshot"),
                    observed_at=observed_at,
                    snapshot_semantics="snapshot",
                    commit_ledger_object_kind=None,
                    commit_ledger_columns=None,
                    commit_ledger_key_name=None,
                    commit_ledger_key_constraint=None,
                    evidence_safe=True,
                ),
            )
            for declaration in declarations
        )
        return AcquisitionSourceObservation(
            tenant_id=request.tenant_id,
            source_binding_ref=request.source_binding_ref,
            provider_kind="stripe",
            object_observations=observations,
        )

    @staticmethod
    def _probe_pages(iterator: Iterator[tuple[Mapping[str, object], ...]]) -> None:
        page_received = False
        failure: AcquisitionProviderError | None = None
        try:
            next(iterator)
            page_received = True
        except StopIteration:
            pass
        except AcquisitionProviderError as error:
            failure = error
        except Exception:
            failure = _provider_error("integrity_failure")
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
        if failure is not None:
            raise failure from None
        if not page_received:
            raise _provider_error("invalid_provider_response")

    def open_acquisition(
        self,
        intent: AcquisitionIntent,
        schemas: tuple[AcquisitionObjectSchema, ...],
        private_cursor: bytes | None,
    ) -> AcquisitionSession:
        if intent.acquisition_mode == "incremental":
            return self._open_event_acquisition(intent, schemas, private_cursor)
        reconciliation = intent.acquisition_mode == "reconciliation"
        valid_snapshot = (
            intent.acquisition_mode == "snapshot"
            and intent.prior_checkpoint_revision == 0
            and private_cursor is None
        )
        valid_reconciliation = (
            reconciliation
            and intent.prior_checkpoint_revision > 0
            and private_cursor is not None
            and intent.prior_checkpoint_digest == hashlib.sha256(private_cursor).hexdigest()
        )
        if not valid_snapshot and not valid_reconciliation:
            raise _provider_error("permanent_configuration")
        if reconciliation:
            if intent.object_refs != _OBJECT_REFS:
                raise _provider_error("permanent_configuration")
            if private_cursor is None:
                raise _provider_error("permanent_configuration")
            try:
                reconciliation_cursor = StripeEventCursor.from_payload(private_cursor)
            except (TypeError, ValueError):
                raise _provider_error("integrity_failure") from None
            if reconciliation_cursor.api_version_set_digest != digest(
                self._settings.supported_creation_versions
            ):
                raise _provider_error("integrity_failure")
        declarations = self._resolve_declarations(intent.object_refs)
        self._require_schemas(declarations, schemas)
        return self._open_object_snapshot(
            intent,
            declarations,
            schemas,
            private_cursor=private_cursor,
        )

    def _open_event_acquisition(
        self,
        intent: AcquisitionIntent,
        schemas: tuple[AcquisitionObjectSchema, ...],
        private_cursor: bytes | None,
    ) -> _StripeEventSession:
        valid_incremental = (
            intent.prior_checkpoint_revision > 0
            and private_cursor is not None
            and intent.prior_checkpoint_digest == hashlib.sha256(private_cursor).hexdigest()
        )
        if not valid_incremental:
            raise _provider_error("permanent_configuration")
        if private_cursor is None:
            raise _provider_error("permanent_configuration")
        if not isinstance(self._event_overlap_window, timedelta):
            raise _provider_error("permanent_configuration")
        declarations = self._resolve_declarations(intent.object_refs)
        self._require_schemas(declarations, schemas, incremental=True)
        try:
            prior_cursor = StripeEventCursor.from_payload(private_cursor)
        except ValueError:
            raise _provider_error("integrity_failure") from None
        expected_version_digest = digest(self._settings.supported_creation_versions)
        if prior_cursor.api_version_set_digest != expected_version_digest:
            raise _provider_error("permanent_configuration")
        opened_at = self._utc_now()
        if prior_cursor.last_event_created < opened_at - timedelta(days=30):
            raise _provider_error("stripe_event_cursor_expired")
        return _StripeEventSession(
            client=self._client,
            settings=self._settings,
            intent=intent,
            schemas=schemas,
            prior_cursor=prior_cursor,
            overlap_window=self._event_overlap_window,
            opened_at=opened_at,
            clock=self._clock,
            private_boundary_reference_factory=self._private_boundary_reference_factory,
            private_boundary_writer=self._private_boundary_writer,
        )

    def _open_object_snapshot(
        self,
        intent: AcquisitionIntent,
        declarations: tuple[StripeObjectDeclaration, ...],
        schemas: tuple[AcquisitionObjectSchema, ...],
        *,
        private_cursor: bytes | None,
    ) -> _StripeAcquisitionSession:
        opened_at = self._utc_now()
        upper_epoch = int(opened_at.timestamp())
        prepared_records: list[AcquisitionRecord] = []
        prepared_counts: list[int] = []
        private_payloads: list[tuple[str, bytes]] = []
        total_count = 0
        try:
            for declaration, _schema in zip(declarations, schemas, strict=True):
                records = self._read_object_pages(
                    declaration,
                    upper_epoch=upper_epoch,
                    current_total=total_count,
                    record_ceiling=intent.record_ceiling,
                )
                total_count += len(records)
                prepared_records.extend(records)
                prepared_counts.append(len(records))
                private_reference = self._private_boundary_reference_factory(
                    intent.tenant_id,
                    declaration.logical_object_ref,
                )
                private_payloads.append(
                    (
                        private_reference,
                        canonical_bytes(
                            {
                                "logical_object_ref": declaration.logical_object_ref,
                                "upper_created": upper_epoch,
                                "record_count": len(records),
                                "record_set_digest": digest(records),
                            }
                        ),
                    )
                )
        except (AcquisitionCeilingExceeded, AcquisitionProviderError):
            raise
        except StripeCodecError:
            raise _provider_error("invalid_provider_response") from None
        except Exception:
            raise _provider_error("integrity_failure") from None

        candidate_cursor_payload = (
            private_cursor
            if private_cursor is not None
            else StripeEventCursor(
                last_event_created=opened_at,
                last_event_id=_BOOTSTRAP_EVENT_ID,
                api_version_set_digest=digest(self._settings.supported_creation_versions),
            ).to_payload()
        )
        candidate_digest = hashlib.sha256(candidate_cursor_payload).hexdigest()
        boundaries = tuple(
            AcquisitionBoundary(
                logical_object_ref=declaration.logical_object_ref,
                acquisition_mode=intent.acquisition_mode,
                schema_digest=schema.schema_digest,
                lower_cursor_digest=(
                    intent.prior_checkpoint_digest
                    if intent.acquisition_mode == "reconciliation"
                    else None
                ),
                upper_cursor_digest=candidate_digest,
                query_shape_digest=digest(
                    {
                        "logical_object_ref": declaration.logical_object_ref,
                        "object_kind": declaration.object_kind,
                        "created_operator": "<=",
                        "fields": declaration.source_field_names,
                        "pagination": "starting_after",
                        "order_by": ("created", declaration.normalized_fields[0].name),
                    }
                ),
                snapshot_identity_digest=None,
                key_range_digest=None,
                private_boundary_ref=private_payloads[position][0],
                record_count=prepared_counts[position],
                opened_at=opened_at,
                closed_at=opened_at,
            )
            for position, (declaration, schema) in enumerate(
                zip(declarations, schemas, strict=True)
            )
        )
        try:
            for private_reference, payload in private_payloads:
                self._private_boundary_writer(intent.tenant_id, private_reference, payload)
        except Exception:
            raise _provider_error("integrity_failure") from None
        return _StripeAcquisitionSession(
            records=tuple(prepared_records),
            boundaries=boundaries,
            clock=self._clock,
            cursor_version=_SNAPSHOT_CURSOR_VERSION,
            candidate_cursor_payload=candidate_cursor_payload,
        )

    def _read_object_pages(
        self,
        declaration: StripeObjectDeclaration,
        *,
        upper_epoch: int,
        current_total: int,
        record_ceiling: int,
    ) -> tuple[AcquisitionRecord, ...]:
        iterator = self._client.iter_object_pages(
            object_kind=declaration.object_kind,
            created_lte=upper_epoch,
        )
        seen_payloads: dict[str, bytes] = {}
        records: dict[str, AcquisitionRecord] = {}
        try:
            for page in iterator:
                if len(page) > 100:
                    raise _provider_error("invalid_provider_response")
                for untrusted_payload in page:
                    object_id = untrusted_payload.get("id")
                    created = untrusted_payload.get("created")
                    if (
                        not isinstance(object_id, str)
                        or not object_id
                        or type(created) is not int
                        or created < 0
                        or created > upper_epoch
                    ):
                        raise _provider_error("invalid_provider_response")
                    payload_bytes = canonical_bytes(untrusted_payload)
                    prior_payload = seen_payloads.get(object_id)
                    if prior_payload is not None:
                        if prior_payload != payload_bytes:
                            raise _provider_error("integrity_failure")
                        continue
                    seen_payloads[object_id] = payload_bytes
                    records[object_id] = normalize_stripe_object(
                        untrusted_payload,
                        logical_object_ref=_logical_object_ref(declaration),
                        expected_livemode=self._settings.account_mode == "live",
                        creation_version=STRIPE_API_VERSION,
                        customer_identity_metadata_key=(
                            self._settings.customer_identity_metadata_key
                        ),
                    )
                    if current_total + len(records) > record_ceiling:
                        raise AcquisitionCeilingExceeded(
                            logical_object_ref=declaration.logical_object_ref,
                            limit_kind="records",
                            ceiling=record_ceiling,
                        )
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
        return tuple(
            sorted(
                records.values(),
                key=lambda record: (
                    record.source_created_at,
                    str(record.fields[0].value),
                ),
            )
        )

    def _resolve_declarations(
        self,
        object_refs: tuple[str, ...],
    ) -> tuple[StripeObjectDeclaration, ...]:
        if object_refs != tuple(sorted(set(object_refs))):
            raise _provider_error("permanent_configuration")
        try:
            declarations = tuple(self._declarations[object_ref] for object_ref in object_refs)
        except KeyError:
            raise _provider_error("permanent_configuration") from None
        return declarations

    @staticmethod
    def _require_schemas(
        declarations: tuple[StripeObjectDeclaration, ...],
        schemas: tuple[AcquisitionObjectSchema, ...],
        *,
        incremental: bool = False,
    ) -> None:
        if tuple(schema.logical_object_ref for schema in schemas) != tuple(
            declaration.logical_object_ref for declaration in declarations
        ):
            raise _provider_error("permanent_configuration")
        for declaration, schema in zip(declarations, schemas, strict=True):
            if (
                schema.schema_digest != digest(declaration.normalized_fields)
                or schema.fields != declaration.normalized_fields
                or schema.record_key_fields != (declaration.normalized_fields[0].name,)
                or schema.source_updated_at_field != ("created" if incremental else None)
                or schema.operation_semantics != "upsert_only"
            ):
                raise _provider_error("permanent_configuration")

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise _provider_error("integrity_failure")
        return value.astimezone(UTC)


def _logical_object_ref(declaration: StripeObjectDeclaration) -> _LogicalObjectRef:
    logical_object_ref = _OBJECT_REF_BY_KIND[declaration.object_kind]
    if declaration.logical_object_ref != logical_object_ref:
        raise _provider_error("permanent_configuration")
    return logical_object_ref


def _logical_ref(value: str) -> _LogicalObjectRef:
    if value == "charges":
        return "charges"
    if value == "customers":
        return "customers"
    if value == "invoices":
        return "invoices"
    if value == "refunds":
        return "refunds"
    raise _provider_error("permanent_configuration")


class _StripeAcquisitionSession:
    def __init__(
        self,
        *,
        records: tuple[AcquisitionRecord, ...],
        boundaries: tuple[AcquisitionBoundary, ...],
        clock: Callable[[], datetime],
        cursor_version: str,
        candidate_cursor_payload: bytes,
    ) -> None:
        self._records = records
        self._boundaries = boundaries
        self._clock = clock
        self._cursor_version = cursor_version
        self._candidate_cursor_payload = candidate_cursor_payload
        self._position = 0
        self._completed: CompletedAcquisition | None = None
        self._aborted = False

    def __iter__(self) -> _StripeAcquisitionSession:
        return self

    def __next__(self) -> AcquisitionRecord:
        if self._aborted:
            raise StopIteration
        if self._position < len(self._records):
            record = self._records[self._position]
            self._position += 1
            return record
        if self._completed is None:
            closed_at = self._clock()
            if closed_at.tzinfo is None or closed_at.utcoffset() != timedelta(0):
                self._aborted = True
                raise _provider_error("integrity_failure")
            self._completed = CompletedAcquisition(
                boundaries=tuple(
                    boundary.model_copy(update={"closed_at": closed_at.astimezone(UTC)})
                    for boundary in self._boundaries
                ),
                cursor_version=self._cursor_version,
                candidate_cursor_payload=self._candidate_cursor_payload,
            )
        raise StopIteration

    def complete(self) -> CompletedAcquisition:
        if self._completed is None:
            raise AcquisitionSessionIncomplete()
        return self._completed

    def abort(self) -> None:
        if self._completed is None:
            self._aborted = True


class _StripeEventSession:
    def __init__(
        self,
        *,
        client: StripeClient,
        settings: StripeSettings,
        intent: AcquisitionIntent,
        schemas: tuple[AcquisitionObjectSchema, ...],
        prior_cursor: StripeEventCursor,
        overlap_window: timedelta,
        opened_at: datetime,
        clock: Callable[[], datetime],
        private_boundary_reference_factory: _PrivateBoundaryReferenceFactory,
        private_boundary_writer: _PrivateBoundaryWriter,
    ) -> None:
        self._client = client
        self._settings = settings
        self._intent = intent
        self._schemas = schemas
        self._prior_cursor = prior_cursor
        self._overlap_window = overlap_window
        self._opened_at = opened_at
        self._clock = clock
        self._private_boundary_reference_factory = private_boundary_reference_factory
        self._private_boundary_writer = private_boundary_writer
        self._admitted_object_refs = frozenset(intent.object_refs)
        self._bootstrap = prior_cursor.last_event_id == _BOOTSTRAP_EVENT_ID
        self._event_types = tuple(
            event_type
            for event_type in settings.event_types
            if any(
                event_type.startswith(_EVENT_PREFIX_BY_REF[_logical_ref(object_ref)])
                for object_ref in intent.object_refs
            )
        )
        if not self._event_types:
            raise _provider_error("permanent_configuration")
        self._records: tuple[AcquisitionRecord, ...] | None = None
        self._boundaries: tuple[AcquisitionBoundary, ...] | None = None
        self._candidate_cursor_payload: bytes | None = None
        self._position = 0
        self._completed: CompletedAcquisition | None = None
        self._aborted = False

    def __iter__(self) -> _StripeEventSession:
        return self

    def __next__(self) -> AcquisitionRecord:
        if self._aborted:
            raise StopIteration
        if self._records is None:
            try:
                self._prepare()
            except (AcquisitionCeilingExceeded, AcquisitionProviderError):
                self._aborted = True
                raise
            except StripeCodecError as error:
                self._aborted = True
                if error.reason_code == "unsupported_creation_version":
                    raise _provider_error("permanent_configuration") from None
                raise _provider_error("invalid_provider_response") from None
            except Exception:
                self._aborted = True
                raise _provider_error("integrity_failure") from None
        if self._records is None:
            self._aborted = True
            raise _provider_error("integrity_failure")
        if self._position < len(self._records):
            record = self._records[self._position]
            self._position += 1
            return record
        self._finish()
        raise StopIteration

    def complete(self) -> CompletedAcquisition:
        if self._completed is None:
            raise AcquisitionSessionIncomplete()
        return self._completed

    def abort(self) -> None:
        if self._completed is None:
            self._aborted = True

    def _prepare(self) -> None:
        lower_created = self._prior_cursor.last_event_created - self._overlap_window
        lower_epoch = int(lower_created.timestamp())
        upper_epoch = int(self._opened_at.timestamp())
        iterator = self._client.iter_event_pages(
            created_gte=lower_epoch,
            created_lte=upper_epoch,
            event_types=self._event_types,
        )
        payloads_by_id: dict[str, bytes] = {}
        events_by_id: dict[str, Mapping[str, object]] = {}
        normalized_by_id: dict[str, AcquisitionRecord] = {}
        eligible_record_keys: set[tuple[str, str]] = set()
        # Continuity is proven from the whole retrieved window, not from the first
        # page. Stripe lists newest first and `starting_after` walks toward older
        # events, so the prior cursor's event is the oldest boundary of the overlap
        # and arrives on the LAST page whenever the window spans more than one.
        anchor_created = int(self._prior_cursor.last_event_created.timestamp())
        anchor_seen = self._bootstrap
        try:
            for page in iterator:
                if len(page) > 100:
                    raise _provider_error("invalid_provider_response")
                for event in page:
                    event_id = event.get("id")
                    created = event.get("created")
                    if not isinstance(event_id, str) or not event_id or type(created) is not int:
                        raise _provider_error("invalid_provider_response")
                    if created < lower_epoch:
                        raise _provider_error("stripe_event_overlap_gap")
                    if created > upper_epoch:
                        raise _provider_error("invalid_provider_response")
                    if (
                        not anchor_seen
                        and event_id == self._prior_cursor.last_event_id
                        and created == anchor_created
                    ):
                        anchor_seen = True
                    payload_bytes = canonical_bytes(event)
                    prior_payload = payloads_by_id.get(event_id)
                    if prior_payload is not None:
                        if prior_payload != payload_bytes:
                            raise _provider_error("integrity_failure")
                        continue
                    payloads_by_id[event_id] = payload_bytes
                    events_by_id[event_id] = event
                    record = normalize_stripe_event(
                        event,
                        expected_livemode=self._settings.account_mode == "live",
                        customer_identity_metadata_key=(
                            self._settings.customer_identity_metadata_key
                        ),
                    )
                    if record.logical_object_ref not in self._admitted_object_refs:
                        raise _provider_error("integrity_failure")
                    normalized_by_id[event_id] = record
                    event_order = (created, event_id)
                    if self._bootstrap or event_order > (
                        int(self._prior_cursor.last_event_created.timestamp()),
                        self._prior_cursor.last_event_id,
                    ):
                        eligible_record_keys.add((record.logical_object_ref, record.record_key))
                        if len(eligible_record_keys) > self._intent.record_ceiling:
                            raise AcquisitionCeilingExceeded(
                                logical_object_ref=record.logical_object_ref,
                                limit_kind="records",
                                ceiling=self._intent.record_ceiling,
                            )
            if not anchor_seen:
                # The window did not contain the event the cursor names, so this run
                # cannot prove it saw everything between the two cursors.
                raise _provider_error("stripe_event_overlap_gap")
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()

        ordered_events = tuple(
            sorted(
                events_by_id.values(),
                key=lambda event: (_event_created(event), _event_id(event)),
            )
        )
        prior_order = (
            int(self._prior_cursor.last_event_created.timestamp()),
            self._prior_cursor.last_event_id,
        )
        new_events = tuple(
            event
            for event in ordered_events
            if self._bootstrap or (_event_created(event), _event_id(event)) > prior_order
        )
        normalized: list[tuple[tuple[int, str], AcquisitionRecord]] = []
        for event in new_events:
            record = normalized_by_id[_event_id(event)]
            normalized.append(((_event_created(event), _event_id(event)), record))
        latest_by_record: dict[tuple[str, str], tuple[tuple[int, str], AcquisitionRecord]] = {}
        for event_order, record in normalized:
            latest_by_record[(record.logical_object_ref, record.record_key)] = (
                event_order,
                record,
            )
        self._records = tuple(
            item[1]
            for item in sorted(
                latest_by_record.values(),
                key=lambda item: (
                    item[1].logical_object_ref,
                    item[1].source_updated_at,
                    str(item[1].fields[0].value),
                ),
            )
        )

        candidate_cursor = self._prior_cursor
        cursor_events = (
            tuple(
                event
                for event in new_events
                if _event_created(event) > int(self._prior_cursor.last_event_created.timestamp())
            )
            if self._bootstrap
            else new_events
        )
        if cursor_events:
            greatest = cursor_events[-1]
            candidate_cursor = StripeEventCursor(
                last_event_created=datetime.fromtimestamp(_event_created(greatest), tz=UTC),
                last_event_id=_event_id(greatest),
                api_version_set_digest=digest(self._settings.supported_creation_versions),
            )
        self._candidate_cursor_payload = candidate_cursor.to_payload()
        candidate_digest = hashlib.sha256(self._candidate_cursor_payload).hexdigest()
        counts = {
            object_ref: sum(record.logical_object_ref == object_ref for record in self._records)
            for object_ref in self._intent.object_refs
        }
        boundaries: list[AcquisitionBoundary] = []
        private_payloads: list[tuple[str, bytes]] = []
        for schema in self._schemas:
            private_reference = self._private_boundary_reference_factory(
                self._intent.tenant_id,
                schema.logical_object_ref,
            )
            private_payloads.append(
                (
                    private_reference,
                    canonical_bytes(
                        {
                            "logical_object_ref": schema.logical_object_ref,
                            "overlap_start": lower_created,
                            "upper_event_created": self._opened_at,
                            "prior_event_created": self._prior_cursor.last_event_created,
                            "prior_event_id": self._prior_cursor.last_event_id,
                            "candidate_cursor_digest": candidate_digest,
                            "record_count": counts[schema.logical_object_ref],
                        }
                    ),
                )
            )
            boundaries.append(
                AcquisitionBoundary(
                    logical_object_ref=schema.logical_object_ref,
                    acquisition_mode="incremental",
                    schema_digest=schema.schema_digest,
                    lower_cursor_digest=self._intent.prior_checkpoint_digest,
                    upper_cursor_digest=candidate_digest,
                    query_shape_digest=digest(
                        {
                            "event_types": self._event_types,
                            "created_lower_operator": ">=",
                            "created_upper_operator": "<=",
                            "pagination": "starting_after",
                            "order_by": ("created", "event_id"),
                        }
                    ),
                    snapshot_identity_digest=None,
                    key_range_digest=None,
                    private_boundary_ref=private_reference,
                    record_count=counts[schema.logical_object_ref],
                    opened_at=self._opened_at,
                    closed_at=self._opened_at,
                )
            )
        for private_reference, payload in private_payloads:
            self._private_boundary_writer(
                self._intent.tenant_id,
                private_reference,
                payload,
            )
        self._boundaries = tuple(boundaries)

    def _finish(self) -> None:
        if self._completed is not None:
            return
        if self._boundaries is None or self._candidate_cursor_payload is None:
            self._aborted = True
            raise _provider_error("integrity_failure")
        closed_at = self._clock()
        if closed_at.tzinfo is None or closed_at.utcoffset() != timedelta(0):
            self._aborted = True
            raise _provider_error("integrity_failure")
        self._completed = CompletedAcquisition(
            boundaries=tuple(
                boundary.model_copy(update={"closed_at": closed_at.astimezone(UTC)})
                for boundary in self._boundaries
            ),
            cursor_version=_SNAPSHOT_CURSOR_VERSION,
            candidate_cursor_payload=self._candidate_cursor_payload,
        )


def _event_created(event: Mapping[str, object]) -> int:
    created = event.get("created")
    if type(created) is not int:
        raise _provider_error("invalid_provider_response")
    return created


def _event_id(event: Mapping[str, object]) -> str:
    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id:
        raise _provider_error("invalid_provider_response")
    return event_id
