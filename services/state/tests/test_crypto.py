from __future__ import annotations

import pytest
from pillarmesh_state import CursorCipherError, decrypt_cursor, encrypt_cursor


class Cipher:
    def encrypt(self, *, tenant_id: str, plaintext: bytes) -> bytes:
        return b"cipher:" + tenant_id.encode() + b":" + plaintext[::-1]

    def decrypt(self, *, tenant_id: str, ciphertext: bytes) -> bytes:
        prefix = b"cipher:" + tenant_id.encode() + b":"
        if not ciphertext.startswith(prefix):
            raise ValueError("wrong tenant secret-canary")
        return ciphertext.removeprefix(prefix)[::-1]


class LeakingCipher(Cipher):
    def encrypt(self, *, tenant_id: str, plaintext: bytes) -> bytes:
        return b"not-really-encrypted:" + plaintext


class FailingCipher(Cipher):
    def encrypt(self, *, tenant_id: str, plaintext: bytes) -> bytes:
        raise RuntimeError(plaintext.decode())


def test_cursor_cipher_round_trip_is_tenant_scoped() -> None:
    plaintext = b'[{"updated_at":"2026-09-01T12:00:00Z","id":7}]'

    ciphertext = encrypt_cursor(Cipher(), tenant_id="tenant-a", plaintext=plaintext)

    assert plaintext not in ciphertext
    assert decrypt_cursor(Cipher(), tenant_id="tenant-a", ciphertext=ciphertext) == plaintext
    with pytest.raises(CursorCipherError) as wrong_tenant:
        decrypt_cursor(Cipher(), tenant_id="tenant-b", ciphertext=ciphertext)
    assert "secret-canary" not in str(wrong_tenant.value)
    assert wrong_tenant.value.__cause__ is None


def test_cursor_cipher_rejects_ciphertext_that_contains_plaintext() -> None:
    with pytest.raises(CursorCipherError, match="encrypt cursor"):
        encrypt_cursor(LeakingCipher(), tenant_id="tenant-a", plaintext=b"private-cursor")


def test_cursor_cipher_failure_does_not_expose_plaintext_or_cause() -> None:
    with pytest.raises(CursorCipherError) as captured:
        encrypt_cursor(FailingCipher(), tenant_id="tenant-a", plaintext=b"private-cursor-canary")

    assert "private-cursor-canary" not in str(captured.value)
    assert captured.value.__cause__ is None
