from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from heinzel_bi_control import DashboardContractVerifier
from heinzel_console.demo.catalog import demo_dimension_reference, demo_metric_reference
from heinzel_console.demo.dashboard_contract import (
    DASHBOARD_CONTRACT_KEY_FILENAME,
    DEMO_DASHBOARD_ID,
    DEMO_DASHBOARD_VERSION,
    DemoDashboardContractError,
    demo_dashboard_contract_keys,
    resolve_dashboard_contract_key,
    seed_demo_dashboard_contract,
)
from heinzel_console.demo.publication import DemoPublication, build_demo_publication
from heinzel_console.demo.stores import DemoStores
from heinzel_console.governed_adapters import (
    ContractPublishableDashboardReader,
    RepositoryDashboardRevisionReader,
)
from heinzel_contract_model import ApprovedSemanticVersion, ArtifactReference
from heinzel_request_management import DashboardAnswerAuthority, DashboardProductGenerationReference

TENANT = "tenant-demo"
NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
PRODUCT_REF = ArtifactReference(artifact_id="orders_daily", version=1, digest="a" * 64)
POLICY_REF = ArtifactReference(artifact_id="answer-policy-demo", version=1, digest="b" * 64)


class _Answers:
    def __init__(self, answer_id: str | None = "answer-1") -> None:
        self._answer_id = answer_id

    def current_answer_id(self, tenant_id: str, request_id: str) -> str | None:
        del tenant_id, request_id
        return self._answer_id


class _AnswerAuthority:
    def __init__(self, semantic_version: ApprovedSemanticVersion) -> None:
        self._semantic_version = semantic_version

    def read_exact(
        self, *, tenant_id: str, request_id: str, answer_id: str
    ) -> DashboardAnswerAuthority:
        return DashboardAnswerAuthority(
            tenant_id=tenant_id,
            request_id=request_id,
            request_revision=6,
            answer_id=answer_id,
            title="Daily order value by region",
            execution_receipt_ref="execution-1",
            result_ref="result-1",
            result_digest="c" * 64,
            product_generation_refs=(
                DashboardProductGenerationReference(product_ref=PRODUCT_REF, generation=1),
            ),
            metric_version_refs=(demo_metric_reference(self._semantic_version),),
            as_of=NOW - timedelta(hours=2),
            freshness_disposition="current",
            delivered_at=NOW - timedelta(minutes=1),
            result_expires_at=NOW + timedelta(hours=1),
        )


def _publication(stores: DemoStores) -> DemoPublication:
    return build_demo_publication(stores, clock=lambda: NOW)


def test_the_seeded_contract_is_certified_over_the_answers_own_product_and_terms(
    tmp_path: Path,
) -> None:
    stores = DemoStores(tmp_path / "state")
    try:
        publication = _publication(stores)
        signing_key = resolve_dashboard_contract_key(tmp_path / "state")

        signed = seed_demo_dashboard_contract(
            stores.dashboard_contracts,
            tenant_id=TENANT,
            signing_key=signing_key,
            semantic_version=publication.semantic_version,
            product_ref=PRODUCT_REF,
            access_policy_ref=POLICY_REF,
        )

        contract = signed.contract
        assert contract.lifecycle_state == "certified"
        assert contract.dashboard_id == DEMO_DASHBOARD_ID
        assert contract.version == DEMO_DASHBOARD_VERSION
        assert contract.data_product_versions == (PRODUCT_REF,)
        assert contract.metric_versions == (demo_metric_reference(publication.semantic_version),)
        assert contract.dimensions == (demo_dimension_reference(publication.semantic_version),)
        assert contract.visual_intents == ("line",)
        # Verified by the real verifier under the identifier the stored contract names.
        assert (
            DashboardContractVerifier(demo_dashboard_contract_keys(signing_key)).verify(signed)
            == contract
        )
    finally:
        stores.close()


def test_seeding_twice_adopts_the_first_contract_rather_than_signing_a_second(
    tmp_path: Path,
) -> None:
    stores = DemoStores(tmp_path / "state")
    try:
        publication = _publication(stores)
        key = resolve_dashboard_contract_key(tmp_path / "state")
        first = seed_demo_dashboard_contract(
            stores.dashboard_contracts,
            tenant_id=TENANT,
            signing_key=key,
            semantic_version=publication.semantic_version,
            product_ref=PRODUCT_REF,
            access_policy_ref=POLICY_REF,
        )
        second = seed_demo_dashboard_contract(
            stores.dashboard_contracts,
            tenant_id=TENANT,
            signing_key=key,
            semantic_version=publication.semantic_version,
            product_ref=PRODUCT_REF,
            access_policy_ref=POLICY_REF,
        )

        assert second == first
    finally:
        stores.close()


def test_the_signing_key_survives_a_restart_so_a_stored_contract_still_verifies(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    stores = DemoStores(state_dir)
    try:
        publication = _publication(stores)
        first_key = resolve_dashboard_contract_key(state_dir)
        signed = seed_demo_dashboard_contract(
            stores.dashboard_contracts,
            tenant_id=TENANT,
            signing_key=first_key,
            semantic_version=publication.semantic_version,
            product_ref=PRODUCT_REF,
            access_policy_ref=POLICY_REF,
        )
    finally:
        stores.close()

    restarted = DemoStores(state_dir)
    try:
        second_key = resolve_dashboard_contract_key(state_dir)
        stored = restarted.dashboard_contracts.read_exact(
            tenant_id=TENANT, dashboard_id=DEMO_DASHBOARD_ID, version=DEMO_DASHBOARD_VERSION
        )
        assert stored == signed
        assert stored is not None
        assert (
            DashboardContractVerifier(demo_dashboard_contract_keys(second_key)).verify(stored)
            == signed.contract
        )
    finally:
        restarted.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_the_signing_key_is_written_readable_only_by_its_owner(tmp_path: Path) -> None:
    resolve_dashboard_contract_key(tmp_path / "state")

    mode = (tmp_path / "state" / DASHBOARD_CONTRACT_KEY_FILENAME).stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_a_truncated_key_file_is_refused_rather_than_used(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    resolve_dashboard_contract_key(state_dir)
    (state_dir / DASHBOARD_CONTRACT_KEY_FILENAME).write_bytes(b"short")

    with pytest.raises(DemoDashboardContractError):
        resolve_dashboard_contract_key(state_dir)


def test_the_seeded_contract_is_what_the_offering_names_for_the_demonstrations_answer(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    stores = DemoStores(state_dir)
    try:
        publication = _publication(stores)
        signing_key = resolve_dashboard_contract_key(state_dir)
        seed_demo_dashboard_contract(
            stores.dashboard_contracts,
            tenant_id=TENANT,
            signing_key=signing_key,
            semantic_version=publication.semantic_version,
            product_ref=PRODUCT_REF,
            access_policy_ref=POLICY_REF,
        )
        reader = ContractPublishableDashboardReader(
            contracts=stores.dashboard_contracts,
            contract_verifier=DashboardContractVerifier(demo_dashboard_contract_keys(signing_key)),
            answers=_Answers(),
            answer_authority=_AnswerAuthority(publication.semantic_version),
            dashboard_control=RepositoryDashboardRevisionReader(stores.dashboards),
            clock=lambda: NOW,
        )

        offering = reader.offering(tenant_id=TENANT, request_id="request-1")

        assert offering.answer_title == "Daily order value by region"
        assert [item.dashboard_id for item in offering.dashboards] == [DEMO_DASHBOARD_ID]
        assert offering.dashboards[0].next_revision == 1
    finally:
        stores.close()


def test_a_request_with_no_answer_is_offered_the_seeded_contract_for_nothing(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    stores = DemoStores(state_dir)
    try:
        publication = _publication(stores)
        signing_key = resolve_dashboard_contract_key(state_dir)
        seed_demo_dashboard_contract(
            stores.dashboard_contracts,
            tenant_id=TENANT,
            signing_key=signing_key,
            semantic_version=publication.semantic_version,
            product_ref=PRODUCT_REF,
            access_policy_ref=POLICY_REF,
        )
        reader = ContractPublishableDashboardReader(
            contracts=stores.dashboard_contracts,
            contract_verifier=DashboardContractVerifier(demo_dashboard_contract_keys(signing_key)),
            answers=_Answers(answer_id=None),
            answer_authority=_AnswerAuthority(publication.semantic_version),
            dashboard_control=RepositoryDashboardRevisionReader(stores.dashboards),
            clock=lambda: NOW,
        )

        offering = reader.offering(tenant_id=TENANT, request_id="request-1")

        assert offering.answer_title is None
        assert offering.dashboards == ()
    finally:
        stores.close()
