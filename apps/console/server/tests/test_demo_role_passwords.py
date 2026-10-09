"""The warehouse role passwords are the same on every start over one state directory.

They used to be minted per start, which worked only because the administering login reset
them into PostgreSQL on the way up. What it cost is every connection detail downstream: a
source connection is enrolled under a handle and the enrolment is immutable on purpose, so a
DSN carrying a password that changed overnight is refused as a different detail for a handle
already validated -- and the demonstration therefore enrolled no source at all.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from heinzel_console.demo.role_passwords import (
    ROLE_PASSWORD_ROOT_FILENAME,
    RolePasswordsUnavailable,
    role_passwords,
)
from heinzel_console.demo.warehouse import DEMO_WAREHOUSE_ROLES


def test_two_starts_over_one_state_directory_derive_the_same_passwords(tmp_path: Path) -> None:
    first = role_passwords(tmp_path, DEMO_WAREHOUSE_ROLES)
    second = role_passwords(tmp_path, DEMO_WAREHOUSE_ROLES)

    assert first == second
    assert set(first) == set(DEMO_WAREHOUSE_ROLES)


def test_two_state_directories_derive_different_passwords(tmp_path: Path) -> None:
    """One demonstration's credentials are not another's, even on the same machine."""
    assert role_passwords(tmp_path / "a", DEMO_WAREHOUSE_ROLES) != role_passwords(
        tmp_path / "b", DEMO_WAREHOUSE_ROLES
    )


def test_every_role_gets_its_own_password(tmp_path: Path) -> None:
    """One root is behind all of them, so the derivation has to separate them by name.

    Two roles sharing a password would make the least-privilege split a fiction: whoever held
    the answer role's credential could connect as the one that writes.
    """
    passwords = role_passwords(tmp_path, DEMO_WAREHOUSE_ROLES)

    assert len(set(passwords.values())) == len(passwords)


def test_the_root_is_created_unreadable_by_anyone_but_its_owner(tmp_path: Path) -> None:
    role_passwords(tmp_path, DEMO_WAREHOUSE_ROLES)

    mode = (tmp_path / ROLE_PASSWORD_ROOT_FILENAME).stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_a_root_that_is_not_a_root_is_refused_rather_than_used(tmp_path: Path) -> None:
    """A truncated file would otherwise derive passwords PostgreSQL has never been told."""
    (tmp_path / ROLE_PASSWORD_ROOT_FILENAME).write_bytes(os.urandom(8))

    with pytest.raises(RolePasswordsUnavailable):
        role_passwords(tmp_path, DEMO_WAREHOUSE_ROLES)
