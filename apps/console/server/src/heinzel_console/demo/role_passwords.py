"""The warehouse role passwords, the same ones on every start.

They used to be minted per start and written nowhere, which worked because the administering
login resets them into PostgreSQL on the way up. What it cost is everything downstream that
needs a connection detail to stay the same: a source connection is enrolled under a handle and
the enrolment is immutable, so a DSN carrying a password that changed overnight is refused as a
different detail for a handle already validated -- and the demonstration therefore enrols no
source at all.

One root secret is kept instead, and each role's password is derived from it by name. Derived
rather than stored per role so that a role added to `DemoWarehouseRoles` cannot be left without
one, which is the property the minting had and worth keeping; and from the role's name so that
two roles never share a password even though one secret is behind both.

The root is a file under the state directory, owner-only from the instant it exists. That is
the demonstration's answer and not a deployment's, which resolves each role's credential from a
secret manager at the moment it connects.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from pathlib import Path
from typing import Final

from .secret_files import create_secret_file, read_secret_file
from .warehouse import DemoWarehouseRoles

__all__ = ["ROLE_PASSWORD_ROOT_FILENAME", "role_passwords"]

ROLE_PASSWORD_ROOT_FILENAME: Final = "warehouse-role-root"

_ROOT_BYTES: Final = 32
# Separated from anything else this root could ever be used for, so a password derived here
# could not collide with a key derived from the same file for another purpose.
_DOMAIN: Final = b"heinzel-demo-warehouse-role-v1"
# 24 bytes, URL-safe: the same shape and strength `secrets.token_urlsafe(24)` produced.
_PASSWORD_BYTES: Final = 24


class RolePasswordsUnavailable(RuntimeError):
    """The root could not be read or created, so no role password can be derived."""


def role_passwords(state_dir: Path, roles: DemoWarehouseRoles) -> dict[str, str]:
    """One password per role, the same on every start over one state directory.

    `roles` is the role set itself rather than a list of names, so this cannot be called with a
    subset: iterating it is what guarantees every role the demonstration provisions is given a
    password, which is the whole reason the previous version derived its keys from the set.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    root = _root(state_dir / ROLE_PASSWORD_ROOT_FILENAME)
    return {role: _derive(root, role) for role in roles}


def _derive(root: bytes, role: str) -> str:
    material = hashlib.pbkdf2_hmac(
        "sha256", root, _DOMAIN + b":" + role.encode("utf-8"), 1, dklen=_PASSWORD_BYTES
    )
    return base64.urlsafe_b64encode(material).rstrip(b"=").decode("ascii")


def _root(path: Path) -> bytes:
    stored = read_secret_file(path, length=_ROOT_BYTES, refuse=_refuse)
    if stored is not None:
        return stored
    root = secrets.token_bytes(_ROOT_BYTES)
    try:
        create_secret_file(path, root, refuse=_refuse)
    except FileExistsError:
        # Two consoles over one state directory raced on first use. The loser adopts the
        # winner's root and discards its own, which nothing has been derived from yet.
        adopted = read_secret_file(path, length=_ROOT_BYTES, refuse=_refuse)
        if adopted is None:
            raise _refuse() from None
        return adopted
    return root


def _refuse() -> Exception:
    return RolePasswordsUnavailable(
        "the demonstration's warehouse role secret could not be read or created"
    )
