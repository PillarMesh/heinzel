from __future__ import annotations

import stat
from pathlib import Path

import pytest
from heinzel_console.demo.cursor_cipher import CURSOR_KEY_FILENAME, DemoCursorCipher
from heinzel_state import CursorCipher, CursorCipherError, decrypt_cursor, encrypt_cursor

TENANT = "tenant-demo"
OTHER_TENANT = "tenant-other"
CURSOR = b'{"updated_at":"2026-09-01T12:00:00Z","id":7}'


def _cipher(state_dir: Path) -> CursorCipher:
    """The cipher, seen as the repository sees it.

    The annotation is the point: it makes the type checker, not a comment, answer for this
    class satisfying the protocol `SQLiteAcquisitionStateRepository` takes.
    """
    return DemoCursorCipher(state_dir)


def test_a_sealed_cursor_opens_back_to_the_same_bytes_for_its_tenant(tmp_path: Path) -> None:
    cipher = _cipher(tmp_path / "state")
    ciphertext = encrypt_cursor(cipher, tenant_id=TENANT, plaintext=CURSOR)
    assert decrypt_cursor(cipher, tenant_id=TENANT, ciphertext=ciphertext) == CURSOR


def test_an_empty_cursor_opens_back_as_an_empty_cursor(tmp_path: Path) -> None:
    """The repository seals whatever a source reports, including nothing at all."""
    cipher = _cipher(tmp_path / "state")
    ciphertext = encrypt_cursor(cipher, tenant_id=TENANT, plaintext=b"")
    assert decrypt_cursor(cipher, tenant_id=TENANT, ciphertext=ciphertext) == b""


def test_the_ciphertext_never_carries_the_cursor_it_sealed(tmp_path: Path) -> None:
    """`encrypt_cursor` refuses a cipher that passes its plaintext through, and must not here.

    A pass-through would leave the cursor legible in the demonstration's SQLite state while
    every record claimed it was encrypted.
    """
    cipher = _cipher(tmp_path / "state")
    ciphertext = encrypt_cursor(cipher, tenant_id=TENANT, plaintext=CURSOR)
    assert CURSOR not in ciphertext
    assert b"updated_at" not in ciphertext


def test_sealing_one_cursor_twice_never_produces_the_same_ciphertext(tmp_path: Path) -> None:
    """A nonce reused under one key forfeits both confidentiality and tenant binding."""
    cipher = _cipher(tmp_path / "state")
    sealed = {encrypt_cursor(cipher, tenant_id=TENANT, plaintext=CURSOR) for _ in range(16)}
    assert len(sealed) == 16
    for ciphertext in sealed:
        assert decrypt_cursor(cipher, tenant_id=TENANT, ciphertext=ciphertext) == CURSOR


def test_a_cursor_sealed_before_a_restart_still_opens_after_one(tmp_path: Path) -> None:
    """The ciphertext outlives the process, so the key must too.

    Two ciphers over one state directory stand in for two runs of the console: the second has
    to open what the first committed, or every checkpoint in the state directory is lost.
    """
    state_dir = tmp_path / "state"
    ciphertext = encrypt_cursor(_cipher(state_dir), tenant_id=TENANT, plaintext=CURSOR)
    assert decrypt_cursor(_cipher(state_dir), tenant_id=TENANT, ciphertext=ciphertext) == CURSOR


def test_a_cipher_over_another_state_directory_cannot_open_the_first_ones_cursor(
    tmp_path: Path,
) -> None:
    """Which keeps the test above from passing on a key that is the same for everyone."""
    ciphertext = encrypt_cursor(_cipher(tmp_path / "first"), tenant_id=TENANT, plaintext=CURSOR)
    with pytest.raises(CursorCipherError):
        decrypt_cursor(_cipher(tmp_path / "second"), tenant_id=TENANT, ciphertext=ciphertext)


def test_a_cursor_sealed_for_one_tenant_does_not_open_for_another(tmp_path: Path) -> None:
    cipher = _cipher(tmp_path / "state")
    ciphertext = encrypt_cursor(cipher, tenant_id=TENANT, plaintext=CURSOR)
    with pytest.raises(CursorCipherError):
        decrypt_cursor(cipher, tenant_id=OTHER_TENANT, ciphertext=ciphertext)


def test_a_tampered_ciphertext_is_refused_rather_than_opened_as_other_bytes(
    tmp_path: Path,
) -> None:
    """Every byte is authenticated, the nonce included, so no edit can go unnoticed."""
    cipher = _cipher(tmp_path / "state")
    ciphertext = encrypt_cursor(cipher, tenant_id=TENANT, plaintext=CURSOR)
    for position in (0, len(ciphertext) // 2, len(ciphertext) - 1):
        tampered = bytearray(ciphertext)
        tampered[position] ^= 0x01
        with pytest.raises(CursorCipherError):
            decrypt_cursor(cipher, tenant_id=TENANT, ciphertext=bytes(tampered))


def test_a_ciphertext_too_short_to_hold_a_nonce_is_refused(tmp_path: Path) -> None:
    cipher = _cipher(tmp_path / "state")
    with pytest.raises(CursorCipherError):
        decrypt_cursor(cipher, tenant_id=TENANT, ciphertext=b"short")


def test_the_key_file_is_created_readable_only_by_its_owner(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _cipher(state_dir)
    key_path = state_dir / CURSOR_KEY_FILENAME
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_a_key_file_that_does_not_hold_a_key_is_refused(tmp_path: Path) -> None:
    """Rather than sealing new cursors under something the committed ones were not sealed under."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / CURSOR_KEY_FILENAME).write_bytes(b"not a key")
    with pytest.raises(CursorCipherError):
        _cipher(state_dir)
