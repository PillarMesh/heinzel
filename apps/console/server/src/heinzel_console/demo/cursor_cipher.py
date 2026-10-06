"""The cursor cipher the demonstration console seals its source cursors with.

`SQLiteAcquisitionStateRepository` never stores a source cursor in the clear: it takes a
`CursorCipher` as a collaborator and persists only what that returns. A deployment injects the
cipher its own key custody answers for. A local demonstration has none, so this is one it can
run: AES-GCM under a 256-bit key kept beside the demonstration's other state.

What this stands in for is the key custodian, not the cipher. The construction is the real one --
a fresh random nonce per encryption, and the owning tenant bound into the authenticated data, so
a cursor one tenant wrote cannot be opened as another's. What it does not stand in for is a key
management service: the key is a file under the demonstration's state directory, readable by
whoever can read that directory, it is never rotated, and deleting it discards every cursor ever
sealed under it.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from heinzel_state import CursorCipherError

__all__ = ["CURSOR_KEY_FILENAME", "DemoCursorCipher"]

# Named rather than nested in the state directory's other files: the demonstration's stores are
# SQLite databases, and the one file among them that must never be copied anywhere is this one.
CURSOR_KEY_FILENAME = "cursor-cipher-key"

_KEY_BYTES = 32
# 96 bits, the nonce length AES-GCM is specified for and the only one that needs no rehashing.
_NONCE_BYTES = 12
# Sealed into every ciphertext ahead of the tenant, so that a key which somehow came to be used
# for anything else could not produce something this accepts as one of its cursors.
_AUTHENTICATED_DOMAIN = b"heinzel-demo-cursor-v1"

_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)


class DemoCursorCipher:
    """Seals and opens source cursors under a key persisted in the state directory.

    Satisfies `heinzel_state.CursorCipher`. The key is resolved once, when the cipher is
    constructed, rather than per call: the repository encrypts and decrypts on every
    acquisition, and a cursor that stopped opening because the key file changed underneath a
    running console would be indistinguishable from a corrupt one.
    """

    def __init__(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        self._cipher = AESGCM(_resolve_key(state_dir / CURSOR_KEY_FILENAME))

    def encrypt(self, *, tenant_id: str, plaintext: bytes) -> bytes:
        # A fresh nonce per encryption, from the operating system's generator, and never a
        # counter: a nonce repeated under one key forfeits both confidentiality and the
        # authentication that binds the ciphertext to its tenant. The key is replaced only by
        # deleting its file, which discards every cursor sealed under it, so no nonce this
        # draws can collide with one drawn under a different key.
        nonce = os.urandom(_NONCE_BYTES)
        # Nothing `cryptography` raises here is one of its own exception types -- an oversized
        # plaintext is `OverflowError` -- so there is nothing to translate. `decrypt` below is
        # where that matters.
        return nonce + self._cipher.encrypt(nonce, plaintext, _authenticated_data(tenant_id))

    def decrypt(self, *, tenant_id: str, ciphertext: bytes) -> bytes:
        nonce, sealed = ciphertext[:_NONCE_BYTES], ciphertext[_NONCE_BYTES:]
        if len(nonce) != _NONCE_BYTES:
            raise CursorCipherError(operation="decrypt cursor")
        try:
            return self._cipher.decrypt(nonce, sealed, _authenticated_data(tenant_id))
        except InvalidTag:
            # Tampering, truncation, the wrong tenant and the wrong key are one failure here,
            # deliberately: GCM authenticates before it releases anything, so none of them can
            # return bytes that were not sealed under this key for this tenant. The driver's
            # exception type stays inside this module; the repository contracts on
            # `CursorCipherError`.
            raise CursorCipherError(operation="decrypt cursor") from None


def _authenticated_data(tenant_id: str) -> bytes:
    """What every cursor is sealed against: this cipher's domain, then the owning tenant.

    GCM authenticates this without storing it, so a cursor sealed for one tenant fails to open
    as another's instead of yielding the wrong cursor -- the one outcome a repository reading a
    tenant's checkpoint must never see. The domain is a fixed prefix containing no NUL, so no
    domain and tenant pair can collide with another across the separator.
    """
    return _AUTHENTICATED_DOMAIN + b"\x00" + tenant_id.encode("utf-8")


def _resolve_key(key_path: Path) -> bytes:
    """Return the key at `key_path`, generating it on first use.

    The ciphertext outlives the process that wrote it: it is committed to the demonstration's
    SQLite state, and the next start must still open it. So the key is made durable before it
    seals anything -- written, fsynced, and its name fsynced into the directory -- rather than
    generated per run, which would leave every committed cursor unopenable.
    """
    stored = _read_key(key_path)
    if stored is not None:
        return stored
    key = AESGCM.generate_key(bit_length=_KEY_BYTES * 8)
    try:
        _create_key_file(key_path, key)
    except FileExistsError as error:
        # Two consoles over one state directory raced on first use. The loser adopts the
        # winner's key and discards its own, which sealed nothing.
        adopted = _read_key(key_path)
        if adopted is None:
            raise CursorCipherError(operation="load the demonstration cursor key") from error
        return adopted
    return key


def _read_key(key_path: Path) -> bytes | None:
    """The key stored at `key_path`, or `None` when the demonstration has not sealed one yet.

    Any other failure to read it is the filesystem's, and is reported as the `OSError` it is:
    that names the path and the reason, which `CursorCipherError` does not.
    """
    try:
        descriptor = os.open(key_path, _READ_FLAGS)
    except FileNotFoundError:
        return None
    # One byte more than a key, so that an overlong file is seen as one rather than silently
    # truncated to something that looks like a key.
    with os.fdopen(descriptor, "rb") as key_file:
        stored = key_file.read(_KEY_BYTES + 1)
    if len(stored) != _KEY_BYTES:
        # A short or overlong key file is not a key. Refusing is the only safe answer: carrying
        # on would either fail inside the driver or seal new cursors under something that is
        # not the key the committed ones were sealed under.
        raise CursorCipherError(operation="load the demonstration cursor key")
    return stored


def _create_key_file(key_path: Path, key: bytes) -> None:
    """Create the key file readable only by its owner, or raise `FileExistsError`.

    The mode is set by `os.open` rather than by a later `chmod`: a file written first and
    restricted second is readable by everyone for as long as it takes to reach the second call.
    """
    descriptor = os.open(key_path, _CREATE_FLAGS, 0o600)
    try:
        written = os.write(descriptor, key)
        if written != len(key):
            # A partial key file is not a key, and the next start will say so rather than seal
            # cursors under half of one.
            raise CursorCipherError(operation="store the demonstration cursor key")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    # The contents are durable above; this is the file's name. Without the directory's own
    # fsync a crash can lose the name while keeping the cursors already committed beside it.
    directory_descriptor = os.open(key_path.parent, _DIRECTORY_FLAGS)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
