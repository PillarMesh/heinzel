"""The dbt profile describes the connection the rest of the demonstration already uses.

dbt reaches the warehouse as its own process, over its own profile, so it is the one client whose
transport is written down rather than passed along. A profile that names a transport of its own
decides for dbt what every other client of the same warehouse was told something different about,
and the disagreement surfaces as missing materialization evidence rather than as a connection the
server was never going to accept.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from heinzel_console.demo.materialization import write_demo_dbt_profile

_PLAINTEXT = "postgresql://postgres:secret@warehouse:5432/heinzel"
_MUTUAL_TLS = (
    "postgresql://postgres:secret@127.0.0.1:47369/heinzel_warehouse"
    "?sslmode=verify-full&sslrootcert=/private/ca.crt"
    "&sslcert=/private/client.crt&sslkey=/private/client.key"
)


def _output(directory: Path, bootstrap_dsn: str) -> dict[str, Any]:
    write_demo_dbt_profile(directory, bootstrap_dsn, target_schema="product")
    profile = yaml.safe_load((directory / "profiles.yml").read_text(encoding="utf-8"))
    outputs = profile["heinzel_materialization"]["outputs"]["postgresql"]
    assert isinstance(outputs, dict)
    return outputs


def test_the_profile_carries_the_mutual_tls_its_warehouse_requires(tmp_path: Path) -> None:
    """A warehouse-control warehouse serves `hostssl` with `clientcert=verify-ca`.

    It rejects plaintext through its own `pg_hba.conf`, so dbt must present the same certificate
    and verify the same authority as every other client of that warehouse.
    """
    output = _output(tmp_path, _MUTUAL_TLS)

    assert output["sslmode"] == "verify-full"
    assert output["sslrootcert"] == "/private/ca.crt"
    assert output["sslcert"] == "/private/client.crt"
    assert output["sslkey"] == "/private/client.key"


def test_a_connection_that_names_no_transport_still_leaves_dbt_a_profile_it_can_use(
    tmp_path: Path,
) -> None:
    """The demonstration's own warehouse is reached over a container network nothing else is on.

    Its PostgreSQL serves no certificate any name the console could use would verify against, so
    the absence of `sslmode` on the connection stays the absence of TLS in the profile, and no
    certificate setting is invented for files that do not exist.
    """
    output = _output(tmp_path, _PLAINTEXT)

    assert output["sslmode"] == "disable"
    assert "sslrootcert" not in output
    assert "sslcert" not in output
    assert "sslkey" not in output


def test_the_profile_never_writes_the_password_it_names(tmp_path: Path) -> None:
    """The credential is passed to dbt as an environment variable, not left on disk."""
    write_demo_dbt_profile(tmp_path, _MUTUAL_TLS, target_schema="product")
    written = (tmp_path / "profiles.yml").read_text(encoding="utf-8")

    assert "secret" not in written
    assert "env_var(" in written
