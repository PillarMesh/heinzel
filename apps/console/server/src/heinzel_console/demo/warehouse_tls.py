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
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

__all__ = ["DemoWarehouseTLSMaterial", "generate_demo_warehouse_tls_material"]

# How long the minted certificates are good for. A demonstration that ran for longer than this
# would have to mint again, and the provider holds the material for one warehouse's lifetime,
# so this is a bound on one demonstration rather than a rotation period.
_VALIDITY = timedelta(days=7)
# Clock skew between this process and the container it issues for.
_BACKDATE = timedelta(days=1)

# The provider reaches the warehouse on the loopback address its Compose project publishes, so
# these are the only two names a server certificate has to carry.
_SERVER_COMMON_NAME = "localhost"
_LOOPBACK_ADDRESS = "127.0.0.1"
_CLIENT_COMMON_NAME = "heinzel-demonstration-warehouse-client"
_AUTHORITY_COMMON_NAME = "Heinzel demonstration warehouse CA"


@dataclass(frozen=True, slots=True)
class DemoWarehouseTLSMaterial:
    """The two JSON bundles the provider's TLS secret capabilities resolve to."""

    private_key_bundle: str
    certificate_bundle: str


def generate_demo_warehouse_tls_material(*, now: datetime) -> DemoWarehouseTLSMaterial:
    """Mint one authority, one server certificate and one client certificate.

    Elliptic-curve keys rather than RSA: the material is minted on every start, and a
    demonstration that spent a second generating RSA keys before it listened would be paying
    that second for nothing.
    """
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("the TLS material's issue time must be timezone-aware UTC")
    observed_at = now.astimezone(UTC)
    authority_key = ec.generate_private_key(ec.SECP256R1())
    authority_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _AUTHORITY_COMMON_NAME)])
    authority_certificate = (
        _builder(
            subject=authority_name,
            issuer=authority_name,
            public_key=authority_key.public_key(),
            observed_at=observed_at,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(authority_key, hashes.SHA256())
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_certificate = _leaf_certificate(
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
            ]
        ),
    )
    client_key = ec.generate_private_key(ec.SECP256R1())
    client_certificate = _leaf_certificate(
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
                "server_private_key_pem": _private_key_pem(server_key),
                "client_private_key_pem": _private_key_pem(client_key),
            }
        ),
        certificate_bundle=_canonical_json(
            {
                "schema_version": "1",
                "ca_certificate_pem": _certificate_pem(authority_certificate),
                "server_certificate_pem": _certificate_pem(server_certificate),
                "client_certificate_pem": _certificate_pem(client_certificate),
            }
        ),
    )


def _builder(
    *,
    subject: x509.Name,
    issuer: x509.Name,
    public_key: ec.EllipticCurvePublicKey,
    observed_at: datetime,
) -> x509.CertificateBuilder:
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(observed_at - _BACKDATE)
        .not_valid_after(observed_at + _VALIDITY)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
    )


def _leaf_certificate(
    *,
    common_name: str,
    public_key: ec.EllipticCurvePublicKey,
    authority_name: x509.Name,
    authority_key: ec.EllipticCurvePrivateKey,
    observed_at: datetime,
    extended_usage: x509.ObjectIdentifier,
    subject_alternative_name: x509.SubjectAlternativeName | None,
) -> x509.Certificate:
    builder = (
        _builder(
            subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]),
            issuer=authority_name,
            public_key=public_key,
            observed_at=observed_at,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([extended_usage]), critical=True)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(authority_key.public_key()),
            critical=False,
        )
    )
    if subject_alternative_name is not None:
        builder = builder.add_extension(subject_alternative_name, critical=False)
    return builder.sign(authority_key, hashes.SHA256())


def _canonical_json(payload: dict[str, str]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _private_key_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def _certificate_pem(certificate: x509.Certificate) -> str:
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
