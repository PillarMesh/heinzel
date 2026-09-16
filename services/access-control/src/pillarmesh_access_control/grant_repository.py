from __future__ import annotations

import sqlite3
from contextlib import suppress

from pillarmesh_contract_model import canonical_bytes
from pydantic import ValidationError

from .models import AccessEffectAction, AccessEffectReceipt, AccessEffectSurface, AccessGrant


class AccessGrantConflict(RuntimeError):
    pass


class AccessGrantIntegrityError(RuntimeError):
    pass


class AccessGrantStaleRevision(RuntimeError):
    pass


class SQLiteAccessGrantRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(
            "CREATE TABLE IF NOT EXISTS access_grant_revisions ("
            "tenant_id TEXT NOT NULL, grant_id TEXT NOT NULL, revision INTEGER NOT NULL, "
            "payload BLOB NOT NULL, PRIMARY KEY (tenant_id, grant_id, revision));"
            "CREATE TABLE IF NOT EXISTS access_grant_current ("
            "tenant_id TEXT NOT NULL, grant_id TEXT NOT NULL, current_revision INTEGER NOT NULL, "
            "PRIMARY KEY (tenant_id, grant_id), "
            "FOREIGN KEY (tenant_id, grant_id, current_revision) "
            "REFERENCES access_grant_revisions(tenant_id, grant_id, revision));"
            "CREATE TABLE IF NOT EXISTS access_effect_receipts ("
            "tenant_id TEXT NOT NULL, effect_id TEXT NOT NULL, grant_id TEXT NOT NULL, "
            "grant_revision INTEGER NOT NULL, surface TEXT NOT NULL, action TEXT NOT NULL, "
            "attempt INTEGER NOT NULL, payload BLOB NOT NULL, "
            "PRIMARY KEY (tenant_id, effect_id), "
            "UNIQUE (tenant_id, grant_id, grant_revision, surface, action, attempt), "
            "FOREIGN KEY (tenant_id, grant_id, grant_revision) "
            "REFERENCES access_grant_revisions(tenant_id, grant_id, revision));"
        )

    def append(self, grant: AccessGrant, *, expected_current_revision: int) -> AccessGrant:
        if expected_current_revision < 0 or grant.revision != expected_current_revision + 1:
            raise AccessGrantStaleRevision("access grant revision is stale")
        payload = canonical_bytes(grant)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            replay = self._connection.execute(
                "SELECT payload FROM access_grant_revisions "
                "WHERE tenant_id = ? AND grant_id = ? AND revision = ?",
                (grant.tenant_id, grant.grant_id, grant.revision),
            ).fetchone()
            if replay is not None:
                if bytes(replay[0]) != payload:
                    raise AccessGrantConflict("access grant revision conflicts with stored state")
                recorded = _decode_grant(
                    replay[0], (grant.tenant_id, grant.grant_id, grant.revision)
                )
                self._connection.commit()
                return recorded
            current = self._connection.execute(
                "SELECT current_revision FROM access_grant_current "
                "WHERE tenant_id = ? AND grant_id = ?",
                (grant.tenant_id, grant.grant_id),
            ).fetchone()
            actual_revision = 0 if current is None else int(current[0])
            if actual_revision != expected_current_revision:
                raise AccessGrantStaleRevision("access grant revision is stale")
            self._connection.execute(
                "INSERT INTO access_grant_revisions "
                "(tenant_id, grant_id, revision, payload) VALUES (?, ?, ?, ?)",
                (grant.tenant_id, grant.grant_id, grant.revision, payload),
            )
            if current is None:
                self._connection.execute(
                    "INSERT INTO access_grant_current "
                    "(tenant_id, grant_id, current_revision) VALUES (?, ?, ?)",
                    (grant.tenant_id, grant.grant_id, grant.revision),
                )
            else:
                updated = self._connection.execute(
                    "UPDATE access_grant_current SET current_revision = ? "
                    "WHERE tenant_id = ? AND grant_id = ? AND current_revision = ?",
                    (
                        grant.revision,
                        grant.tenant_id,
                        grant.grant_id,
                        expected_current_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise AccessGrantStaleRevision("access grant revision is stale")
            self._connection.commit()
            return grant
        except BaseException:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise

    def load_current(self, tenant_id: str, grant_id: str) -> AccessGrant | None:
        row = self._connection.execute(
            "SELECT current.current_revision, revisions.payload "
            "FROM access_grant_current AS current "
            "JOIN access_grant_revisions AS revisions "
            "ON revisions.tenant_id = current.tenant_id "
            "AND revisions.grant_id = current.grant_id "
            "AND revisions.revision = current.current_revision "
            "WHERE current.tenant_id = ? AND current.grant_id = ?",
            (tenant_id, grant_id),
        ).fetchone()
        if row is None:
            return None
        return _decode_grant(row[1], (tenant_id, grant_id, int(row[0])))

    def load_current_for_request(self, tenant_id: str, request_id: str) -> AccessGrant | None:
        rows = self._connection.execute(
            "SELECT current.grant_id, current.current_revision, revisions.payload "
            "FROM access_grant_current AS current "
            "JOIN access_grant_revisions AS revisions "
            "ON revisions.tenant_id = current.tenant_id "
            "AND revisions.grant_id = current.grant_id "
            "AND revisions.revision = current.current_revision "
            "WHERE current.tenant_id = ? ORDER BY current.grant_id COLLATE BINARY",
            (tenant_id,),
        ).fetchall()
        matches: list[AccessGrant] = []
        for grant_id, revision, payload in rows:
            grant = _decode_grant(payload, (tenant_id, str(grant_id), int(revision)))
            if grant.request_id == request_id:
                matches.append(grant)
        if len(matches) > 1:
            raise AccessGrantIntegrityError("multiple current access grants exist for request")
        return matches[0] if matches else None

    def record_effect(self, receipt: AccessEffectReceipt) -> AccessEffectReceipt:
        payload = canonical_bytes(receipt)
        identity = (receipt.tenant_id, receipt.effect_id)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self._connection.execute(
                "SELECT grant_id, grant_revision, surface, action, attempt, payload "
                "FROM access_effect_receipts WHERE tenant_id = ? AND effect_id = ?",
                identity,
            ).fetchone()
            if existing is not None:
                if bytes(existing[5]) != payload:
                    raise AccessGrantConflict("access effect receipt replay conflicts")
                recorded = _decode_effect(existing[5], (*identity, *existing[:5]))
                self._connection.commit()
                return recorded
            try:
                self._connection.execute(
                    "INSERT INTO access_effect_receipts "
                    "(tenant_id, effect_id, grant_id, grant_revision, surface, action, attempt, "
                    "payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        receipt.tenant_id,
                        receipt.effect_id,
                        receipt.grant_id,
                        receipt.grant_revision,
                        receipt.surface,
                        receipt.action,
                        receipt.attempt,
                        payload,
                    ),
                )
            except sqlite3.IntegrityError:
                raise AccessGrantConflict("access effect receipt identity conflicts") from None
            self._connection.commit()
            return receipt
        except BaseException:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise

    def successful_effect_surfaces(
        self,
        tenant_id: str,
        grant_id: str,
        grant_revision: int,
        *,
        action: AccessEffectAction,
    ) -> tuple[AccessEffectSurface, ...]:
        rows = self._connection.execute(
            "SELECT tenant_id, effect_id, grant_id, grant_revision, surface, action, attempt, "
            "payload FROM access_effect_receipts "
            "WHERE tenant_id = ? AND grant_id = ? AND grant_revision = ? AND action = ? "
            "ORDER BY surface COLLATE BINARY, attempt",
            (tenant_id, grant_id, grant_revision, action),
        ).fetchall()
        succeeded: set[AccessEffectSurface] = set()
        for row in rows:
            receipt = _decode_effect(row[7], tuple(row[:7]))
            if receipt.outcome == "succeeded":
                succeeded.add(receipt.surface)
        return tuple(sorted(succeeded))

    def effect_receipts(
        self,
        tenant_id: str,
        grant_id: str,
        grant_revision: int,
        *,
        action: AccessEffectAction,
    ) -> tuple[AccessEffectReceipt, ...]:
        rows = self._connection.execute(
            "SELECT tenant_id, effect_id, grant_id, grant_revision, surface, action, attempt, "
            "payload FROM access_effect_receipts "
            "WHERE tenant_id = ? AND grant_id = ? AND grant_revision = ? AND action = ? "
            "ORDER BY surface COLLATE BINARY, attempt",
            (tenant_id, grant_id, grant_revision, action),
        ).fetchall()
        return tuple(_decode_effect(row[7], tuple(row[:7])) for row in rows)


def _decode_grant(payload: object, expected_key: tuple[str, str, int]) -> AccessGrant:
    if not isinstance(payload, bytes):
        raise AccessGrantIntegrityError("stored access grant payload is invalid")
    try:
        grant = AccessGrant.model_validate_json(payload, strict=True)
    except (ValidationError, ValueError, TypeError):
        raise AccessGrantIntegrityError("stored access grant payload is invalid") from None
    if (grant.tenant_id, grant.grant_id, grant.revision) != expected_key:
        raise AccessGrantIntegrityError("stored access grant index does not match its payload")
    return grant


def _decode_effect(payload: object, expected_key: tuple[object, ...]) -> AccessEffectReceipt:
    if not isinstance(payload, bytes):
        raise AccessGrantIntegrityError("stored access effect receipt is invalid")
    try:
        receipt = AccessEffectReceipt.model_validate_json(payload, strict=True)
    except (ValidationError, ValueError, TypeError):
        raise AccessGrantIntegrityError("stored access effect receipt is invalid") from None
    actual_key = (
        receipt.tenant_id,
        receipt.effect_id,
        receipt.grant_id,
        receipt.grant_revision,
        receipt.surface,
        receipt.action,
        receipt.attempt,
    )
    if actual_key != expected_key:
        raise AccessGrantIntegrityError(
            "stored access effect receipt index does not match its payload"
        )
    return receipt
