from __future__ import annotations

from typing import Literal

type LegacyProviderErrorClassification = Literal[
    "retryable",
    "throttled",
    "authorization",
    "permanent",
    "ambiguous",
]

type AcquisitionProviderErrorClassification = Literal[
    "transient_transport",
    "transient_unavailable",
    "throttled",
    "ambiguous_outcome",
    "authorization_denied",
    "statement_rejected",
    "invalid_provider_response",
    "integrity_failure",
    "permanent_configuration",
    "resynchronization_required",
]
type ProviderErrorClassification = (
    LegacyProviderErrorClassification | AcquisitionProviderErrorClassification
)

type AcquisitionProviderKind = Literal["postgresql", "stripe"]
type AcquisitionProviderReasonCode = Literal[
    "ambiguous_outcome",
    "authorization_denied",
    "integrity_failure",
    "invalid_provider_response",
    "permanent_configuration",
    "provider_unavailable",
    "rate_limited",
    "statement_rejected",
    "stripe_event_cursor_expired",
    "stripe_event_overlap_gap",
    "transport_failure",
]

_PROVIDER_KINDS = frozenset(("postgresql", "stripe"))
_CLASSIFICATION_BY_REASON: dict[
    AcquisitionProviderReasonCode, AcquisitionProviderErrorClassification
] = {
    "ambiguous_outcome": "ambiguous_outcome",
    "authorization_denied": "authorization_denied",
    "integrity_failure": "integrity_failure",
    "invalid_provider_response": "invalid_provider_response",
    "permanent_configuration": "permanent_configuration",
    "provider_unavailable": "transient_unavailable",
    "rate_limited": "throttled",
    "statement_rejected": "statement_rejected",
    "stripe_event_cursor_expired": "resynchronization_required",
    "stripe_event_overlap_gap": "resynchronization_required",
    "transport_failure": "transient_transport",
}
_REASON_CODES = frozenset(_CLASSIFICATION_BY_REASON)
_ACQUISITION_CLASSIFICATIONS = frozenset(_CLASSIFICATION_BY_REASON.values())
_STRIPE_REASON_CODES = frozenset(("stripe_event_cursor_expired", "stripe_event_overlap_gap"))


class ProviderError(RuntimeError):
    classification: ProviderErrorClassification

    def __init__(
        self,
        message: str,
        classification: ProviderErrorClassification,
    ) -> None:
        super().__init__(message)
        self.classification = classification


class AcquisitionProviderError(ProviderError):
    classification: AcquisitionProviderErrorClassification
    provider_kind: AcquisitionProviderKind
    reason_code: AcquisitionProviderReasonCode

    def __init__(
        self,
        *,
        provider_kind: AcquisitionProviderKind,
        classification: AcquisitionProviderErrorClassification,
        reason_code: AcquisitionProviderReasonCode,
    ) -> None:
        if provider_kind not in _PROVIDER_KINDS:
            raise ValueError("provider_kind must be allowlisted")
        if reason_code not in _REASON_CODES:
            raise ValueError("reason_code must be allowlisted")
        if reason_code in _STRIPE_REASON_CODES and provider_kind != "stripe":
            raise ValueError("stripe continuity reasons require the stripe provider")
        if classification not in _ACQUISITION_CLASSIFICATIONS:
            raise ValueError("classification must be allowlisted")
        if classification != _CLASSIFICATION_BY_REASON[reason_code]:
            raise ValueError("classification must match reason_code")
        super().__init__(f"{provider_kind} acquisition failed: {reason_code}", classification)
        self.provider_kind = provider_kind
        self.reason_code = reason_code
