from __future__ import annotations

import sqlite3
from contextlib import suppress

from heinzel_contract_model import canonical_bytes

from .query_models import GovernedQueryPlan


class QueryPlanConflict(ValueError):
    pass


class SQLiteQueryPlanRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS governed_query_plans ("
            "tenant_id TEXT NOT NULL, plan_digest TEXT NOT NULL, plan_id TEXT NOT NULL, "
            "payload BLOB NOT NULL, PRIMARY KEY (tenant_id, plan_digest), "
            "UNIQUE (tenant_id, plan_id))"
        )
        self._connection.commit()

    def read(self, tenant_id: str, plan_digest: str) -> GovernedQueryPlan | None:
        row = self._connection.execute(
            "SELECT payload, plan_id FROM governed_query_plans "
            "WHERE tenant_id = ? AND plan_digest = ?",
            (tenant_id, plan_digest),
        ).fetchone()
        if row is None:
            return None
        plan = GovernedQueryPlan.model_validate_json(bytes(row[0]), strict=True)
        if plan.tenant_id != tenant_id or plan.plan_digest != plan_digest or plan.plan_id != row[1]:
            raise QueryPlanConflict("stored query plan authority does not match its key")
        return plan

    def save(self, plan: GovernedQueryPlan) -> GovernedQueryPlan:
        validated = GovernedQueryPlan.model_validate(plan.model_dump(), strict=True)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self.read(validated.tenant_id, validated.plan_digest)
            if existing is not None:
                if existing != validated:
                    raise QueryPlanConflict("query plan replay conflicts with the stored artifact")
                self._connection.commit()
                return existing
            self._connection.execute(
                "INSERT INTO governed_query_plans (tenant_id, plan_digest, plan_id, payload) "
                "VALUES (?, ?, ?, ?)",
                (
                    validated.tenant_id,
                    validated.plan_digest,
                    validated.plan_id,
                    canonical_bytes(validated),
                ),
            )
            self._connection.commit()
            return validated
        except sqlite3.IntegrityError as error:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise QueryPlanConflict("query plan identity is already bound") from error
        except BaseException:
            with suppress(sqlite3.Error):
                self._connection.rollback()
            raise
