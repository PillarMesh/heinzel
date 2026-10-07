"""The TLS material the demonstration's Superset serves, minted by the console that reaches it.

`SupersetCredentials` refuses a base URL that is not HTTPS, so a Superset the demonstration can
publish to has to serve TLS even on a Compose network nothing outside can reach. There is no key
custody here to resolve that material from, so the console mints a throwaway authority on startup
and writes it where both containers can read it.

The private key is written world-readable inside that volume, which would be wrong anywhere else
and is stated here rather than hidden: the volume is shared by exactly these two containers of one
local demonstration, the key is minted per start and never leaves the volume, and Superset runs as
its own user so a key only this process could read would be a Superset that cannot start. A
deployment resolves server material from key custody it answers for, as `demo/warehouse_tls.py`
says of its own bundles.

The material is minted once and then left alone. Superset keeps serving the certificate it started
with, so a second start that replaced it would leave the console trusting an authority that no
longer signs what answers it.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

from .tls_material import (
    authority_certificate,
    certificate_pem,
    leaf_certificate,
    private_key_pem,
    utc_issue_time,
)

__all__ = [
    "SUPERSET_AUTHORITY_FILENAME",
    "SUPERSET_SERVER_CERTIFICATE_FILENAME",
    "SUPERSET_SERVER_KEY_FILENAME",
    "DemoSupersetTLSMaterial",
    "demo_superset_tls_material",
    "ensure_demo_superset_tls_material",
]

SUPERSET_AUTHORITY_FILENAME = "ca.crt"
SUPERSET_SERVER_CERTIFICATE_FILENAME = "server.crt"
SUPERSET_SERVER_KEY_FILENAME = "server.key"

# The names the certificate has to carry: the Compose service the console reaches it by, and the
# loopback address a browser on the host reaches the published port by. A certificate missing
# either produces a verification failure that reads as an unreachable Superset.
_SERVER_COMMON_NAME = "superset"
_ALTERNATIVE_DNS_NAME = "localhost"
_LOOPBACK_ADDRESS = "127.0.0.1"
_AUTHORITY_COMMON_NAME = "Heinzel demonstration Superset CA"
# Readable by the Superset user, which is not the console's. See this module's docstring.
_SHARED_MODE = 0o644


@dataclass(frozen=True, slots=True)
class DemoSupersetTLSMaterial:
    """Where the minted material was written, for whoever has to trust or serve it."""

    authority_path: Path
    server_certificate_path: Path
    server_key_path: Path

    @property
    def complete(self) -> bool:
        return all(
            path.is_file()
            for path in (self.authority_path, self.server_certificate_path, self.server_key_path)
        )


def demo_superset_tls_material(directory: Path) -> DemoSupersetTLSMaterial:
    """Where the material lives, whether or not it has been minted."""
    return DemoSupersetTLSMaterial(
        authority_path=directory / SUPERSET_AUTHORITY_FILENAME,
        server_certificate_path=directory / SUPERSET_SERVER_CERTIFICATE_FILENAME,
        server_key_path=directory / SUPERSET_SERVER_KEY_FILENAME,
    )


def ensure_demo_superset_tls_material(directory: Path, *, now: datetime) -> DemoSupersetTLSMaterial:
    """Mint the authority and server certificate on first use, and leave them alone after.

    Written whole rather than file by file: a Superset that found a certificate without its key, or
    a console that trusted an authority that had not signed the certificate beside it, would report
    a verification failure that says nothing about a half-written directory.
    """
    directory.mkdir(parents=True, exist_ok=True)
    material = demo_superset_tls_material(directory)
    if material.complete:
        return material
    observed_at = utc_issue_time(now)
    authority_key = ec.generate_private_key(ec.SECP256R1())
    authority_name, authority = authority_certificate(
        common_name=_AUTHORITY_COMMON_NAME, key=authority_key, observed_at=observed_at
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_certificate = leaf_certificate(
        common_name=_SERVER_COMMON_NAME,
        public_key=server_key.public_key(),
        authority_name=authority_name,
        authority_key=authority_key,
        observed_at=observed_at,
        extended_usage=ExtendedKeyUsageOID.SERVER_AUTH,
        subject_alternative_name=x509.SubjectAlternativeName(
            [
                x509.DNSName(_SERVER_COMMON_NAME),
                x509.DNSName(_ALTERNATIVE_DNS_NAME),
                x509.IPAddress(ipaddress.ip_address(_LOOPBACK_ADDRESS)),
            ]
        ),
    )
    # The key first and the authority last, so a start interrupted partway leaves the authority
    # absent -- which is what `complete` reads as "not minted" and mints again, rather than a
    # console that trusts an authority whose server material never arrived.
    _write(material.server_key_path, private_key_pem(server_key))
    _write(material.server_certificate_path, certificate_pem(server_certificate))
    _write(material.authority_path, certificate_pem(authority))
    return material


def _write(path: Path, contents: str) -> None:
    path.write_text(contents, encoding="ascii")
    path.chmod(_SHARED_MODE)
