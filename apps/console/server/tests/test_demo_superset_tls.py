from __future__ import annotations

import ipaddress
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID
from heinzel_console.demo.superset_tls import (
    SUPERSET_AUTHORITY_FILENAME,
    SUPERSET_SERVER_CERTIFICATE_FILENAME,
    SUPERSET_SERVER_KEY_FILENAME,
    demo_superset_tls_material,
    ensure_demo_superset_tls_material,
)
from heinzel_console.demo.warehouse import DEMO_WAREHOUSE_ROLES

NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)


def _certificate(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def test_the_minted_certificate_names_every_host_the_demonstration_reaches_it_by(
    tmp_path: Path,
) -> None:
    material = ensure_demo_superset_tls_material(tmp_path / "tls", now=NOW)

    names = _certificate(material.server_certificate_path).extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    )
    assert set(names.value.get_values_for_type(x509.DNSName)) == {"superset", "localhost"}
    assert names.value.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address("127.0.0.1")]


def test_the_minted_certificate_is_a_server_certificate_under_its_own_authority(
    tmp_path: Path,
) -> None:
    material = ensure_demo_superset_tls_material(tmp_path / "tls", now=NOW)

    server = _certificate(material.server_certificate_path)
    authority = _certificate(material.authority_path)
    usage = server.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(usage) == [ExtendedKeyUsageOID.SERVER_AUTH]
    assert server.issuer == authority.subject
    assert authority.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True
    assert server.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False


def test_the_authority_loads_into_a_real_trust_store(tmp_path: Path) -> None:
    material = ensure_demo_superset_tls_material(tmp_path / "tls", now=NOW)

    # The console verifies Superset with exactly this, so a file that is not a loadable authority
    # would fail at the first publication rather than here.
    context = ssl.create_default_context(cafile=str(material.authority_path))
    assert context.get_ca_certs()


def test_material_already_minted_is_left_alone_so_superset_keeps_serving_it(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "tls"
    first = ensure_demo_superset_tls_material(directory, now=NOW)
    contents = {
        path.name: path.read_bytes()
        for path in (first.authority_path, first.server_certificate_path, first.server_key_path)
    }

    second = ensure_demo_superset_tls_material(directory, now=NOW + timedelta(days=1))

    assert second == first
    assert {
        path.name: path.read_bytes()
        for path in (second.authority_path, second.server_certificate_path, second.server_key_path)
    } == contents


def test_a_directory_missing_its_authority_is_minted_again(tmp_path: Path) -> None:
    directory = tmp_path / "tls"
    first = ensure_demo_superset_tls_material(directory, now=NOW)
    original = first.server_certificate_path.read_bytes()
    first.authority_path.unlink()

    second = ensure_demo_superset_tls_material(directory, now=NOW)

    assert second.complete
    assert second.server_certificate_path.read_bytes() != original


def test_an_issue_time_without_a_utc_offset_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        ensure_demo_superset_tls_material(tmp_path / "tls", now=datetime(2026, 10, 7, 12))


def test_the_material_is_readable_by_the_superset_user_that_is_not_the_console(
    tmp_path: Path,
) -> None:
    material = ensure_demo_superset_tls_material(tmp_path / "tls", now=NOW)

    # Shared deliberately: Superset runs as its own user, so a key only this process could read
    # would be a Superset that cannot start. See the module docstring for why that is acceptable
    # here and nowhere else.
    for path in (
        material.authority_path,
        material.server_certificate_path,
        material.server_key_path,
    ):
        assert path.stat().st_mode & 0o044 == 0o044


def test_where_the_material_lives_is_known_before_it_is_minted(tmp_path: Path) -> None:
    material = demo_superset_tls_material(tmp_path / "tls")

    assert material.complete is False
    assert material.authority_path.name == SUPERSET_AUTHORITY_FILENAME
    assert material.server_certificate_path.name == SUPERSET_SERVER_CERTIFICATE_FILENAME
    assert material.server_key_path.name == SUPERSET_SERVER_KEY_FILENAME


def test_the_dashboard_reader_is_a_login_of_its_own_and_not_the_answer_runtime() -> None:
    """ADR-0007: the runtime's service principal is not a requester grant.

    A BI provider connecting as `answer_runtime` would make a database login stand in for an
    access decision, so the demonstration provisions a sixth role rather than reusing one.
    """
    names = tuple(DEMO_WAREHOUSE_ROLES)

    assert len(names) == len(set(names)) == 6
    assert DEMO_WAREHOUSE_ROLES.dashboard == "dashboard_reader"
    assert DEMO_WAREHOUSE_ROLES.dashboard not in {
        DEMO_WAREHOUSE_ROLES.answer,
        DEMO_WAREHOUSE_ROLES.estimator,
    }
