"""The demonstration answers its own seeded question, over its own warehouse and its own policy.

Every other test of this chain stops somewhere short. This one goes the whole way: it resolves
the requester's entitlement from the local signed authority over loopback TLS, reads the
materialization receipt and the freshness observation, validates the question against the
approved scope policy, reads the column mapping out of the durable query binding, compiles the
statement, has the warehouse bound its scan, admits the plan under the policy's ceilings,
executes it as the read-only answer role, and reads the delivered answer back as the requester.

It is the only test that can say the demonstration works. Every part of this chain was green
while five separate things the answer path reads were being lost or invented: the catalog
publication, the thread affinity of the product stores, the materialization receipt, the signed
compiled model, and the scan estimate.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from heinzel_console.demo.answer_runtime import demo_governed_answer
from heinzel_console.demo.answers import demo_question_selection
from heinzel_console.demo.bootstrap import ensure_demo_generation
from heinzel_console.demo.collaborators import (
    DEMO_REQUESTER_ID,
    DEMO_REQUESTER_PRINCIPAL_REF,
)
from heinzel_console.demo.cursor_cipher import DemoCursorCipher
from heinzel_console.demo.materialization import DEMO_GROUP_COLUMN, DEMO_MEASURE_COLUMN
from heinzel_console.demo.publication import DEMO_TENANT_ID, build_demo_publication
from heinzel_console.demo.role_passwords import role_passwords
from heinzel_console.demo.seed import seed_demo_request
from heinzel_console.demo.source_registry import open_demo_source_registry
from heinzel_console.demo.stores import DemoStores
from heinzel_console.demo.warehouse import DEMO_SOURCE_DAYS, DEMO_WAREHOUSE_ROLES, role_dsn
from heinzel_console.governed_adapters import InMemoryWorkspacePrincipalDirectory
from heinzel_request_management import (
    RequestManagementService,
    RequestState,
    StakeholderQuestion,
)

from tests.integration.test_postgresql_answer_query_live import _fresh_postgresql_cluster

pytestmark = pytest.mark.live


def _clock() -> datetime:
    return datetime.now(UTC)


def test_the_demonstration_answers_its_own_seeded_question(tmp_path: Path) -> None:
    """Acquire, land, compile, materialize, publish, admit, execute, deliver.

    The architect moves the request to `investigating`, which is where a question becomes one the
    governed answer is asked. Everything after that is the product's own: nothing here writes a
    result, a plan, an admission or a row of the answer.
    """
    dbt_executable = shutil.which("dbt")
    if dbt_executable is None:
        pytest.skip("the locked dbt executable is unavailable")

    with _fresh_postgresql_cluster(tmp_path) as bootstrap_dsn:
        # The cipher that seals the source cursors: without one the governed acquisition has
        # no state store to admit a checkpoint into and refuses to run at all.
        stores = DemoStores(tmp_path / "state", cursor_cipher_factory=DemoCursorCipher)
        # The connection broker the bootstrap registers its source through. Opening it enrols
        # the acquisition login under the handle and reaches nothing; the bootstrap registers
        # it once the warehouse that login belongs to exists.
        sources = open_demo_source_registry(
            tmp_path / "state",
            acquisition_dsn=role_dsn(
                bootstrap_dsn,
                DEMO_WAREHOUSE_ROLES.acquisition,
                role_passwords(tmp_path / "state", DEMO_WAREHOUSE_ROLES)[
                    DEMO_WAREHOUSE_ROLES.acquisition
                ],
            ),
            clock=_clock,
        )
        try:
            publication = build_demo_publication(stores, clock=_clock)
            requests = RequestManagementService(stores.requests, clock=_clock)
            seed_demo_request(requests)
            seeded = next(
                request
                for request in requests.list_inbox(DEMO_TENANT_ID)
                if request.requester_id == DEMO_REQUESTER_ID
            )
            investigating = requests.transition(
                DEMO_TENANT_ID,
                seeded.request_id,
                RequestState.INVESTIGATING,
                actor_id="architect-demo",
                expected_revision=seeded.revision,
            )

            generation = ensure_demo_generation(
                bootstrap_dsn=bootstrap_dsn,
                sources=sources,
                stores=stores,
                publication=publication,
                dbt_executable=Path(dbt_executable),
                state_dir=tmp_path / "state",
                workspace=tmp_path / "materialization",
                clock=_clock,
            )

            principals = InMemoryWorkspacePrincipalDirectory()
            principals.bind_principal(
                tenant_id=DEMO_TENANT_ID,
                actor_id=DEMO_REQUESTER_ID,
                role="requester",
                principal_ref=DEMO_REQUESTER_PRINCIPAL_REF,
            )

            with demo_governed_answer(
                tmp_path / "answers",
                stores=stores,
                requests=requests,
                principals=principals,
                publication=publication,
                generation=generation,
                principal_ref=DEMO_REQUESTER_PRINCIPAL_REF,
                clock=_clock,
            ) as governed:
                # The seeded question itself, selection included: the interpreter resolves the
                # governed terms the request carries and refuses a question that names none.
                assert isinstance(seeded.payload, StakeholderQuestion)
                assert seeded.payload.selection == demo_question_selection()
                admission = governed.preparation.prepare(
                    request_id=seeded.request_id,
                    request_revision=investigating.revision,
                    question=seeded.payload,
                    actor_id="architect-demo",
                )

                # Admitted under the policy, not merely compiled: the scan the estimator measured
                # was inside the ceiling the policy set.
                assert admission.plan_digest is not None

                governed.runtime.execute_answer(
                    tenant_id=DEMO_TENANT_ID,
                    request_id=seeded.request_id,
                    actor_id="heinzel-runtime",
                    expected_revision=admission.request_revision + 1,
                )

                assert (
                    requests.get(DEMO_TENANT_ID, seeded.request_id).state is RequestState.DELIVERED
                )

                answer = governed.runtime.answers.read_for_request(
                    DEMO_TENANT_ID, DEMO_REQUESTER_ID, seeded.request_id
                )
                assert answer.result_ref is not None
                assert answer.freshness_disposition == "current"
                assert answer.material_quality_limitations == ()

                # The answer is the demonstration's own numbers, read out of the product the
                # chain materialized. Asserting only that an answer exists would pass for an
                # answer about anything at all.
                snapshot = governed.runtime.results.read_result(DEMO_TENANT_ID, answer.result_ref)
                assert tuple(column.name for column in snapshot.columns) == (
                    DEMO_GROUP_COLUMN,
                    DEMO_MEASURE_COLUMN,
                )
                assert [tuple(str(value) for value in row) for row in snapshot.rows] == list(
                    zip(
                        DEMO_SOURCE_DAYS,
                        ("30.000000000", "125.500000000", "99.000000000"),
                        strict=True,
                    )
                )
                # Nothing was withheld: the policy's minimum group size is one and every day is
                # its own group, so a suppressed group would mean the query grouped wrongly.
                assert snapshot.row_count == 3
        finally:
            sources.close()
            stores.close()
