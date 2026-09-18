from __future__ import annotations

from typing import Protocol


class CursorCipher(Protocol):
    def encrypt(self, *, tenant_id: str, plaintext: bytes) -> bytes: ...

    def decrypt(self, *, tenant_id: str, ciphertext: bytes) -> bytes: ...


class CursorCipherError(RuntimeError):
    def __init__(self, *, operation: str) -> None:
        self.operation = operation
        super().__init__(f"cursor cipher failed during {operation}")


def encrypt_cursor(cipher: CursorCipher, *, tenant_id: str, plaintext: bytes) -> bytes:
    try:
        ciphertext = bytes(cipher.encrypt(tenant_id=tenant_id, plaintext=plaintext))
    except Exception:
        raise CursorCipherError(operation="encrypt cursor") from None
    if not ciphertext or (plaintext and plaintext in ciphertext):
        raise CursorCipherError(operation="encrypt cursor")
    return ciphertext


def decrypt_cursor(cipher: CursorCipher, *, tenant_id: str, ciphertext: bytes) -> bytes:
    try:
        return bytes(cipher.decrypt(tenant_id=tenant_id, ciphertext=ciphertext))
    except Exception:
        raise CursorCipherError(operation="decrypt cursor") from None
