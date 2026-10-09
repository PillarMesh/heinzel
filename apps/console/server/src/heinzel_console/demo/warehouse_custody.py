"""Where the demonstration keeps the secrets its warehouse was created with.

Provisioning mints eight principal passwords, a TLS key pair and a backup encryption key, and
rotates the warehouse's administering login to one of them. Until now they were held in the
process that minted them and written nowhere, so a restart adopted a `ready` binding holding
none of the credentials that warehouse accepts: it could report the binding and nothing else,
and every capability that needs to reach the warehouse answered `not_delivered`.

They are kept now, in warehouse-control's own encrypted operation secret store -- the one that
service already implements, locks, retires and tests. The demonstration composes it rather than
keeping a second kind of secret file, so what a restart reads back is what a deployment's
warehouse-control would hand its provider.

The Fernet key lives beside the store rather than inside it, under the same owner-only creation
the cursor cipher's key uses. That is a demonstration's answer and not a deployment's: a key in
the directory it protects is a key an attacker who reached the directory already has. A
deployment puts it in a key management service, which is the difference between this and
custody.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from cryptography.fernet import Fernet
from heinzel_warehouse_control import (
    WarehouseSecretAuthority,
    WarehouseSecretStorageError,
    open_encrypted_secret_authority,
)
from pydantic import SecretStr

from .secret_files import create_secret_file, read_secret_file

__all__ = [
    "WAREHOUSE_SECRET_DIRECTORY_NAME",
    "WAREHOUSE_SECRET_KEY_FILENAME",
    "new_warehouse_operation_id",
    "open_demo_warehouse_secret_authority",
]

# Named rather than nested among the state directory's SQLite databases, for the same reason
# the cursor key is: these two files are the ones that must never be copied anywhere.
WAREHOUSE_SECRET_KEY_FILENAME: Final = "warehouse-secret-key"
WAREHOUSE_SECRET_DIRECTORY_NAME: Final = "warehouse-secrets"

# `Fernet.generate_key` is 32 random bytes in URL-safe base64, which is always this long. Read
# back at a fixed length so a truncated file is seen as one rather than used as a short key.
_KEY_LENGTH: Final = 44
# `wop-` and twenty-four hex characters, which is what warehouse-control's store admits.
_OPERATION_ID_BYTES: Final = 12


def new_warehouse_operation_id() -> str:
    """One identifier per provisioning, in the shape the secret store requires."""
    return f"wop-{os.urandom(_OPERATION_ID_BYTES).hex()}"


def open_demo_warehouse_secret_authority(state_dir: Path) -> WarehouseSecretAuthority:
    """The authority this demonstration stores its warehouse operation secrets in.

    Opened against a key that is created on first use and read back on every start after it, so
    a warehouse provisioned by one start is administrable by the next.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    directory = state_dir / WAREHOUSE_SECRET_DIRECTORY_NAME
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return open_encrypted_secret_authority(
        directory=directory, key=SecretStr(_key(state_dir / WAREHOUSE_SECRET_KEY_FILENAME))
    )


def _key(key_path: Path) -> str:
    stored = read_secret_file(key_path, length=_KEY_LENGTH, refuse=_refuse_to_load)
    if stored is not None:
        return stored.decode("ascii")
    key = Fernet.generate_key()
    try:
        create_secret_file(key_path, key, refuse=_refuse_to_store)
    except FileExistsError:
        # Two consoles over one state directory raced on first use. The loser adopts the
        # winner's key and discards its own, which protects nothing yet.
        adopted = read_secret_file(key_path, length=_KEY_LENGTH, refuse=_refuse_to_load)
        if adopted is None:
            raise _refuse_to_load() from None
        return adopted.decode("ascii")
    return key.decode("ascii")


def _refuse_to_load() -> Exception:
    return WarehouseSecretStorageError()


def _refuse_to_store() -> Exception:
    return WarehouseSecretStorageError()
