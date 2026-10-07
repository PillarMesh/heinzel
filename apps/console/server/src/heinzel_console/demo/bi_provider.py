"""The console side of the demonstration's Superset: credentials, trust, and the publication seam.

ADR-0010 says the source snapshot bounds the window to publish and that nothing here makes a
dashboard demonstrable on its own -- an instance must still exist to publish to. This module is
how the console is told about one. Given a Superset, it assembles the BI provider bi-control's
publication workflow drives, and hands back the one seam the console's backend takes; given none,
it hands back nothing and dashboard publication stays `not_delivered`, which is the honest answer
with no Superset to publish to.

Three decisions are made here and nowhere else.

The console trusts exactly the authority it minted. `demo/superset_tls.py` mints a throwaway
authority per demonstration and writes it where Superset reads it; the client built here verifies
against that file alone, so a Superset serving anything else is a verification failure rather than
a publication to an instance nobody vouched for.

Superset queries the product as `dashboard_reader` and never as `answer_runtime`. ADR-0007 states
that `answer_runtime` is the runtime's own service principal for an already admitted governed
query, not a requester grant; a BI provider connecting as it would make a database login stand in
for an access decision. `dashboard_reader` holds `SELECT` on the materialized product and nothing
else, and publication still confers no access to the dashboard it creates.

Superset is given a SQLAlchemy URI, not the libpq keyword DSN every other provider here takes.
`SupersetCredentials.database_uri` reaches Superset's own SQLAlchemy layer, so the conversion
happens once, in `demo_superset_database_uri`, with the password percent-escaped: the
demonstration's role passwords are generated per start and routinely carry characters a URI
reserves.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode

import httpx
import psycopg
from heinzel_bi_control import (
    DashboardAnswerAuthorityReader,
    DashboardControlService,
    DashboardDatasetConnectionBinding,
    DashboardPublicationWorkflow,
    DashboardPublisher,
    DashboardQueryBindingReader,
    SQLiteDashboardConnectionRepository,
    SQLiteDashboardPublicationRepository,
    SQLiteDashboardRepository,
)
from heinzel_contract_model import ArtifactReference, digest
from heinzel_provider_superset import (
    HttpSupersetClient,
    HttpxSupersetTransport,
    SupersetCredentials,
    SupersetProvider,
)
from heinzel_warehouse_control import WarehouseBinding

from ..governed_adapters import (
    CurrentGovernedAnswerReader,
    WorkflowDashboardPublicationCommands,
)
from .superset_tls import DemoSupersetTLSMaterial, ensure_demo_superset_tls_material
from .warehouse import ProvisioningRefused

__all__ = [
    "SUPERSET_ADMIN_PASSWORD_VARIABLE",
    "SUPERSET_BASE_URL_VARIABLE",
    "SUPERSET_TLS_DIRECTORY_VARIABLE",
    "SUPERSET_WAREHOUSE_TLS_DIRECTORY_VARIABLE",
    "DemoSupersetConnection",
    "demo_dashboard_publication",
    "demo_superset_connection",
    "demo_superset_database_uri",
]

# The deployment's contract with `deploy/quickstart/compose.yaml`, which sets all three from one
# `HEINZEL_SUPERSET_ADMIN_PASSWORD` and leaves all three empty without it. Renaming one here
# without renaming it there would leave the console told about no Superset by a Compose project
# that started one.
SUPERSET_BASE_URL_VARIABLE = "HEINZEL_DEMO_SUPERSET_BASE_URL"
SUPERSET_TLS_DIRECTORY_VARIABLE = "HEINZEL_DEMO_SUPERSET_TLS_DIRECTORY"
SUPERSET_ADMIN_PASSWORD_VARIABLE = "HEINZEL_DEMO_SUPERSET_ADMIN_PASSWORD"
# Where the warehouse's own private directory is mounted inside Superset's container, on the
# warehouse-control path. A warehouse that service provisioned demands TLS and a client
# certificate its `pg_hba.conf` verifies, and those files are the console's: Superset cannot
# present them unless the deployment put them where it can read them, and this names where.
SUPERSET_WAREHOUSE_TLS_DIRECTORY_VARIABLE = "HEINZEL_DEMO_SUPERSET_WAREHOUSE_TLS_DIRECTORY"

# The account Superset's initialization creates, and the only one the console has to reach it
# with. `superset fab create-admin --username admin` in the Compose project names it; a different
# name here would authenticate as nobody.
_SUPERSET_ADMIN_USERNAME = "admin"

# What Superset's own driver takes. Not `postgresql://`: the console's other providers are given
# libpq keyword DSNs, and the scheme is what says which of the two this string is.
_SQLALCHEMY_SCHEME = "postgresql+psycopg2"

# Long enough for Superset to create a database, a dataset and a chart per visual intent on a
# cold instance, which is slower than any read the console makes.
_REQUEST_TIMEOUT = 30.0

_PUBLICATIONS_DATABASE = "dashboard-publications.sqlite3"


@dataclass(frozen=True, slots=True)
class DemoSupersetConnection:
    """One Superset the console may publish to, and the trust it reaches it with."""

    # Kept out of the representation: it carries the admin password and the dashboard reader's,
    # and a repr of this object reaches a log or a traceback.
    credentials: SupersetCredentials = field(repr=False)
    tls_material: DemoSupersetTLSMaterial
    ssl_context: ssl.SSLContext

    def open_client(self) -> httpx.Client:
        """A client that trusts the minted authority and nothing else.

        `trust_env` is off deliberately. A proxy or a certificate bundle picked up from the
        environment would either route the console's publication somewhere it was not sent or
        widen the trust this context exists to narrow.
        """
        return httpx.Client(
            verify=self.ssl_context,
            timeout=_REQUEST_TIMEOUT,
            trust_env=False,
        )


@dataclass(frozen=True, slots=True)
class DemoWarehouseRoute:
    """How Superset reaches the warehouse, from inside its own container.

    Named separately from the console's own connection because the two differ on the
    warehouse-control path, and silently: the console reaches that warehouse on the loopback port
    its Compose project publishes, which from inside any container is that container. Superset
    reaches it by name on a network they share, presenting the client certificate the warehouse's
    `pg_hba.conf` verifies -- so the paths here are Superset's, not the console's.
    """

    host: str
    port: int
    tls_directory: PurePosixPath


def demo_superset_database_uri(
    bootstrap_dsn: str,
    *,
    role: str,
    password: str,
    route: DemoWarehouseRoute | None = None,
) -> str:
    """The SQLAlchemy URI Superset queries the product with, from the console's own warehouse DSN.

    Without a `route` the host comes from the console's bootstrap DSN, because on the
    demonstration's own warehouse both reach it the same way: it is a sibling container on the
    same Compose network, named the same from either side.

    With one, the route is used instead, and it has to be. The loopback address that reaches a
    published port from the host reaches Superset itself from inside its container, so a URI
    naming it has Superset looking for the warehouse inside itself -- which is a refused
    connection, reported as a provider that rejected the dashboard.

    The login and password are percent-escaped. The demonstration generates a fresh password per
    start from `secrets.token_urlsafe`, which includes `-` and `_` today and whose output no caller
    may assume; an unescaped one that happened to carry `@` or `/` would silently change which host
    and database the URI names.
    """
    parsed = psycopg.conninfo.conninfo_to_dict(bootstrap_dsn)
    database = parsed.get("dbname")
    if not isinstance(database, str) or not database:
        raise ProvisioningRefused(
            "the warehouse connection names no database for Superset to read the product from"
        )
    login = f"{quote(role, safe='')}:{quote(password, safe='')}"
    if route is not None:
        return (
            f"{_SQLALCHEMY_SCHEME}://{login}@{route.host}:{route.port}"
            f"/{quote(database, safe='')}?{_route_query(route.tls_directory)}"
        )
    host = parsed.get("host")
    if not isinstance(host, str) or not host:
        raise ProvisioningRefused(
            "the warehouse connection names no host for Superset to reach it by"
        )
    port = parsed.get("port")
    authority = f"{host}:{port}" if isinstance(port, str) and port else host
    return f"{_SQLALCHEMY_SCHEME}://{login}@{authority}/{quote(database, safe='')}"


def _route_query(tls_directory: PurePosixPath) -> str:
    """The transport parameters, naming files by their path inside Superset's container.

    `verify-full` rather than `verify-ca`, so the warehouse's name is checked against the
    certificate and not only the authority that signed it. Its certificate carries that name
    because the console minted it to -- see `demo/warehouse_tls.py`.
    """
    return urlencode(
        {
            "sslmode": "verify-full",
            "sslrootcert": str(tls_directory / "ca.crt"),
            "sslcert": str(tls_directory / "client.crt"),
            "sslkey": str(tls_directory / "client.key"),
        }
    )


def demo_superset_connection(
    environment: Mapping[str, str],
    *,
    database_uri: str,
    now: datetime,
) -> DemoSupersetConnection | None:
    """The Superset this deployment asked for, or `None` because it asked for none.

    A half-configured Superset is not a Superset. All three variables are read, an empty value
    counts as absent, and anything short of all three is `None`: the Compose project sets them
    together from one secret, so a partial set is a hand-edit, and starting against a Superset that
    was only partly described would publish nowhere in particular. What the console reports instead
    is dashboard publication `not_delivered`, which is true.

    A variable that is present and wrong is a different thing and is never read as absence.
    `SupersetCredentials` refuses a base URL that is not an HTTPS origin, and that refusal
    propagates: an operator who set `http://superset:8088` gets told so rather than getting a
    console that quietly publishes nothing.

    The environment is passed in rather than read from `os.environ`, so nothing has to mutate
    process state to describe a deployment.
    """
    base_url = environment.get(SUPERSET_BASE_URL_VARIABLE, "")
    tls_directory = environment.get(SUPERSET_TLS_DIRECTORY_VARIABLE, "")
    admin_password = environment.get(SUPERSET_ADMIN_PASSWORD_VARIABLE, "")
    if not all((base_url, tls_directory, admin_password)):
        return None
    # Before the material is minted, so a base URL no client could use is refused rather than
    # leaving a certificate authority behind for a Superset the console will not reach.
    credentials = SupersetCredentials(
        base_url=base_url,
        username=_SUPERSET_ADMIN_USERNAME,
        password=admin_password,
        database_uri=database_uri,
    )
    material = ensure_demo_superset_tls_material(Path(tls_directory), now=now)
    return DemoSupersetConnection(
        credentials=credentials,
        tls_material=material,
        ssl_context=ssl.create_default_context(cafile=str(material.authority_path)),
    )


def demo_dashboard_connection_secret_ref(*, tenant_id: str, warehouse_binding_id: str) -> str:
    """Where a deployment would resolve the credential Superset queries the product with.

    A reference, never the credential. The demonstration holds the password in the process that
    minted it and resolves nothing, so this names the custody a deployment would have rather than
    one that exists here -- and `demo/bi_provider.py` hands Superset the URI directly, which is why
    no resolver is wired behind it.

    It also has to be stable across starts, because the Superset provider derives the database's
    own identity from it: a reference carrying anything per-start would have each start create a
    second database connection beside the first rather than finding the one already there.
    """
    return f"secret://{tenant_id}/warehouse/{warehouse_binding_id}/dashboard-reader"


def record_demo_dashboard_connection(
    connections: SQLiteDashboardConnectionRepository,
    *,
    query_bindings: DashboardQueryBindingReader,
    product_ref: ArtifactReference,
    generation: int,
    warehouse_binding: WarehouseBinding,
) -> DashboardDatasetConnectionBinding | None:
    """Authorize Superset to reach the product's consumption object, citing the warehouse binding.

    The relation comes from the approved query binding rather than from anything here: that is the
    authority that says which consumption object the product is read through, and bi-control
    refuses a connection naming a different namespace or relation than the binding it publishes
    against.

    The warehouse binding comes from warehouse-control, and this is why there is no equivalent on
    the demonstration's other path. `warehouse_binding_id`, its revision and its digest assert that
    this connection is the one that service authorized, at that revision; bi-control carries them
    and verifies none of them, so nothing downstream would catch an assertion that was invented.
    ADR-0003 keeps a warehouse warehouse-control never provisioned out of scope, so a database it
    never saw has no binding to cite and gets no connection -- and the publication then fails as an
    unavailable authority, which is the truth about it.

    `None` when the product has no approved query binding yet, which is a product that cannot be
    published rather than one to invent a connection for.
    """
    binding = query_bindings.read_current(
        tenant_id=warehouse_binding.tenant_id, product_ref=product_ref, generation=generation
    )
    if binding is None:
        return None
    return connections.store(
        DashboardDatasetConnectionBinding(
            tenant_id=warehouse_binding.tenant_id,
            engine_kind=binding.engine_kind,
            consumption_object_ref=binding.consumption_object_ref,
            namespace=binding.namespace,
            relation_name=binding.relation_name,
            warehouse_binding_id=warehouse_binding.binding_id,
            warehouse_binding_revision=warehouse_binding.revision,
            warehouse_binding_digest=digest(warehouse_binding),
            connection_secret_ref=demo_dashboard_connection_secret_ref(
                tenant_id=warehouse_binding.tenant_id,
                warehouse_binding_id=warehouse_binding.binding_id,
            ),
        )
    )


@contextmanager
def demo_dashboard_publication(
    environment: Mapping[str, str],
    *,
    state_dir: Path,
    dashboards: SQLiteDashboardRepository,
    database_uri: str,
    current_answers: CurrentGovernedAnswerReader,
    answer_authority: DashboardAnswerAuthorityReader,
    compose_publisher: Callable[[DashboardControlService], DashboardPublisher],
    clock: Callable[[], datetime],
) -> Iterator[WorkflowDashboardPublicationCommands | None]:
    """The console's publication seam over a real Superset, or `None` given no Superset.

    A context manager because what it opens has to be released: an HTTP client holding a
    connection to Superset and the SQLite database the publication intents are recorded in. The
    console enters it alongside the governed answer whose delivery a publication is compiled from.

    `compose_publisher` is given the control service this builds and returns what publishes
    through it, which is bi-control's `DashboardCompositionService` over the demonstration's
    contract, query-binding, materialization, product-publication and connection authorities. It
    is injected rather than assembled here because none of those authorities is about Superset,
    while the control service cannot be built without the provider that is: the caller holds the
    authorities, this holds the provider, and the callable is where the two meet.
    """
    connection = demo_superset_connection(environment, database_uri=database_uri, now=clock())
    if connection is None:
        yield None
        return
    with ExitStack() as closing:
        client = closing.enter_context(connection.open_client())
        repository = SQLiteDashboardPublicationRepository(str(state_dir / _PUBLICATIONS_DATABASE))
        closing.callback(repository.close)
        provider = SupersetProvider(
            HttpSupersetClient(
                credentials=connection.credentials,
                transport=HttpxSupersetTransport(client),
            )
        )
        yield WorkflowDashboardPublicationCommands(
            DashboardPublicationWorkflow(
                repository=repository,
                composition=compose_publisher(
                    DashboardControlService(dashboards, provider, clock=clock)
                ),
                answers=answer_authority,
                clock=clock,
            ),
            current_answers,
        )
