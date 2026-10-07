"""Minting the short-lived certificates the demonstration's own containers serve.

Two of the demonstration's collaborators are reached over TLS: a warehouse provisioned through
warehouse-control, and the Superset it publishes dashboards to. Both need an authority, a server
certificate under it, and PEM encodings of each; neither needs a certificate that outlives the
demonstration that minted it.

This is the one place those are built, so the validity window, the backdating for clock skew and
the extensions are decided once. Elliptic-curve keys rather than RSA throughout: the material is
minted on every start, and a demonstration that spent a second generating RSA keys before it
listened would be paying that second for nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

__all__ = [
    "authority_certificate",
    "certificate_pem",
    "leaf_certificate",
    "private_key_pem",
    "utc_issue_time",
]

# How long the minted certificates are good for. A demonstration that ran for longer than this
# would have to mint again, so this bounds one demonstration rather than setting a rotation period.
_VALIDITY = timedelta(days=7)
# Clock skew between this process and the container it issues for.
_BACKDATE = timedelta(days=1)


def utc_issue_time(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("the TLS material's issue time must be timezone-aware UTC")
    return now.astimezone(UTC)


def authority_certificate(
    *,
    common_name: str,
    key: ec.EllipticCurvePrivateKey,
    observed_at: datetime,
) -> tuple[x509.Name, x509.Certificate]:
    """A self-signed authority that may sign leaves and nothing below them."""
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        _builder(subject=name, issuer=name, public_key=key.public_key(), observed_at=observed_at)
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
        .sign(key, hashes.SHA256())
    )
    return name, certificate


def leaf_certificate(
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


def private_key_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def certificate_pem(certificate: x509.Certificate) -> str:
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


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
