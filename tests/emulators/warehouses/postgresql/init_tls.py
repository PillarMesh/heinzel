from __future__ import annotations

import argparse
import ipaddress
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


@dataclass(frozen=True, slots=True)
class PostgreSQLTLSMaterial:
    private_key_bundle: str
    certificate_bundle: str


def generate_tls_material(*, now: datetime | None = None) -> PostgreSQLTLSMaterial:
    observed_at = datetime.now(UTC) if now is None else now
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PillarMesh local warehouse CA")])
    ca_certificate = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(observed_at - timedelta(days=1))
        .not_valid_after(observed_at + timedelta(days=7))
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
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_certificate = _leaf_certificate(
        common_name="localhost",
        public_key=server_key.public_key(),
        ca_name=ca_name,
        ca_key=ca_key,
        observed_at=observed_at,
        extended_usage=ExtendedKeyUsageOID.SERVER_AUTH,
        subject_alternative_name=x509.SubjectAlternativeName(
            [
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]
        ),
    )
    client_key = ec.generate_private_key(ec.SECP256R1())
    client_certificate = _leaf_certificate(
        common_name="pillarmesh-warehouse-client",
        public_key=client_key.public_key(),
        ca_name=ca_name,
        ca_key=ca_key,
        observed_at=observed_at,
        extended_usage=ExtendedKeyUsageOID.CLIENT_AUTH,
        subject_alternative_name=None,
    )
    return PostgreSQLTLSMaterial(
        private_key_bundle=json.dumps(
            {
                "schema_version": "1",
                "server_private_key_pem": _private_key_pem(server_key),
                "client_private_key_pem": _private_key_pem(client_key),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        certificate_bundle=json.dumps(
            {
                "schema_version": "1",
                "ca_certificate_pem": _certificate_pem(ca_certificate),
                "server_certificate_pem": _certificate_pem(server_certificate),
                "client_certificate_pem": _certificate_pem(client_certificate),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def write_tls_material(directory: Path, material: PostgreSQLTLSMaterial) -> None:
    directory.mkdir(mode=0o700)
    if directory.is_symlink() or directory.stat().st_mode & 0o777 != 0o700:
        raise ValueError("TLS output directory must be an owner-only non-symlink directory")
    private_keys = json.loads(material.private_key_bundle)
    certificates = json.loads(material.certificate_bundle)
    files = {
        "server.key": private_keys["server_private_key_pem"],
        "client.key": private_keys["client_private_key_pem"],
        "server.crt": certificates["server_certificate_pem"],
        "client.crt": certificates["client_certificate_pem"],
        "ca.crt": certificates["ca_certificate_pem"],
    }
    for name, value in files.items():
        path = directory / name
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="ascii", closefd=True) as destination:
            destination.write(value)


def _leaf_certificate(
    *,
    common_name: str,
    public_key: ec.EllipticCurvePublicKey,
    ca_name: x509.Name,
    ca_key: ec.EllipticCurvePrivateKey,
    observed_at: datetime,
    extended_usage: x509.ObjectIdentifier,
    subject_alternative_name: x509.SubjectAlternativeName | None,
) -> x509.Certificate:
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(ca_name)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(observed_at - timedelta(days=1))
        .not_valid_after(observed_at + timedelta(days=7))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([extended_usage]), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
    )
    if subject_alternative_name is not None:
        builder = builder.add_extension(subject_alternative_name, critical=False)
    return builder.sign(ca_key, hashes.SHA256())


def _private_key_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def _certificate_pem(certificate: x509.Certificate) -> str:
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True, type=Path)
    arguments = parser.parse_args()
    write_tls_material(arguments.directory, generate_tls_material())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
