"""The values the governed answer is validated against, derived rather than asserted.

The whole preparation needs a warehouse, and the live suite drives it. What is here is the part
that decides what the validation is told: which approved terms the entitlement covers, and how
stale the product is. Both are values the acceptance fixture states by hand, and a demonstration
that stated them would be demonstrating its own statements.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from heinzel_console.demo.answer_preparation import (
    DemoGovernedAnswerUnavailable,
    entitled_references,
    product_staleness_seconds,
)
from heinzel_console.demo.answers import demo_answer_bindings, demo_product_reference
from heinzel_console.demo.catalog import demo_dimension_reference, demo_metric_reference
from heinzel_console.demo.publication import DemoPublication, build_demo_publication
from heinzel_console.demo.stores import DemoStores
from heinzel_contract_model import ArtifactReference, digest
from heinzel_request_management import BoundSemanticReference

_NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)


@pytest.fixture(name="published")
def _published(tmp_path: Path) -> Iterator[DemoPublication]:
    stores = DemoStores(tmp_path / "state")
    try:
        yield build_demo_publication(stores, clock=lambda: _NOW)
    finally:
        stores.close()


def _bindings(published: DemoPublication) -> tuple[BoundSemanticReference, ...]:
    return demo_answer_bindings(
        published.semantic_version,
        product_ref=demo_product_reference(
            published.contract.version, digest(published.contract.destination_product)
        ),
    )


def test_an_entitlement_over_both_terms_covers_both(published: DemoPublication) -> None:
    bindings = _bindings(published)

    covered = entitled_references(
        bindings,
        semantic_refs=(
            demo_metric_reference(published.semantic_version),
            demo_dimension_reference(published.semantic_version),
        ),
    )

    assert set(covered) == {binding.canonical_ref for binding in bindings}


def test_a_narrowed_entitlement_narrows_what_the_question_may_name(
    published: DemoPublication,
) -> None:
    """Listing every bound term instead would make the entitlement's own scope decorative."""
    bindings = _bindings(published)

    covered = entitled_references(
        bindings, semantic_refs=(demo_metric_reference(published.semantic_version),)
    )

    metric = next(binding for binding in bindings if binding.kind == "metric")
    assert covered == (metric.canonical_ref,)


def test_an_entitlement_naming_another_revision_of_a_term_covers_nothing(
    published: DemoPublication,
) -> None:
    """A term's meaning is part of its identity, so the match is on the whole reference."""
    metric = demo_metric_reference(published.semantic_version)
    reworded = ArtifactReference(
        artifact_id=metric.artifact_id, version=metric.version, digest="b" * 64
    )

    assert entitled_references(_bindings(published), semantic_refs=(reworded,)) == ()


def test_staleness_is_measured_from_the_least_current_input() -> None:
    """A product is as stale as the least current thing it was built from."""
    recent = _NOW - timedelta(minutes=30)
    older = _NOW - timedelta(hours=6)

    assert product_staleness_seconds((recent, older), now=_NOW) == 6 * 3600


def test_a_watermark_ahead_of_the_clock_is_not_negative_staleness() -> None:
    """Negative staleness would read as a very fresh product rather than an impossible one."""
    assert product_staleness_seconds((_NOW + timedelta(hours=1),), now=_NOW) == 0


def test_a_generation_with_no_input_to_measure_is_refused() -> None:
    """Zero staleness would claim the product is current, having measured nothing."""
    with pytest.raises(DemoGovernedAnswerUnavailable, match="no input to measure"):
        product_staleness_seconds((), now=_NOW)
