from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from pillarmesh_state import RunIntent, RunRecord, RunService, TriggerWindow
from pydantic import BaseModel, ConfigDict, Field

from .policy import DailyTriggerPolicy, RunNowPolicy


class ActivatedRunContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    tenant_id: str = Field(min_length=1)
    contract_ref: str = Field(min_length=1)
    revision: int = Field(ge=1)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ActivatedRunContractReader(Protocol):
    def load_activated(
        self, tenant_id: str, contract_ref: str, revision: int
    ) -> ActivatedRunContract | None: ...


class ContractNotActivatedError(PermissionError):
    def __init__(self) -> None:
        super().__init__("contract is unavailable")


class BackfillWindowError(ValueError):
    pass


def materialize_daily_intent(
    contract: ActivatedRunContract,
    policy: DailyTriggerPolicy,
    *,
    observed_at: datetime,
) -> RunIntent:
    if observed_at.tzinfo is None or observed_at.utcoffset() != timedelta(0):
        raise ValueError("observed_at must be timezone-aware UTC")
    observed_at = observed_at.astimezone(UTC)
    window_end = observed_at.replace(
        hour=policy.hour_utc,
        minute=policy.minute_utc,
        second=0,
        microsecond=0,
    )
    if window_end > observed_at:
        window_end -= timedelta(days=1)
    window = TriggerWindow(starts_at=window_end - timedelta(days=1), ends_at=window_end)
    return _materialize_intent(
        contract,
        policy_version=policy.policy_version,
        window=window,
        reason="scheduled",
    )


def materialize_run_now_intent(
    contract: ActivatedRunContract,
    policy: RunNowPolicy,
    *,
    requested_at: datetime,
) -> RunIntent:
    requested_at = _require_utc(requested_at, field_name="requested_at")
    return _materialize_intent(
        contract,
        policy_version=policy.policy_version,
        window=TriggerWindow(
            starts_at=requested_at,
            ends_at=requested_at + timedelta(microseconds=1),
        ),
        reason="run_now",
    )


def materialize_bounded_backfill(
    contract: ActivatedRunContract,
    policy: DailyTriggerPolicy,
    *,
    start: datetime,
    end: datetime,
) -> tuple[RunIntent, ...]:
    start = _require_backfill_utc(start)
    end = _require_backfill_utc(end)
    if end <= start:
        raise BackfillWindowError("backfill end must follow start")
    if not _is_schedule_boundary(start, policy) or not _is_schedule_boundary(end, policy):
        raise BackfillWindowError("backfill range must be aligned to daily schedule boundaries")
    duration = end - start
    day_seconds = timedelta(days=1).total_seconds()
    if duration.total_seconds() % day_seconds != 0:
        raise BackfillWindowError("backfill range must contain complete daily windows")
    window_count = int(duration.total_seconds() // day_seconds)
    if window_count > policy.maximum_backfill_windows:
        raise BackfillWindowError("backfill range exceeds the configured maximum windows")
    return tuple(
        _materialize_intent(
            contract,
            policy_version=policy.policy_version,
            window=TriggerWindow(
                starts_at=start + timedelta(days=offset),
                ends_at=start + timedelta(days=offset + 1),
            ),
            reason="backfill",
        )
        for offset in range(window_count)
    )


def _materialize_intent(
    contract: ActivatedRunContract,
    *,
    policy_version: str,
    window: TriggerWindow,
    reason: Literal["scheduled", "run_now", "backfill"],
) -> RunIntent:
    return RunIntent(
        tenant_id=contract.tenant_id,
        contract_id=contract.contract_ref,
        contract_revision=contract.revision,
        plan_digest=contract.plan_digest,
        trigger_policy_version=policy_version,
        trigger_window=window,
        reason=reason,
    )


def _require_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(UTC)


def _require_backfill_utc(value: datetime) -> datetime:
    try:
        return _require_utc(value, field_name="backfill timestamp")
    except ValueError as error:
        raise BackfillWindowError(str(error)) from None


def _is_schedule_boundary(value: datetime, policy: DailyTriggerPolicy) -> bool:
    return (
        value.hour == policy.hour_utc
        and value.minute == policy.minute_utc
        and value.second == 0
        and value.microsecond == 0
    )


class TriggerRunService:
    def __init__(self, contracts: ActivatedRunContractReader, runs: RunService) -> None:
        self._contracts = contracts
        self._runs = runs

    def materialize_daily(
        self,
        *,
        tenant_id: str,
        contract_ref: str,
        contract_revision: int,
        policy: DailyTriggerPolicy,
        observed_at: datetime,
    ) -> RunRecord:
        contract = self._require_contract(tenant_id, contract_ref, contract_revision)
        intent = materialize_daily_intent(contract, policy, observed_at=observed_at)
        return self._runs.materialize(intent)

    def materialize_run_now(
        self,
        *,
        tenant_id: str,
        contract_ref: str,
        contract_revision: int,
        policy: RunNowPolicy,
        requested_at: datetime,
    ) -> RunRecord:
        contract = self._require_contract(tenant_id, contract_ref, contract_revision)
        intent = materialize_run_now_intent(contract, policy, requested_at=requested_at)
        return self._runs.materialize(intent)

    def materialize_backfill(
        self,
        *,
        tenant_id: str,
        contract_ref: str,
        contract_revision: int,
        policy: DailyTriggerPolicy,
        start: datetime,
        end: datetime,
    ) -> tuple[RunRecord, ...]:
        contract = self._require_contract(tenant_id, contract_ref, contract_revision)
        intents = materialize_bounded_backfill(contract, policy, start=start, end=end)
        return tuple(self._runs.materialize(intent) for intent in intents)

    def _require_contract(
        self, tenant_id: str, contract_ref: str, contract_revision: int
    ) -> ActivatedRunContract:
        contract = self._contracts.load_activated(tenant_id, contract_ref, contract_revision)
        if contract is None:
            raise ContractNotActivatedError
        try:
            return ActivatedRunContract.model_validate(
                contract.model_dump(mode="python"), strict=True
            )
        except (AttributeError, TypeError, ValueError):
            raise ContractNotActivatedError from None
