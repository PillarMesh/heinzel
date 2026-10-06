"""The secret store the demonstration resolves its source connection details from.

`SourceBindingService` never holds a connection detail: it takes a `SourceSecretResolver` as a
collaborator and persists only the `PrivateSourceCapability` that returns, whose
`endpoint_reference` and `credential_reference` are references into whatever store the deployment
keeps its secrets in. A deployment injects the resolver its own secret custody answers for. A local
demonstration has none, so this is one it can run: the connection details in files beside the
demonstration's other state, each file readable only by its owner.

What this stands in for is the secret custodian, not the reference discipline. The construction is
the real one -- a reference is 32 bytes from the operating system's generator and is not derived
from the secret it names, so holding one tells you nothing about the credential and cannot be used
to test a guess at it; the DSN never appears in a `repr` or in anything this module raises; and an
enrollment is immutable, so a second, different detail under one identity is refused rather than
quietly replacing the one a binding was validated against. What it does not stand in for is a
secret manager: the files sit under the demonstration's state directory, readable by whoever can
read that directory, nothing is ever re-encrypted or rotated out, and deleting the directory loses
every connection detail the demonstration enrolled.

Rotation is the one place the demonstration cannot be the real thing even in posture.
`SourceBindingService.rotate_credentials` asks for the capability at the next credential revision,
and a custodian would mint a new credential at the source. This store mints a new reference pair
over the connection detail already enrolled for the handle: the reference changes, the credential
behind it does not. That is a rotation of the handle, not of the secret, and it is named here
rather than hidden.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from heinzel_connection_broker import PrivateSourceCapability, SourceAccountMode
from heinzel_contract_model import digest
from heinzel_provider_postgresql import PostgreSQLAcquisitionSettings
from heinzel_provider_sdk.errors import AcquisitionProviderKind
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_serializer

__all__ = [
    "DEMO_SOURCE_SECRET_DIRNAME",
    "DemoPostgreSQLSourceCapabilityAuthority",
    "DemoSourceSecretError",
    "DemoSourceSecretStore",
]

# Named rather than nested among the demonstration's SQLite stores: everything in here is a
# connection detail, and this is the one directory of the demonstration's state that must never be
# copied anywhere.
DEMO_SOURCE_SECRET_DIRNAME = "source-secrets"

_CONNECTION_DOMAIN = "heinzel-demo-source-connection-v1"
_CAPABILITY_DOMAIN = "heinzel-demo-source-capability-v1"
_REFERENCE_BYTES = 32

_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)


class DemoSourceSecretError(RuntimeError):
    """A connection detail is missing, unreadable, or disagrees with what is asked for.

    The message names the operation and the identity, never the connection detail: this error
    reaches a log, and `SourceBindingService` wraps whatever a resolver raises into
    `SourceBindingBoundaryError` without reading it, so nothing is gained by putting a DSN in it.
    """

    def __init__(self, *, operation: str, detail: str) -> None:
        self.operation = operation
        super().__init__(f"demonstration source secret store failed to {operation}: {detail}")


class _StoredSourceConnection(BaseModel):
    """A connection detail the demonstration enrolled, under the handle that names it."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    connection_handle: str = Field(min_length=1)
    dsn: SecretStr = Field(repr=False)

    @field_serializer("dsn", when_used="json")
    def _unsealed_dsn(self, value: SecretStr) -> str:
        """Write the DSN itself, because this model *is* the stored secret.

        `SecretStr` serializes to a mask by default, which is right everywhere else and wrong
        here: a masked file is not a connection detail. The field is `repr=False` and the python
        dump still masks, so the clear value appears only in the JSON this store writes to a file
        it created mode 0600.
        """
        return value.get_secret_value()


class _StoredSourceCapability(BaseModel):
    """The reference pair the demonstration minted for one binding at one credential revision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    tenant_id: str = Field(min_length=1)
    binding_id: str = Field(min_length=1)
    provider_kind: AcquisitionProviderKind
    connection_handle: str = Field(min_length=1)
    account_mode: SourceAccountMode
    credential_revision: int = Field(ge=1)
    endpoint_reference: str = Field(pattern=r"^endpoint-ref:[0-9a-f]{64}$")
    credential_reference: str = Field(pattern=r"^credential-ref:[0-9a-f]{64}$")

    def as_capability(self) -> PrivateSourceCapability:
        return PrivateSourceCapability(
            tenant_id=self.tenant_id,
            binding_id=self.binding_id,
            provider_kind=self.provider_kind,
            connection_handle=self.connection_handle,
            account_mode=self.account_mode,
            credential_revision=self.credential_revision,
            endpoint_reference=self.endpoint_reference,
            credential_reference=self.credential_reference,
        )


class DemoSourceSecretStore:
    """Enrols a source connection detail, and resolves capabilities and connections from it.

    Satisfies `heinzel_connection_broker.SourceSecretResolver`. Two kinds of record live under one
    directory, each in its own file: a connection, keyed by the handle an operator enrolled it
    under, and a capability, keyed by the binding and credential revision the broker asked for.
    They are separate because they are written by different parties at different times -- the
    operator enrols the connection before any binding exists, and the broker asks for the
    capability while creating the draft, when the binding identifier first exists.
    """

    def __init__(self, state_dir: Path) -> None:
        directory = state_dir / DEMO_SOURCE_SECRET_DIRNAME
        # Mode on the `mkdir` itself rather than a later `chmod`: a directory created world
        # readable and restricted afterwards is readable by everyone until the second call lands.
        # An existing directory keeps whatever mode it has, which is the demonstration's own from
        # an earlier start.
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._directory = directory

    def enroll_connection(self, *, connection_handle: str, dsn: SecretStr) -> None:
        """Record the connection detail behind `connection_handle`, once.

        Enrolling the same handle with the same detail again is the operator restarting the
        demonstration, and succeeds. Enrolling it with a *different* detail is refused: a binding
        already validated against the first would then be ready over a source nobody probed.
        """
        if not connection_handle:
            raise DemoSourceSecretError(
                operation="enrol a source connection", detail="the handle is empty"
            )
        record = _StoredSourceConnection(connection_handle=connection_handle, dsn=dsn)
        path = self._connection_path(connection_handle)
        if self._write_once(path, record, operation="enrol a source connection"):
            return
        existing = _read(path, _StoredSourceConnection, operation="enrol a source connection")
        if existing.connection_handle != connection_handle or existing.dsn.get_secret_value() != (
            dsn.get_secret_value()
        ):
            raise DemoSourceSecretError(
                operation="enrol a source connection",
                detail=f"{connection_handle} is already enrolled with a different detail",
            )

    def enrolled_connection_handles(self) -> tuple[str, ...]:
        """Every handle an operator has enrolled a connection under, and nothing else.

        A handle is a name, not a connection: holding one tells a caller nothing about the
        endpoint or the credential behind it, which is the whole point of the reference
        discipline this store keeps. So this is the one read a console surface can be built on,
        and it returns handles alone.

        The record has to be opened to answer, because a connection's file is named by a digest
        of its handle and a digest does not invert. That is this store reading its own records,
        which it already does to resolve one; what is new is only that a caller can ask which
        handles exist. The DSN is read into this method and never leaves it -- not in the return
        value, not in a log, and not in anything raised from here.

        Sorted, so two reads of an unchanged store agree and a surface built on it is stable.
        A file that is not one of these records fails here rather than being skipped: a store
        holding something unreadable is a store whose answer cannot be trusted to be complete.
        """
        handles: list[str] = []
        try:
            entries = tuple(self._directory.iterdir())
        except OSError as error:
            raise DemoSourceSecretError(
                operation="list enrolled source connections",
                detail="the store cannot be read",
            ) from error
        for path in entries:
            if not path.name.startswith("connection-"):
                continue
            record = _read(
                path,
                _StoredSourceConnection,
                operation="list enrolled source connections",
            )
            if self._connection_path(record.connection_handle) != path:
                # The file is named by a digest of the handle it holds. A record found under
                # another handle's name is corruption, and returning its handle would offer a
                # handle whose connection this store could not then resolve.
                raise DemoSourceSecretError(
                    operation="list enrolled source connections",
                    detail="a stored connection is filed under a different handle",
                )
            handles.append(record.connection_handle)
        return tuple(sorted(handles))

    def resolve(
        self,
        *,
        tenant_id: str,
        binding_id: str,
        provider_kind: AcquisitionProviderKind,
        connection_handle: str,
        account_mode: SourceAccountMode,
        credential_revision: int,
    ) -> PrivateSourceCapability:
        """The capability for this binding at this credential revision, minting it on first ask.

        The references are minted here rather than at enrollment because they belong to the
        binding, not to the connection: `rotate_credentials` asks again at the next credential
        revision and must get a reference pair the previous revision's does not open.
        """
        connection = _read(
            self._connection_path(connection_handle),
            _StoredSourceConnection,
            operation="resolve a source capability",
        )
        if connection.connection_handle != connection_handle:
            raise DemoSourceSecretError(
                operation="resolve a source capability",
                detail="the stored connection names a different handle",
            )
        record = _StoredSourceCapability(
            tenant_id=tenant_id,
            binding_id=binding_id,
            provider_kind=provider_kind,
            connection_handle=connection_handle,
            account_mode=account_mode,
            credential_revision=credential_revision,
            endpoint_reference=f"endpoint-ref:{secrets.token_hex(_REFERENCE_BYTES)}",
            credential_reference=f"credential-ref:{secrets.token_hex(_REFERENCE_BYTES)}",
        )
        path = self._capability_path(tenant_id, binding_id, credential_revision)
        if self._write_once(path, record, operation="resolve a source capability"):
            return record.as_capability()
        # Already minted on an earlier ask. The references are what the broker persisted and what
        # a probe will present, so the stored pair is the answer and the freshly drawn one is
        # discarded -- it opened nothing.
        existing = _read(path, _StoredSourceCapability, operation="resolve a source capability")
        if (
            existing.tenant_id != tenant_id
            or existing.binding_id != binding_id
            or existing.provider_kind != provider_kind
            or existing.connection_handle != connection_handle
            or existing.account_mode != account_mode
            or existing.credential_revision != credential_revision
        ):
            raise DemoSourceSecretError(
                operation="resolve a source capability",
                detail=f"{binding_id} revision {credential_revision} is enrolled differently",
            )
        return existing.as_capability()

    def resolve_connection(
        self,
        *,
        tenant_id: str,
        binding_id: str,
        credential_revision: int,
        endpoint_reference: str,
        credential_reference: str,
    ) -> tuple[str, SecretStr]:
        """The handle and connection detail a reference pair names, or a refusal.

        Both references must be the ones minted for this identity. A pair that half matches is
        refused rather than resolved on the half that did: a reference is the only thing standing
        between a caller and the connection detail, so one of them being right is not enough.
        """
        record = _read(
            self._capability_path(tenant_id, binding_id, credential_revision),
            _StoredSourceCapability,
            operation="resolve a source connection",
        )
        if (
            record.tenant_id != tenant_id
            or record.binding_id != binding_id
            or record.credential_revision != credential_revision
            or record.endpoint_reference != endpoint_reference
            or record.credential_reference != credential_reference
        ):
            raise DemoSourceSecretError(
                operation="resolve a source connection",
                detail=f"{binding_id} revision {credential_revision} has no such reference pair",
            )
        connection = _read(
            self._connection_path(record.connection_handle),
            _StoredSourceConnection,
            operation="resolve a source connection",
        )
        return record.connection_handle, connection.dsn

    def _connection_path(self, connection_handle: str) -> Path:
        return self._directory / (
            "connection-" + digest({"domain": _CONNECTION_DOMAIN, "handle": connection_handle})
        )

    def _capability_path(self, tenant_id: str, binding_id: str, credential_revision: int) -> Path:
        return self._directory / (
            "capability-"
            + digest(
                {
                    "domain": _CAPABILITY_DOMAIN,
                    "tenant_id": tenant_id,
                    "binding_id": binding_id,
                    "credential_revision": credential_revision,
                }
            )
        )

    @staticmethod
    def _write_once(path: Path, record: BaseModel, *, operation: str) -> bool:
        """Create `path` holding `record`, and say whether this call is what created it.

        Durable before it is returned: a capability the broker has persisted a reference to, or a
        connection a binding was validated against, must still be readable on the next start.
        """
        payload = record.model_dump_json().encode("utf-8")
        try:
            descriptor = os.open(path, _CREATE_FLAGS, 0o600)
        except FileExistsError:
            return False
        except OSError as error:
            raise DemoSourceSecretError(
                operation=operation, detail="the file cannot be created"
            ) from error
        try:
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise DemoSourceSecretError(
                    operation=operation, detail="the record was written only in part"
                )
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        # The contents are durable above; this is the file's name, which a crash can otherwise
        # lose while keeping a reference to it that the broker already persisted.
        directory_descriptor = os.open(path.parent, _DIRECTORY_FLAGS)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return True


class DemoPostgreSQLSourceCapabilityAuthority:
    """Turns an enrolled reference pair into the acquisition settings a probe validates with.

    Satisfies `heinzel_provider_postgresql.PostgreSQLSourceCapabilityAuthority`. The declaration
    is the deployment's, not the store's, so it arrives as a factory of the resolved DSN -- the
    same factory the deployment builds its acquisition provider from, which is what makes a
    binding this admits one the acquisition can actually read.
    """

    def __init__(
        self,
        store: DemoSourceSecretStore,
        settings_factory: Callable[[str, SecretStr], PostgreSQLAcquisitionSettings],
    ) -> None:
        self._store = store
        self._settings_factory = settings_factory

    def resolve_source_acquisition(
        self,
        *,
        tenant_id: str,
        binding_id: str,
        credential_revision: int,
        endpoint_reference: str,
        credential_reference: str,
    ) -> PostgreSQLAcquisitionSettings:
        connection_handle, dsn = self._store.resolve_connection(
            tenant_id=tenant_id,
            binding_id=binding_id,
            credential_revision=credential_revision,
            endpoint_reference=endpoint_reference,
            credential_reference=credential_reference,
        )
        settings = self._settings_factory(connection_handle, dsn)
        if settings.connection_handle != connection_handle:
            raise DemoSourceSecretError(
                operation="compose source acquisition settings",
                detail="the composed settings name a different handle",
            )
        return settings


def _read[Record: BaseModel](path: Path, model: type[Record], *, operation: str) -> Record:
    """The record at `path`, or a refusal that names the operation and nothing else.

    Validated against the real model rather than read as a mapping: a file that is no longer one
    of these records must fail here, where the failure names the store, and not later as a
    connection attempt against something that is not a DSN.
    """
    try:
        descriptor = os.open(path, _READ_FLAGS)
    except FileNotFoundError as error:
        raise DemoSourceSecretError(operation=operation, detail="no such record") from error
    except OSError as error:
        raise DemoSourceSecretError(
            operation=operation, detail="the record cannot be read"
        ) from error
    with os.fdopen(descriptor, "rb") as record_file:
        payload = record_file.read()
    try:
        return model.model_validate_json(payload)
    except ValueError:
        # The driver's own message would quote the payload, and the payload is the secret.
        raise DemoSourceSecretError(
            operation=operation, detail="the stored record is not readable as one"
        ) from None
