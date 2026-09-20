from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from heinzel_console.demo.publication import build_demo_publication
from heinzel_console.demo.stores import DemoStores

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


def test_the_authority_observation_outlives_a_long_demonstration(tmp_path: Path) -> None:
    stores = DemoStores(tmp_path / "state")
    try:
        now = datetime.now(UTC)
        published = build_demo_publication(stores, clock=_clock(now))
        assert published.valid_until > now + timedelta(days=30)
    finally:
        stores.close()


def test_building_twice_leaves_one_publication(tmp_path: Path) -> None:
    stores = DemoStores(tmp_path / "state")
    try:
        clock = _clock(datetime(2026, 1, 1, tzinfo=UTC))
        first = build_demo_publication(stores, clock=clock)
        second = build_demo_publication(stores, clock=clock)
        assert len(stores.publications.list_publications(tenant_id=TENANT)) == 1
        assert second.catalog_binding_id == first.catalog_binding_id
    finally:
        stores.close()
