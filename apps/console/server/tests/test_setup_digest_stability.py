"""The setup digest must identify the setup state, not the moment it was read.

A command guarded by this digest can only be satisfied if two reads of an
unchanged setup agree. The reset token is an authorization capability the server
mints per read, not part of the state the browser is agreeing to.
"""

from __future__ import annotations

from pillarmesh_console.contracts import setup_snapshot_digest


def _snapshot(reset_token: str) -> dict[str, object]:
    return {
        "workspace_ref": "workspace-a",
        "stages": [],
        "reset_token": reset_token,
        "setup_digest": "0" * 64,
    }


def test_two_reads_of_an_unchanged_setup_agree() -> None:
    first = setup_snapshot_digest(_snapshot("token-one"))
    second = setup_snapshot_digest(_snapshot("token-two"))

    assert first == second


def test_a_changed_setup_state_still_changes_the_digest() -> None:
    unchanged = setup_snapshot_digest(_snapshot("token-one"))
    changed = setup_snapshot_digest(_snapshot("token-one") | {"workspace_ref": "workspace-b"})

    assert unchanged != changed
