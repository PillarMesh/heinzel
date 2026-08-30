from __future__ import annotations

import json
from datetime import UTC, datetime

from cryptography import x509

from tests.emulators.warehouses.postgresql.init_tls import generate_tls_material


def test_generated_tls_chain_carries_authority_identifiers_required_by_python_ssl() -> None:
    material = generate_tls_material(now=datetime(2026, 8, 28, tzinfo=UTC))
    certificates = json.loads(material.certificate_bundle)
    ca_certificate = x509.load_pem_x509_certificate(
        certificates["ca_certificate_pem"].encode("ascii")
    )
    server_certificate = x509.load_pem_x509_certificate(
        certificates["server_certificate_pem"].encode("ascii")
    )

    subject_identifier = ca_certificate.extensions.get_extension_for_class(
        x509.SubjectKeyIdentifier
    ).value
    authority_identifier = server_certificate.extensions.get_extension_for_class(
        x509.AuthorityKeyIdentifier
    ).value

    assert authority_identifier.key_identifier == subject_identifier.digest
