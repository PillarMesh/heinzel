from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from heinzel_console.demo.collaborators import _published_semantic_objects
from heinzel_console.demo.materialization import demo_target_schema
from heinzel_console.demo.publication import build_demo_publication
from heinzel_console.demo.stores import DemoStores
from heinzel_contract_model import digest

TENANT = "tenant-demo"


def _clock(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


def test_the_demo_publication_is_stored_and_listed(tmp_path: Path) -> None:
    stores = DemoStores(tmp_path / "state")
    try:
        build_demo_publication(stores, clock=_clock(datetime(2026, 1, 1, tzinfo=UTC)))
        assert len(stores.publications.list_publications(tenant_id=TENANT)) == 1
    finally:
        stores.close()


def test_the_demo_publication_carries_a_validity_window_longer_than_a_month(
    tmp_path: Path,
) -> None:
    stores = DemoStores(tmp_path / "state")
    try:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        published = build_demo_publication(stores, clock=_clock(now))
        assert published.valid_until > now + timedelta(days=30)
    finally:
        stores.close()


def test_building_twice_leaves_one_publication(tmp_path: Path) -> None:
    stores = DemoStores(tmp_path / "state")
    try:
        clock = _clock(datetime(2026, 1, 1, tzinfo=UTC))
        first = build_demo_publication(stores, clock=clock)
        effects = stores.publications.effect_count(tenant_id=TENANT)
        second = build_demo_publication(stores, clock=clock)
        assert len(stores.publications.list_publications(tenant_id=TENANT)) == 1
        assert stores.publications.effect_count(tenant_id=TENANT) == effects
        assert second == first
    finally:
        stores.close()


def test_a_clock_without_a_utc_offset_is_refused(tmp_path: Path) -> None:
    stores = DemoStores(tmp_path / "state")
    try:
        naive = datetime(2026, 1, 1)
        with pytest.raises(ValueError, match="timezone-aware UTC"):
            build_demo_publication(stores, clock=_clock(naive))
        assert stores.publications.list_publications(tenant_id=TENANT) == ()
    finally:
        stores.close()


def test_the_published_term_mirror_matches_the_publication_intent(tmp_path: Path) -> None:
    """`_published_semantic_objects` must list exactly what the intent publishes.

    The two are matched by hand, so a change to `publication_intent` for a kind this
    demonstration does not currently publish — events, states, relationships — would stay
    invisible until someone extended the publication, and would then surface as an
    unanswerable 409 rather than as anything legible.
    """
    stores = DemoStores(tmp_path / "state")
    try:
        published = build_demo_publication(stores, clock=_clock(datetime(2026, 1, 1, tzinfo=UTC)))
        intent, _receipt, _references = stores.publications.load_publication(
            tenant_id=TENANT, publication_id=published.receipt.publication_id
        )
        assert _published_semantic_objects(published.semantic_version) == intent.semantic_objects
    finally:
        stores.close()


def test_two_starts_a_year_apart_approve_the_same_meaning_and_name_the_same_schema(
    tmp_path: Path,
) -> None:
    """The contract is a canonical artifact, so its digest must not move for unchanged inputs.

    It moved: the approved meaning took `created_at` from the clock, the contract carries that
    version's digest, and `demo_target_schema` names the materialization schema after the contract.
    One approved meaning therefore had a different schema every start, and a warehouse reused across
    starts accumulated one per run -- while the consumption object that schema name reaches
    identified the dashboard's dataset differently each time.

    A year apart rather than a second, so a clock that reached the artifact could not pass by
    rounding.
    """
    first = DemoStores(tmp_path / "first")
    second = DemoStores(tmp_path / "second")
    try:
        earlier = build_demo_publication(first, clock=_clock(datetime(2026, 1, 1, tzinfo=UTC)))
        later = build_demo_publication(second, clock=_clock(datetime(2027, 1, 1, tzinfo=UTC)))

        assert digest(earlier.semantic_version) == digest(later.semantic_version)
        assert digest(earlier.contract) == digest(later.contract)
        assert demo_target_schema(earlier.contract) == demo_target_schema(later.contract)
    finally:
        first.close()
        second.close()


def test_what_this_start_did_still_carries_this_start_s_time(tmp_path: Path) -> None:
    """Fixing the artifact must not fix the evidence around it.

    The publication receipt records when this demonstration published, and the authority window
    runs from now: a determinism that reached those would be a demonstration claiming it published
    on a date in its own source.
    """
    now = datetime(2027, 3, 4, tzinfo=UTC)
    stores = DemoStores(tmp_path / "state")
    try:
        published = build_demo_publication(stores, clock=_clock(now))

        assert published.valid_until > now
        receipt = stores.publications.list_publications(tenant_id=TENANT)[0]
        assert receipt.published_at == now
    finally:
        stores.close()
