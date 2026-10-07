"""Short-lived TLS material for a warehouse the demonstration provisions through
warehouse-control.

`PostgreSQLWarehouseProvider` takes its server and client TLS material as two secret
capabilities carrying JSON bundles, writes them into the warehouse's private directory, and
then connects `sslmode=verify-full` with a client certificate. A deployment resolves those
capabilities from a key custody it answers for. This demonstration has none, so it mints a
throwaway certificate authority per start and holds the keys in the process that uses them --
the same posture `demo/warehouse.py` already takes with the warehouse's role passwords, and
the reason nothing here is ever written to the state volume.

The two bundles are the provider's own format, so the test beside this module parses what is
generated here with the provider's own models rather than with a copy of the field names.
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

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

__all__ = ["DemoWarehouseTLSMaterial", "generate_demo_warehouse_tls_material"]

# How long the minted certificates are good for. A demonstration that ran for longer than this
# would have to mint again, and the provider holds the material for one warehouse's lifetime,
# so this is a bound on one demonstration rather than a rotation period.
_VALIDITY = timedelta(days=7)
# Clock skew between this process and the container it issues for.
_BACKDATE = timedelta(days=1)

# The provider reaches the warehouse on the loopback address its Compose project publishes, so
# these are the two names every server certificate here carries. They are not the only two a
# deployment needs: a client in another container reaches the warehouse by its name on their shared
# network, and `verify-full` checks that name against this certificate -- so a caller that will
# connect that way names it through `internal_hostnames`.
_SERVER_COMMON_NAME = "localhost"
_LOOPBACK_ADDRESS = "127.0.0.1"
_CLIENT_COMMON_NAME = "heinzel-demonstration-warehouse-client"
_AUTHORITY_COMMON_NAME = "Heinzel demonstration warehouse CA"


@dataclass(frozen=True, slots=True)
class DemoWarehouseTLSMaterial:
    """The two JSON bundles the provider's TLS secret capabilities resolve to."""

    private_key_bundle: str
    certificate_bundle: str


def generate_demo_warehouse_tls_material(
    *, now: datetime, internal_hostnames: tuple[str, ...] = ()
) -> DemoWarehouseTLSMaterial:
    """Mint one authority, one server certificate and one client certificate.

    Elliptic-curve keys rather than RSA: the material is minted on every start, and a
    demonstration that spent a second generating RSA keys before it listened would be paying
    that second for nothing.

    `internal_hostnames` are further names the server certificate must cover, for a client that
    reaches the warehouse on a container network rather than through the published loopback port.
    Omitted, the certificate carries only `localhost` and `127.0.0.1`, and such a client fails
    hostname verification rather than connecting to something unverified.
    """
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
                x509.IPAddress(ipaddress.ip_address(_LOOPBACK_ADDRESS)),
                *(x509.DNSName(name) for name in dict.fromkeys(internal_hostnames)),
            ]
        ),
    )
    client_key = ec.generate_private_key(ec.SECP256R1())
    client_certificate = leaf_certificate(
        common_name=_CLIENT_COMMON_NAME,
        public_key=client_key.public_key(),
        authority_name=authority_name,
        authority_key=authority_key,
        observed_at=observed_at,
        extended_usage=ExtendedKeyUsageOID.CLIENT_AUTH,
        subject_alternative_name=None,
    )
    return DemoWarehouseTLSMaterial(
        private_key_bundle=_canonical_json(
            {
                "schema_version": "1",
                "server_private_key_pem": private_key_pem(server_key),
                "client_private_key_pem": private_key_pem(client_key),
            }
        ),
        certificate_bundle=_canonical_json(
            {
                "schema_version": "1",
                "ca_certificate_pem": certificate_pem(authority),
                "server_certificate_pem": certificate_pem(server_certificate),
                "client_certificate_pem": certificate_pem(client_certificate),
            }
        ),
    )


def _canonical_json(payload: dict[str, str]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
