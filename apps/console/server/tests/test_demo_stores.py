from __future__ import annotations

import os
from pathlib import Path

import pytest
from heinzel_console.demo.stores import DemoStores, default_state_directory


def test_the_state_directory_is_created_when_absent(tmp_path: Path) -> None:
    stores = DemoStores(tmp_path / "state")
    try:
        assert (tmp_path / "state").is_dir()
    finally:
        stores.close()


def test_every_store_opens_under_the_state_directory(tmp_path: Path) -> None:
    stores = DemoStores(tmp_path / "state")
    try:
        names = {path.name for path in (tmp_path / "state").iterdir()}
    finally:
        stores.close()
    assert {"requests.sqlite3", "semantic.sqlite3", "catalog.sqlite3"} <= names


def test_closing_twice_is_harmless(tmp_path: Path) -> None:
    stores = DemoStores(tmp_path / "state")
    stores.close()
    stores.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unwritable_parent_fails_before_any_store_opens(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)
    try:
        with pytest.raises(OSError):
            DemoStores(blocked / "state")
    finally:
        blocked.chmod(0o700)


def test_the_default_state_directory_follows_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg-example")
    assert default_state_directory() == Path("/tmp/xdg-example/heinzel")


def test_the_default_state_directory_falls_back_to_the_home_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", "/tmp/home-example")
    assert default_state_directory() == Path("/tmp/home-example/.local/state/heinzel")
