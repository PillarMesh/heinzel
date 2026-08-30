from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_BACKUP_MAGIC = b"PMWHBK01"
_BACKUP_HEADER = struct.Struct(">8sI4s")
_BACKUP_RECORD = struct.Struct(">QBI")
_BACKUP_ASSOCIATED_DATA = struct.Struct(">QB")


class BackupStreamIntegrityError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("warehouse backup artifact failed integrity validation")


@dataclass(frozen=True, slots=True)
class BackupStreamObservation:
    plaintext_bytes: int
    chunk_count: int
    maximum_buffer_bytes: int


class BinaryReader(Protocol):
    def read(self, size: int = -1) -> bytes: ...


class BinaryWriter(Protocol):
    def write(self, value: bytes) -> int: ...


def encrypt_backup_stream(
    source: BinaryReader,
    destination: BinaryWriter,
    *,
    key: bytes,
    chunk_size: int,
    nonce_prefix: bytes,
) -> BackupStreamObservation:
    if len(key) != 32 or len(nonce_prefix) != 4 or chunk_size < 4 * 1024:
        raise ValueError("streaming backup encryption configuration is invalid")
    cipher = AESGCM(key)
    header = _BACKUP_HEADER.pack(_BACKUP_MAGIC, chunk_size, nonce_prefix)
    _write_all(destination, header)
    plaintext_bytes = 0
    chunk_index = 0
    maximum_buffer_bytes = 0
    while True:
        plaintext = source.read(chunk_size)
        if not plaintext:
            break
        if len(plaintext) > chunk_size:
            raise ValueError("backup source violated the bounded read contract")
        metadata = _BACKUP_ASSOCIATED_DATA.pack(chunk_index, 0)
        ciphertext = cipher.encrypt(
            _backup_nonce(nonce_prefix, chunk_index),
            plaintext,
            header + metadata,
        )
        _write_all(destination, _BACKUP_RECORD.pack(chunk_index, 0, len(ciphertext)))
        _write_all(destination, ciphertext)
        plaintext_bytes += len(plaintext)
        chunk_index += 1
        maximum_buffer_bytes = max(maximum_buffer_bytes, len(plaintext) + len(ciphertext))
    final_metadata = _BACKUP_ASSOCIATED_DATA.pack(chunk_index, 1)
    final_tag = cipher.encrypt(
        _backup_nonce(nonce_prefix, chunk_index),
        b"",
        header + final_metadata,
    )
    _write_all(destination, _BACKUP_RECORD.pack(chunk_index, 1, len(final_tag)))
    _write_all(destination, final_tag)
    maximum_buffer_bytes = max(maximum_buffer_bytes, len(final_tag))
    return BackupStreamObservation(
        plaintext_bytes=plaintext_bytes,
        chunk_count=chunk_index,
        maximum_buffer_bytes=maximum_buffer_bytes,
    )


def decrypt_backup_stream(
    source: BinaryReader,
    destination: BinaryWriter,
    *,
    key: bytes,
    maximum_chunk_size: int,
) -> BackupStreamObservation:
    try:
        header = _read_exact(source, _BACKUP_HEADER.size)
        magic, chunk_size, nonce_prefix = _BACKUP_HEADER.unpack(header)
        if magic != _BACKUP_MAGIC or chunk_size < 4 * 1024 or chunk_size > maximum_chunk_size:
            raise BackupStreamIntegrityError
        cipher = AESGCM(key)
        plaintext_bytes = 0
        expected_index = 0
        maximum_buffer_bytes = 0
        while True:
            record = _read_exact(source, _BACKUP_RECORD.size)
            chunk_index, final_flag, ciphertext_size = _BACKUP_RECORD.unpack(record)
            if chunk_index != expected_index or final_flag not in {0, 1}:
                raise BackupStreamIntegrityError
            if final_flag == 1:
                if ciphertext_size != 16:
                    raise BackupStreamIntegrityError
            elif not 16 < ciphertext_size <= chunk_size + 16:
                raise BackupStreamIntegrityError
            ciphertext = _read_exact(source, ciphertext_size)
            metadata = _BACKUP_ASSOCIATED_DATA.pack(chunk_index, final_flag)
            plaintext = cipher.decrypt(
                _backup_nonce(nonce_prefix, chunk_index),
                ciphertext,
                header + metadata,
            )
            maximum_buffer_bytes = max(
                maximum_buffer_bytes,
                len(ciphertext) + len(plaintext),
            )
            if final_flag == 1:
                if plaintext or source.read(1):
                    raise BackupStreamIntegrityError
                return BackupStreamObservation(
                    plaintext_bytes=plaintext_bytes,
                    chunk_count=expected_index,
                    maximum_buffer_bytes=maximum_buffer_bytes,
                )
            _write_all(destination, plaintext)
            plaintext_bytes += len(plaintext)
            expected_index += 1
    except (InvalidTag, KeyError, OverflowError, struct.error, ValueError):
        raise BackupStreamIntegrityError from None


def _read_exact(source: BinaryReader, size: int) -> bytes:
    value = bytearray()
    while len(value) < size:
        chunk = source.read(size - len(value))
        if not chunk:
            raise BackupStreamIntegrityError
        value.extend(chunk)
    return bytes(value)


def _write_all(destination: BinaryWriter, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        written = destination.write(value[offset:])
        if type(written) is not int or written <= 0 or written > len(value) - offset:
            raise BackupStreamIntegrityError
        offset += written


def _backup_nonce(prefix: bytes, chunk_index: int) -> bytes:
    try:
        return prefix + chunk_index.to_bytes(8, "big")
    except OverflowError:
        raise BackupStreamIntegrityError from None


__all__ = [
    "BackupStreamIntegrityError",
    "BackupStreamObservation",
    "BinaryReader",
    "BinaryWriter",
    "decrypt_backup_stream",
    "encrypt_backup_stream",
]
