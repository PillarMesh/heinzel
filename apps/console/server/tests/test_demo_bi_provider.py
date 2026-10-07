"""What the console decides before it ever reaches a Superset.

Publishing a dashboard needs a running Superset, and these tests have none. What they can ask for
is everything the console settles on its own: whether a deployment asked for a Superset at all,
which login Superset is given to query the product with, the exact shape of the connection string
that login reaches it by, and that the only authority the console trusts is the one it minted. Each
of those is a decision a live publication would inherit rather than make, so each is asked here.

The boundary is deliberate and not hidden: nothing below authenticates against Superset, creates a
dataset, or publishes anything. That is `tests/integration/test_superset_dashboard_live.py`, which
needs a cold Superset and is marked `live`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from cryptography import x509
from heinzel_bi_control import (
    DashboardControlService,
    DashboardProviderReceipt,
    PublishDashboardCommand,
    SQLiteDashboardRepository,
)
from heinzel_console.demo.bi_provider import (
    SUPERSET_ADMIN_PASSWORD_VARIABLE,
    SUPERSET_BASE_URL_VARIABLE,
    SUPERSET_TLS_DIRECTORY_VARIABLE,
    demo_dashboard_publication,
    demo_superset_connection,
    demo_superset_database_uri,
)
from heinzel_console.demo.superset_tls import ensure_demo_superset_tls_material
from heinzel_console.demo.warehouse import DEMO_WAREHOUSE_ROLES, ProvisioningRefused
from heinzel_console.governed_adapters import WorkflowDashboardPublicationCommands

NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)

# The warehouse as the console is given it by `deploy/quickstart/compose.yaml`: the service name
# Superset shares a network with, not the loopback address the host reaches the published port by.
BOOTSTRAP_DSN = "postgresql://postgres@warehouse:5432/heinzel"
ADMIN_PASSWORD = "superset-admin-secret"
# A password shaped like the ones the demonstration actually generates, plus every character a URI
# reserves in an authority: unescaped, each one would move the host, port, database or query.
UNSAFE_PASSWORD = "p@ss/w:rd?#[]&=+ x%2F"


def _database_uri(password: str = "reader-secret") -> str:
    return demo_superset_database_uri(
        BOOTSTRAP_DSN, role=DEMO_WAREHOUSE_ROLES.dashboard, password=password
    )


def _environment(tls_directory: Path) -> dict[str, str]:
    return {
        SUPERSET_BASE_URL_VARIABLE: "https://superset:8088",
        SUPERSET_TLS_DIRECTORY_VARIABLE: str(tls_directory),
        SUPERSET_ADMIN_PASSWORD_VARIABLE: ADMIN_PASSWORD,
    }


class _RefusingPublisher:
    """A publisher that refuses, because publishing needs the Superset these tests do not have.

    No more permissive than `DashboardCompositionService`, which it stands in for: it satisfies the
    same protocol and settles nothing. A test that reached it would be claiming a publication it
    never made, so it says so instead.
    """

    def publish(self, command: PublishDashboardCommand) -> DashboardProviderReceipt:
        raise AssertionError("publishing a dashboard needs a live Superset")


@pytest.fixture(name="dashboards")
def _dashboards(tmp_path: Path) -> Iterator[SQLiteDashboardRepository]:
    repository = SQLiteDashboardRepository(str(tmp_path / "dashboards.sqlite3"))
    try:
        yield repository
    finally:
        repository.close()


def _authority_serial(path: Path) -> int:
    """The minted authority's serial number, as an integer.

    Compared as a number rather than as the hexadecimal string OpenSSL prints: that string is
    zero-padded to an even length, so a serial whose leading nibble is zero compares unequal to
    `cryptography`'s own formatting roughly one time in sixteen.
    """
    return x509.load_pem_x509_certificate(path.read_bytes()).serial_number


def test_a_deployment_that_asked_for_no_superset_is_offered_no_publication_at_all(
    tmp_path: Path, dashboards: SQLiteDashboardRepository
) -> None:
    """An empty environment is a console that reports dashboard publication as not delivered.

    The default `docker compose up` sets none of the three variables, and the honest answer with
    no Superset to publish to is nothing -- not a seam that refuses every press.
    """
    assert demo_superset_connection({}, database_uri=_database_uri(), now=NOW) is None

    with demo_dashboard_publication(
        {},
        state_dir=tmp_path,
        dashboards=dashboards,
        database_uri=_database_uri(),
        current_answers=_NoAnswers(),
        answer_authority=_NoAnswerAuthority(),
        compose_publisher=_never_composed,
        clock=lambda: NOW,
    ) as commands:
        assert commands is None
    # Nothing was opened, so nothing was recorded: no publication database and no TLS material.
    assert list(tmp_path.iterdir()) == [tmp_path / "dashboards.sqlite3"]


@pytest.mark.parametrize(
    "variable",
    [
        SUPERSET_BASE_URL_VARIABLE,
        SUPERSET_TLS_DIRECTORY_VARIABLE,
        SUPERSET_ADMIN_PASSWORD_VARIABLE,
    ],
)
@pytest.mark.parametrize("absence", ["removed", "empty"])
def test_a_half_configured_superset_is_not_a_superset(
    tmp_path: Path, variable: str, absence: str
) -> None:
    """Each variable alone is enough to leave the console with no Superset.

    Both spellings of absence are asked, because Compose writes the empty string rather than
    unsetting the variable: `${HEINZEL_SUPERSET_ADMIN_PASSWORD:+...}` expands to `""`, so a console
    that only checked for a missing key would start against a Superset described by nothing.
    """
    environment = _environment(tmp_path / "tls")
    if absence == "removed":
        del environment[variable]
    else:
        environment[variable] = ""

    assert demo_superset_connection(environment, database_uri=_database_uri(), now=NOW) is None
    # The material is minted only once a Superset is fully described, so a partial configuration
    # leaves no certificate authority behind for an instance the console will not reach.
    assert not (tmp_path / "tls").exists()


def test_superset_queries_the_product_as_the_dashboard_reader_and_never_as_the_answer_runtime(
    tmp_path: Path,
) -> None:
    """ADR-0007: `answer_runtime` is the runtime's own service principal, not a requester grant.

    A BI provider connecting as it would make a database login stand in for an access decision,
    which is the conflation that record exists to prevent. `dashboard_reader` holds `SELECT` on the
    materialized product and nothing else.
    """
    connection = demo_superset_connection(
        _environment(tmp_path / "tls"), database_uri=_database_uri(), now=NOW
    )

    assert connection is not None
    credentials = connection.credentials
    assert urlsplit(credentials.database_uri).username == DEMO_WAREHOUSE_ROLES.dashboard
    assert DEMO_WAREHOUSE_ROLES.answer not in credentials.database_uri
    assert DEMO_WAREHOUSE_ROLES.materialization not in credentials.database_uri
    # The admin account is Superset's own API login, which is a different credential from the one
    # above: the console administers Superset as admin and Superset reads the warehouse as the
    # dashboard reader.
    assert credentials.username == "admin"
    assert credentials.password == ADMIN_PASSWORD


def test_the_warehouse_connection_superset_is_given_is_a_sqlalchemy_uri_not_a_libpq_dsn() -> None:
    """`SupersetCredentials.database_uri` reaches Superset's own SQLAlchemy layer.

    Every other provider in the demonstration takes a libpq keyword DSN. Handing one of those to
    Superset would be read as a database name, so the scheme is asserted rather than assumed.
    """
    uri = _database_uri()

    assert uri.startswith("postgresql+psycopg2://")
    assert "=" not in uri
    parsed = urlsplit(uri)
    # The host Superset can reach, which is the one the console's own DSN names.
    assert (parsed.hostname, parsed.port, parsed.path) == ("warehouse", 5432, "/heinzel")


def test_a_url_unsafe_role_password_reaches_superset_unchanged(tmp_path: Path) -> None:
    """The generated passwords are URL-unsafe, and an unescaped one would redirect the connection.

    Asserted through the real credentials rather than on the string alone, so the round trip proves
    what Superset is actually given: the authority and database still name the warehouse, and the
    password decodes back to exactly what was generated.
    """
    connection = demo_superset_connection(
        _environment(tmp_path / "tls"), database_uri=_database_uri(UNSAFE_PASSWORD), now=NOW
    )

    assert connection is not None
    parsed = urlsplit(connection.credentials.database_uri)
    assert parsed.password is not None
    assert unquote(parsed.password) == UNSAFE_PASSWORD
    assert parsed.password != UNSAFE_PASSWORD, "an unescaped password is not escaped at all"
    assert (parsed.hostname, parsed.port, parsed.path) == ("warehouse", 5432, "/heinzel")
    assert parsed.username == DEMO_WAREHOUSE_ROLES.dashboard


def test_a_warehouse_connection_naming_no_host_is_refused_before_superset_is_configured() -> None:
    """Superset connects from its own container, so a URI with no host reaches nothing at all."""
    with pytest.raises(ProvisioningRefused, match="no host and database"):
        demo_superset_database_uri(
            "dbname=heinzel", role=DEMO_WAREHOUSE_ROLES.dashboard, password="reader-secret"
        )


def test_a_warehouse_connection_without_a_port_names_one_no_port_rather_than_a_wrong_one() -> None:
    """An omitted port stays omitted, so Superset's driver applies its own default."""
    uri = demo_superset_database_uri(
        "postgresql://postgres@warehouse/heinzel",
        role=DEMO_WAREHOUSE_ROLES.dashboard,
        password="reader-secret",
    )

    parsed = urlsplit(uri)
    assert (parsed.hostname, parsed.port, parsed.path) == ("warehouse", None, "/heinzel")


def test_the_console_trusts_the_authority_it_minted_for_superset_and_nothing_else(
    tmp_path: Path,
) -> None:
    """The trust store the client verifies with holds exactly one certificate: the minted one.

    A context that also carried the system authorities would accept a Superset signed by any public
    issuer, which for a throwaway instance on a private network is no verification at all.
    """
    other = ensure_demo_superset_tls_material(tmp_path / "unrelated", now=NOW)
    connection = demo_superset_connection(
        _environment(tmp_path / "tls"), database_uri=_database_uri(), now=NOW
    )

    assert connection is not None
    trusted = {
        int(str(certificate["serialNumber"]), 16)
        for certificate in connection.ssl_context.get_ca_certs()
    }
    assert trusted == {_authority_serial(connection.tls_material.authority_path)}
    assert _authority_serial(other.authority_path) not in trusted
    client = connection.open_client()
    try:
        # Off deliberately: a bundle or proxy picked up from the environment would widen exactly
        # the trust the context above narrows.
        assert client.trust_env is False
    finally:
        client.close()


def test_the_superset_tls_material_is_minted_where_the_deployment_configured_it(
    tmp_path: Path,
) -> None:
    """Superset serves what the console mints, from the directory both containers mount."""
    directory = tmp_path / "configured-tls"
    connection = demo_superset_connection(
        _environment(directory), database_uri=_database_uri(), now=NOW
    )

    assert connection is not None
    material = connection.tls_material
    assert material.complete
    assert material.authority_path.parent == directory
    assert {path.name for path in directory.iterdir()} == {"ca.crt", "server.crt", "server.key"}


def test_a_superset_that_is_not_https_is_refused_rather_than_read_as_no_superset(
    tmp_path: Path,
) -> None:
    """A variable that is present and wrong is a different thing from one that is absent.

    `SupersetCredentials` refuses a base URL that is not an HTTPS origin, and that refusal is
    allowed to propagate: an operator who configured `http://` is told so, rather than being handed
    a console that reports dashboard publication as not delivered for a Superset that is running.
    """
    environment = _environment(tmp_path / "tls")
    environment[SUPERSET_BASE_URL_VARIABLE] = "http://superset:8088"

    with pytest.raises(ValueError, match="HTTPS origin"):
        demo_superset_connection(environment, database_uri=_database_uri(), now=NOW)


def test_the_publication_seam_is_assembled_over_the_dashboards_the_console_already_holds(
    tmp_path: Path, dashboards: SQLiteDashboardRepository
) -> None:
    """Given a Superset, the console gets bi-control's workflow behind one command.

    The publisher is composed over the control service built here, which is the only way the
    Superset provider reaches a dashboard at all. Nothing is published: that needs the instance the
    live suite brings up.
    """
    composed: list[DashboardControlService] = []

    def compose_publisher(control: DashboardControlService) -> _RefusingPublisher:
        composed.append(control)
        return _RefusingPublisher()

    with demo_dashboard_publication(
        _environment(tmp_path / "tls"),
        state_dir=tmp_path,
        dashboards=dashboards,
        database_uri=_database_uri(),
        current_answers=_NoAnswers(),
        answer_authority=_NoAnswerAuthority(),
        compose_publisher=compose_publisher,
        clock=lambda: NOW,
    ) as commands:
        assert isinstance(commands, WorkflowDashboardPublicationCommands)
        assert len(composed) == 1
        assert isinstance(composed[0], DashboardControlService)
        # The intents the workflow records, in the state directory the demonstration keeps.
        assert (tmp_path / "dashboard-publications.sqlite3").is_file()

    # Released with the context: the publication database is closed, so a second start over the
    # same state directory is not refused a lock the first never gave up.
    assert (tmp_path / "dashboard-publications.sqlite3").is_file()


class _NoAnswers:
    """The console's current-answer reader for a request that has no delivered answer."""

    def current_answer_id(self, tenant_id: str, request_id: str) -> str | None:
        return None


class _NoAnswerAuthority:
    """bi-control's answer authority with nothing to answer, which refuses every publication."""

    def read_exact(self, *, tenant_id: str, request_id: str, answer_id: str) -> None:
        return None


def _never_composed(control: DashboardControlService) -> _RefusingPublisher:
    raise AssertionError("no publisher is composed without a Superset to publish to")
