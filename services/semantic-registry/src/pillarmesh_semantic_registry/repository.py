from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from pillarmesh_contract_model import (
    ApprovedSemanticVersion,
    InformationKind,
    canonical_bytes,
    digest,
)

from .models import (
    AuthorityObservation,
    AuthoritySourceKind,
    CandidateKind,
    CandidateProvenance,
    SemanticCandidate,
    SemanticCandidateSet,
)
from .review import OntologyReviewBundle, SemanticRevision


@dataclass(frozen=True, slots=True)
class _CandidateDraft:
    kind: CandidateKind
    name: str
    proposed_definition: str | None
    related_refs: tuple[str, ...]
    provenance: CandidateProvenance
    confidence: Decimal


class SemanticRepository(Protocol):
    def store(self, candidate_set: SemanticCandidateSet) -> SemanticCandidateSet: ...

    def load_revision(self, tenant_id: str, set_id: str, revision: int) -> SemanticCandidateSet: ...

    def owns_candidate(self, tenant_id: str, candidate: SemanticCandidate) -> bool: ...

    def verify_review_inputs(
        self,
        *,
        tenant_id: str,
        candidate_set_id: str,
        candidate_set_revision: int,
        candidate_ids: tuple[str, ...],
        observation_digests: tuple[str, ...],
    ) -> SemanticCandidateSet: ...

    def has_authority_role(self, *, tenant_id: str, actor_id: str, authority_ref: str) -> bool: ...

    def verify_bundle_candidates(
        self, *, tenant_id: str, candidate_set_digest: str, candidate_ids: tuple[str, ...]
    ) -> None: ...

    def record_observation(
        self,
        *,
        tenant_id: str,
        information_kind: InformationKind,
        source_kind: AuthoritySourceKind,
        subject_ref: str,
        assertion: str,
        authority_ref: str,
        observed_digest: str,
        observed_at: datetime,
        valid_until: datetime,
    ) -> AuthorityObservation: ...

    def load_observation(self, tenant_id: str, observation_id: str) -> AuthorityObservation: ...

    def store_review_bundle(self, bundle: OntologyReviewBundle) -> OntologyReviewBundle: ...

    def compensate_review_bundle_submission(
        self, prior: OntologyReviewBundle, attempted: OntologyReviewBundle
    ) -> None: ...

    def store_review_bundle_with_baselines(
        self, bundle: OntologyReviewBundle, revisions: tuple[SemanticRevision, ...]
    ) -> OntologyReviewBundle: ...

    def load_review_bundle(self, tenant_id: str, bundle_id: str) -> OntologyReviewBundle: ...

    def next_semantic_revision(self, tenant_id: str, bundle_id: str, item_id: str) -> int: ...

    def store_semantic_revision(self, revision: SemanticRevision) -> SemanticRevision: ...

    def store_review_decision(
        self, bundle: OntologyReviewBundle, revision: SemanticRevision | None
    ) -> OntologyReviewBundle: ...


class SemanticVersionRepository(Protocol):
    def store(self, semantic_version: ApprovedSemanticVersion) -> ApprovedSemanticVersion: ...

    def load(
        self, tenant_id: str, semantic_version_id: str, version: int
    ) -> ApprovedSemanticVersion: ...


class SemanticPersistenceError(RuntimeError):
    def __init__(self, *, operation: str) -> None:
        self.operation = operation
        super().__init__(f"semantic persistence failed during {operation}")


class SemanticArtifactConflictError(RuntimeError):
    pass


class SQLiteSemanticRepository:
    def __init__(
        self,
        database_path: str | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Open a database, or borrow a connection the composer already owns.

        The console reads review bundles from a threadpool, and a connection carries
        SQLite's thread affinity, so a repository that opens its own here is bound to
        whichever thread constructed it and fails on the first read from a worker.

        A borrowed connection is never closed by `close()`: the owner closes it.
        """
        if (database_path is None) == (connection is None):
            raise ValueError("supply exactly one of database_path or connection")
        self._owns_connection = connection is None
        try:
            if connection is None:
                assert database_path is not None
                connection = sqlite3.connect(database_path)
            self._connection = connection
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS semantic_candidate_set_sequences ("
                "tenant_id TEXT PRIMARY KEY, next_sequence INTEGER NOT NULL)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS ontology_review_bundle_revisions ("
                "tenant_id TEXT NOT NULL, bundle_id TEXT NOT NULL, revision INTEGER NOT NULL, "
                "payload BLOB NOT NULL, PRIMARY KEY (tenant_id, bundle_id, revision))"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS semantic_revisions ("
                "tenant_id TEXT NOT NULL, bundle_id TEXT NOT NULL, item_id TEXT NOT NULL, "
                "revision INTEGER NOT NULL, payload BLOB NOT NULL, "
                "PRIMARY KEY (tenant_id, bundle_id, item_id, revision))"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS semantic_authority_roles ("
                "tenant_id TEXT NOT NULL, actor_id TEXT NOT NULL, authority_ref TEXT NOT NULL, "
                "PRIMARY KEY (tenant_id, actor_id, authority_ref))"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS semantic_candidate_sequences ("
                "tenant_id TEXT PRIMARY KEY, next_sequence INTEGER NOT NULL)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS authority_observation_sequences ("
                "tenant_id TEXT PRIMARY KEY, next_sequence INTEGER NOT NULL)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS semantic_candidate_set_identities ("
                "tenant_id TEXT NOT NULL, set_id TEXT NOT NULL, set_sequence INTEGER NOT NULL, "
                "package_id TEXT NOT NULL, package_version INTEGER NOT NULL, "
                "original_digest TEXT NOT NULL, manifest_digest TEXT NOT NULL, "
                "PRIMARY KEY (tenant_id, set_id), "
                "UNIQUE (tenant_id, package_id, package_version))"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS semantic_candidate_set_revisions ("
                "tenant_id TEXT NOT NULL, set_id TEXT NOT NULL, revision INTEGER NOT NULL, "
                "extractor_id TEXT NOT NULL, extractor_version TEXT NOT NULL, "
                "material_digest TEXT NOT NULL, payload BLOB NOT NULL, "
                "PRIMARY KEY (tenant_id, set_id, revision), "
                "UNIQUE (tenant_id, set_id, material_digest), "
                "FOREIGN KEY (tenant_id, set_id) REFERENCES "
                "semantic_candidate_set_identities (tenant_id, set_id))"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS authority_observations ("
                "tenant_id TEXT NOT NULL, observation_id TEXT NOT NULL, "
                "observation_sequence INTEGER NOT NULL, material_digest TEXT NOT NULL, "
                "payload BLOB NOT NULL, PRIMARY KEY (tenant_id, observation_id), "
                "UNIQUE (tenant_id, material_digest))"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS approved_semantic_version_sequences ("
                "tenant_id TEXT PRIMARY KEY, next_sequence INTEGER NOT NULL)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS approved_semantic_versions ("
                "tenant_id TEXT NOT NULL, semantic_version_id TEXT NOT NULL, "
                "version INTEGER NOT NULL, "
                "material_digest TEXT NOT NULL, payload BLOB NOT NULL, "
                "PRIMARY KEY (tenant_id, semantic_version_id, version), "
                "UNIQUE (tenant_id, material_digest), UNIQUE (tenant_id, version))"
            )
            self._connection.commit()
        except sqlite3.Error as error:
            raise SemanticPersistenceError(operation="initialize semantic repository") from error

    def close(self) -> None:
        if not self._owns_connection:
            return
        self._connection.close()

    def _materialize(
        self,
        *,
        tenant_id: str,
        package_id: str,
        package_version: int,
        original_digest: str,
        manifest_digest: str,
        extractor_id: str,
        extractor_version: str,
        candidates: tuple[_CandidateDraft, ...],
        unresolved_questions: tuple[str, ...],
        created_at: datetime,
    ) -> SemanticCandidateSet:
        if not tenant_id:
            raise ValueError("tenant_id must not be empty")
        material_digest = _material_digest(
            original_digest=original_digest,
            manifest_digest=manifest_digest,
            extractor_id=extractor_id,
            extractor_version=extractor_version,
            candidates=candidates,
            unresolved_questions=unresolved_questions,
        )
        try:
            with _transaction(self._connection):
                set_id = self._load_or_create_set_identity(
                    tenant_id=tenant_id,
                    package_id=package_id,
                    package_version=package_version,
                    original_digest=original_digest,
                    manifest_digest=manifest_digest,
                )
                replay = self._load_by_material_digest(tenant_id, set_id, material_digest)
                if replay is not None:
                    return replay
                latest_revision = self._latest_revision(tenant_id, set_id)
                next_revision = latest_revision + 1 if latest_revision is not None else 1
                candidate_set = SemanticCandidateSet(
                    set_id=set_id,
                    tenant_id=tenant_id,
                    revision=next_revision,
                    package_id=package_id,
                    package_version=package_version,
                    original_digest=original_digest,
                    manifest_digest=manifest_digest,
                    extractor_id=extractor_id,
                    extractor_version=extractor_version,
                    candidates=tuple(
                        self._materialize_candidate(tenant_id, set_id, draft)
                        for draft in candidates
                    ),
                    unresolved_questions=unresolved_questions,
                    created_at=created_at,
                )
                self._connection.execute(
                    "INSERT INTO semantic_candidate_set_revisions "
                    "(tenant_id, set_id, revision, extractor_id, extractor_version, "
                    "material_digest, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        tenant_id,
                        set_id,
                        next_revision,
                        extractor_id,
                        extractor_version,
                        material_digest,
                        canonical_bytes(candidate_set),
                    ),
                )
            return candidate_set
        except sqlite3.Error as error:
            raise SemanticPersistenceError(
                operation="materialize semantic candidate set"
            ) from error

    def store(self, candidate_set: SemanticCandidateSet) -> SemanticCandidateSet:
        stored = self.load_revision(
            candidate_set.tenant_id,
            candidate_set.set_id,
            candidate_set.revision,
        )
        if canonical_bytes(stored) != canonical_bytes(candidate_set):
            raise SemanticArtifactConflictError(
                "candidate set identity already points to different bytes"
            )
        return stored

    def load_revision(self, tenant_id: str, set_id: str, revision: int) -> SemanticCandidateSet:
        try:
            row = self._connection.execute(
                "SELECT payload FROM semantic_candidate_set_revisions "
                "WHERE tenant_id = ? AND set_id = ? AND revision = ?",
                (tenant_id, set_id, revision),
            ).fetchone()
        except sqlite3.Error as error:
            raise SemanticPersistenceError(operation="load semantic candidate set") from error
        if row is None:
            raise KeyError(f"candidate set {set_id} belongs to another tenant")
        return SemanticCandidateSet.model_validate_json(row[0])

    def owns_candidate(self, tenant_id: str, candidate: SemanticCandidate) -> bool:
        try:
            rows = self._connection.execute(
                "SELECT payload FROM semantic_candidate_set_revisions WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchall()
        except sqlite3.Error as error:
            raise SemanticPersistenceError(
                operation="verify semantic candidate ownership"
            ) from error
        expected_payload = canonical_bytes(candidate)
        return any(
            persisted_candidate.candidate_id == candidate.candidate_id
            and canonical_bytes(persisted_candidate) == expected_payload
            for row in rows
            for persisted_candidate in SemanticCandidateSet.model_validate_json(row[0]).candidates
        )

    def verify_review_inputs(
        self,
        *,
        tenant_id: str,
        candidate_set_id: str,
        candidate_set_revision: int,
        candidate_ids: tuple[str, ...],
        observation_digests: tuple[str, ...],
    ) -> SemanticCandidateSet:
        candidate_set = self.load_revision(tenant_id, candidate_set_id, candidate_set_revision)
        persisted_ids = {candidate.candidate_id for candidate in candidate_set.candidates}
        if not candidate_ids or not set(candidate_ids).issubset(persisted_ids):
            raise ValueError("review candidates must belong to the persisted candidate set")
        rows = self._connection.execute(
            "SELECT payload FROM authority_observations WHERE tenant_id = ?", (tenant_id,)
        ).fetchall()
        persisted_digests = {
            digest(AuthorityObservation.model_validate_json(row[0])) for row in rows
        }
        if not set(observation_digests).issubset(persisted_digests):
            raise ValueError("review observations must belong to the tenant")
        return candidate_set

    def grant_authority_role(self, *, tenant_id: str, actor_id: str, authority_ref: str) -> None:
        with _transaction(self._connection):
            self._connection.execute(
                "INSERT OR IGNORE INTO semantic_authority_roles "
                "(tenant_id, actor_id, authority_ref) VALUES (?, ?, ?)",
                (tenant_id, actor_id, authority_ref),
            )

    def has_authority_role(self, *, tenant_id: str, actor_id: str, authority_ref: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM semantic_authority_roles "
            "WHERE tenant_id = ? AND actor_id = ? AND authority_ref = ?",
            (tenant_id, actor_id, authority_ref),
        ).fetchone()
        return row is not None

    def verify_bundle_candidates(
        self, *, tenant_id: str, candidate_set_digest: str, candidate_ids: tuple[str, ...]
    ) -> None:
        rows = self._connection.execute(
            "SELECT payload FROM semantic_candidate_set_revisions WHERE tenant_id = ?", (tenant_id,)
        ).fetchall()
        for row in rows:
            candidate_set = SemanticCandidateSet.model_validate_json(row[0])
            if digest(candidate_set) != candidate_set_digest:
                continue
            persisted_ids = {candidate.candidate_id for candidate in candidate_set.candidates}
            if candidate_ids and set(candidate_ids).issubset(persisted_ids):
                return
        raise ValueError("merge candidates must belong to the review bundle candidate set")

    def record_observation(
        self,
        *,
        tenant_id: str,
        information_kind: InformationKind,
        source_kind: AuthoritySourceKind,
        subject_ref: str,
        assertion: str,
        authority_ref: str,
        observed_digest: str,
        observed_at: datetime,
        valid_until: datetime,
    ) -> AuthorityObservation:
        if not tenant_id:
            raise ValueError("tenant_id must not be empty")
        material_digest = _observation_material_digest(
            information_kind=information_kind,
            source_kind=source_kind,
            subject_ref=subject_ref,
            assertion=assertion,
            authority_ref=authority_ref,
            observed_digest=observed_digest,
            observed_at=observed_at,
            valid_until=valid_until,
        )
        try:
            with _transaction(self._connection):
                replay = self._load_observation_by_material_digest(tenant_id, material_digest)
                if replay is not None:
                    return replay
                sequence = self._allocate_sequence("authority_observation_sequences", tenant_id)
                observation = AuthorityObservation(
                    observation_id=_observation_id(tenant_id, sequence),
                    tenant_id=tenant_id,
                    information_kind=information_kind,
                    source_kind=source_kind,
                    subject_ref=subject_ref,
                    assertion=assertion,
                    authority_ref=authority_ref,
                    observed_digest=observed_digest,
                    observed_at=observed_at,
                    valid_until=valid_until,
                )
                self._connection.execute(
                    "INSERT INTO authority_observations "
                    "(tenant_id, observation_id, observation_sequence, material_digest, payload) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        tenant_id,
                        observation.observation_id,
                        sequence,
                        material_digest,
                        canonical_bytes(observation),
                    ),
                )
            return observation
        except sqlite3.Error as error:
            raise SemanticPersistenceError(operation="record authority observation") from error

    def load_observation(self, tenant_id: str, observation_id: str) -> AuthorityObservation:
        try:
            row = self._connection.execute(
                "SELECT payload FROM authority_observations "
                "WHERE tenant_id = ? AND observation_id = ?",
                (tenant_id, observation_id),
            ).fetchone()
        except sqlite3.Error as error:
            raise SemanticPersistenceError(operation="load authority observation") from error
        if row is None:
            raise KeyError(f"authority observation {observation_id} belongs to another tenant")
        return AuthorityObservation.model_validate_json(row[0])

    def store_review_bundle(self, bundle: OntologyReviewBundle) -> OntologyReviewBundle:

        try:
            with _transaction(self._connection):
                existing = self._connection.execute(
                    "SELECT revision FROM ontology_review_bundle_revisions "
                    "WHERE tenant_id = ? AND bundle_id = ? ORDER BY revision DESC LIMIT 1",
                    (bundle.tenant_id, bundle.bundle_id),
                ).fetchone()
                if existing is not None and bundle.revision != int(existing[0]) + 1:
                    raise SemanticArtifactConflictError("review bundle revision was not advanced")
                self._connection.execute(
                    "INSERT INTO ontology_review_bundle_revisions "
                    "(tenant_id, bundle_id, revision, payload) VALUES (?, ?, ?, ?)",
                    (bundle.tenant_id, bundle.bundle_id, bundle.revision, canonical_bytes(bundle)),
                )
            return bundle
        except sqlite3.IntegrityError as error:
            raise SemanticArtifactConflictError(
                "review bundle revision was not advanced"
            ) from error
        except sqlite3.Error as error:
            raise SemanticPersistenceError(operation="store review bundle") from error

    def compensate_review_bundle_submission(
        self, prior: OntologyReviewBundle, attempted: OntologyReviewBundle
    ) -> None:
        with _transaction(self._connection):
            current = self.load_review_bundle(prior.tenant_id, prior.bundle_id)
            if canonical_bytes(current) == canonical_bytes(prior):
                return
            if canonical_bytes(current) != canonical_bytes(attempted):
                raise SemanticArtifactConflictError("review bundle is not compensable")
            previous_row = self._connection.execute(
                "SELECT payload FROM ontology_review_bundle_revisions "
                "WHERE tenant_id = ? AND bundle_id = ? AND revision = ?",
                (prior.tenant_id, prior.bundle_id, prior.revision),
            ).fetchone()
            if previous_row is None or previous_row[0] != canonical_bytes(prior):
                raise SemanticArtifactConflictError("prior review bundle changed")
            self._connection.execute(
                "DELETE FROM ontology_review_bundle_revisions "
                "WHERE tenant_id = ? AND bundle_id = ? AND revision = ?",
                (attempted.tenant_id, attempted.bundle_id, attempted.revision),
            )

    def store_review_bundle_with_baselines(
        self, bundle: OntologyReviewBundle, revisions: tuple[SemanticRevision, ...]
    ) -> OntologyReviewBundle:
        try:
            with _transaction(self._connection):
                for revision in revisions:
                    if (
                        revision.tenant_id != bundle.tenant_id
                        or revision.bundle_id != bundle.bundle_id
                    ):
                        raise SemanticArtifactConflictError(
                            "baseline revision does not belong to bundle"
                        )
                    self._connection.execute(
                        "INSERT INTO semantic_revisions "
                        "(tenant_id, bundle_id, item_id, revision, payload) VALUES (?, ?, ?, ?, ?)",
                        (
                            revision.tenant_id,
                            revision.bundle_id,
                            revision.item_id,
                            revision.revision,
                            canonical_bytes(revision),
                        ),
                    )
                self._connection.execute(
                    "INSERT INTO ontology_review_bundle_revisions "
                    "(tenant_id, bundle_id, revision, payload) VALUES (?, ?, ?, ?)",
                    (bundle.tenant_id, bundle.bundle_id, bundle.revision, canonical_bytes(bundle)),
                )
            return bundle
        except sqlite3.IntegrityError as error:
            raise SemanticArtifactConflictError("review baseline was not persisted") from error
        except sqlite3.Error as error:
            raise SemanticPersistenceError(operation="store review bundle baseline") from error

    def load_review_bundle(self, tenant_id: str, bundle_id: str) -> OntologyReviewBundle:
        from .review import OntologyReviewBundle

        owner = self._connection.execute(
            "SELECT tenant_id FROM ontology_review_bundle_revisions WHERE bundle_id = ? "
            "ORDER BY revision DESC LIMIT 1",
            (bundle_id,),
        ).fetchone()
        if owner is None or str(owner[0]) != tenant_id:
            raise KeyError(f"review bundle {bundle_id} belongs to another tenant")
        row = self._connection.execute(
            "SELECT payload FROM ontology_review_bundle_revisions "
            "WHERE tenant_id = ? AND bundle_id = ? ORDER BY revision DESC LIMIT 1",
            (tenant_id, bundle_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("review bundle revision is missing after ownership validation")
        return OntologyReviewBundle.model_validate_json(row[0])

    def next_semantic_revision(self, tenant_id: str, bundle_id: str, item_id: str) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM semantic_revisions "
            "WHERE tenant_id = ? AND bundle_id = ? AND item_id = ?",
            (tenant_id, bundle_id, item_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("semantic revision allocation did not return a revision")
        return int(row[0])

    def store_semantic_revision(self, revision: SemanticRevision) -> SemanticRevision:
        try:
            self._connection.execute(
                "INSERT INTO semantic_revisions (tenant_id, bundle_id, item_id, revision, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    revision.tenant_id,
                    revision.bundle_id,
                    revision.item_id,
                    revision.revision,
                    canonical_bytes(revision),
                ),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as error:
            self._connection.rollback()
            raise SemanticArtifactConflictError("semantic revision was not advanced") from error
        except sqlite3.Error as error:
            self._connection.rollback()
            raise SemanticPersistenceError(operation="store semantic revision") from error
        return revision

    def store_review_decision(
        self, bundle: OntologyReviewBundle, revision: SemanticRevision | None
    ) -> OntologyReviewBundle:
        if revision is not None:
            persisted_bundle = self.load_review_bundle(bundle.tenant_id, bundle.bundle_id)
            persisted_item_ids = {item.item_id for item in persisted_bundle.items}
            item = next(
                (value for value in bundle.items if value.item_id == revision.item_id), None
            )
            if (
                revision.tenant_id != bundle.tenant_id
                or revision.bundle_id != bundle.bundle_id
                or revision.item_id not in persisted_item_ids
                or item is None
                or item.semantic_revision_digest != digest(revision)
            ):
                raise ValueError("semantic revision identity does not belong to review item")
            next_revision = self.next_semantic_revision(
                revision.tenant_id, revision.bundle_id, revision.item_id
            )
            if revision.revision != next_revision:
                raise ValueError("semantic revision is not the exact next revision")
        try:
            with _transaction(self._connection):
                existing = self._connection.execute(
                    "SELECT revision FROM ontology_review_bundle_revisions "
                    "WHERE tenant_id = ? AND bundle_id = ? ORDER BY revision DESC LIMIT 1",
                    (bundle.tenant_id, bundle.bundle_id),
                ).fetchone()
                if existing is None or bundle.revision != int(existing[0]) + 1:
                    raise SemanticArtifactConflictError("review bundle revision was not advanced")
                if revision is not None:
                    self._connection.execute(
                        "INSERT INTO semantic_revisions "
                        "(tenant_id, bundle_id, item_id, revision, payload) VALUES (?, ?, ?, ?, ?)",
                        (
                            revision.tenant_id,
                            revision.bundle_id,
                            revision.item_id,
                            revision.revision,
                            canonical_bytes(revision),
                        ),
                    )
                self._connection.execute(
                    "INSERT INTO ontology_review_bundle_revisions "
                    "(tenant_id, bundle_id, revision, payload) VALUES (?, ?, ?, ?)",
                    (bundle.tenant_id, bundle.bundle_id, bundle.revision, canonical_bytes(bundle)),
                )
            return bundle
        except sqlite3.IntegrityError as error:
            raise SemanticArtifactConflictError("review decision was not persisted") from error
        except sqlite3.Error as error:
            raise SemanticPersistenceError(operation="store review decision") from error

    def _load_observation_by_material_digest(
        self, tenant_id: str, material_digest: str
    ) -> AuthorityObservation | None:
        row = self._connection.execute(
            "SELECT payload FROM authority_observations "
            "WHERE tenant_id = ? AND material_digest = ?",
            (tenant_id, material_digest),
        ).fetchone()
        if row is None:
            return None
        return AuthorityObservation.model_validate_json(row[0])

    def _load_or_create_set_identity(
        self,
        *,
        tenant_id: str,
        package_id: str,
        package_version: int,
        original_digest: str,
        manifest_digest: str,
    ) -> str:
        row = self._connection.execute(
            "SELECT set_id, original_digest, manifest_digest "
            "FROM semantic_candidate_set_identities "
            "WHERE tenant_id = ? AND package_id = ? AND package_version = ?",
            (tenant_id, package_id, package_version),
        ).fetchone()
        if row is not None:
            if str(row[1]) != original_digest or str(row[2]) != manifest_digest:
                raise SemanticArtifactConflictError(
                    "package identity already points to different source digests"
                )
            return str(row[0])
        sequence = self._allocate_sequence("semantic_candidate_set_sequences", tenant_id)
        set_id = _set_id(tenant_id, sequence)
        self._connection.execute(
            "INSERT INTO semantic_candidate_set_identities "
            "(tenant_id, set_id, set_sequence, package_id, package_version, "
            "original_digest, manifest_digest) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                tenant_id,
                set_id,
                sequence,
                package_id,
                package_version,
                original_digest,
                manifest_digest,
            ),
        )
        return set_id

    def _load_by_material_digest(
        self, tenant_id: str, set_id: str, material_digest: str
    ) -> SemanticCandidateSet | None:
        row = self._connection.execute(
            "SELECT payload FROM semantic_candidate_set_revisions "
            "WHERE tenant_id = ? AND set_id = ? AND material_digest = ?",
            (tenant_id, set_id, material_digest),
        ).fetchone()
        if row is None:
            return None
        return SemanticCandidateSet.model_validate_json(row[0])

    def _latest_revision(self, tenant_id: str, set_id: str) -> int | None:
        row = self._connection.execute(
            "SELECT revision "
            "FROM semantic_candidate_set_revisions "
            "WHERE tenant_id = ? AND set_id = ? ORDER BY revision DESC LIMIT 1",
            (tenant_id, set_id),
        ).fetchone()
        if row is None:
            return None
        return int(row[0])

    def _materialize_candidate(
        self, tenant_id: str, set_id: str, draft: _CandidateDraft
    ) -> SemanticCandidate:
        sequence = self._allocate_sequence("semantic_candidate_sequences", tenant_id)
        return SemanticCandidate(
            candidate_id=_candidate_id(tenant_id, set_id, draft.kind, sequence),
            kind=draft.kind,
            name=draft.name,
            proposed_definition=draft.proposed_definition,
            related_refs=draft.related_refs,
            provenance=draft.provenance,
            confidence=draft.confidence,
        )

    def _allocate_sequence(self, table: str, tenant_id: str) -> int:
        if table not in {
            "authority_observation_sequences",
            "semantic_candidate_set_sequences",
            "semantic_candidate_sequences",
            "approved_semantic_version_sequences",
        }:
            raise ValueError("semantic sequence table is not recognized")
        row = self._connection.execute(
            f"INSERT INTO {table} (tenant_id, next_sequence) VALUES (?, 2) "
            f"ON CONFLICT(tenant_id) DO UPDATE SET next_sequence = {table}.next_sequence + 1 "
            "RETURNING next_sequence - 1",
            (tenant_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("semantic sequence allocation did not return a sequence")
        return int(row[0])


def _material_digest(
    *,
    original_digest: str,
    manifest_digest: str,
    extractor_id: str,
    extractor_version: str,
    candidates: tuple[_CandidateDraft, ...],
    unresolved_questions: tuple[str, ...],
) -> str:
    return digest(
        {
            "domain": "pillarmesh-semantic-candidate-material-v1",
            "original_digest": original_digest,
            "manifest_digest": manifest_digest,
            "extractor_id": extractor_id,
            "extractor_version": extractor_version,
            "candidates": tuple(
                {
                    "kind": candidate.kind,
                    "name": candidate.name,
                    "proposed_definition": candidate.proposed_definition,
                    "related_refs": candidate.related_refs,
                    "provenance": candidate.provenance,
                    "confidence": candidate.confidence,
                }
                for candidate in candidates
            ),
            "unresolved_questions": unresolved_questions,
        }
    )


def _observation_material_digest(
    *,
    information_kind: InformationKind,
    source_kind: AuthoritySourceKind,
    subject_ref: str,
    assertion: str,
    authority_ref: str,
    observed_digest: str,
    observed_at: datetime,
    valid_until: datetime,
) -> str:
    return digest(
        {
            "domain": "pillarmesh-authority-observation-material-v1",
            "information_kind": information_kind,
            "source_kind": source_kind,
            "subject_ref": subject_ref,
            "assertion": assertion,
            "authority_ref": authority_ref,
            "observed_digest": observed_digest,
            "observed_at": observed_at,
            "valid_until": valid_until,
        }
    )


def _set_id(tenant_id: str, sequence: int) -> str:
    return (
        "semset-"
        + digest(
            {
                "domain": "pillarmesh-semantic-candidate-set-v1",
                "tenant_id": tenant_id,
                "sequence": sequence,
            }
        )[:24]
    )


def _candidate_id(tenant_id: str, set_id: str, kind: CandidateKind, sequence: int) -> str:
    return (
        "semcand-"
        + digest(
            {
                "domain": "pillarmesh-semantic-candidate-v1",
                "tenant_id": tenant_id,
                "set_id": set_id,
                "kind": kind,
                "sequence": sequence,
            }
        )[:24]
    )


def _observation_id(tenant_id: str, sequence: int) -> str:
    return (
        "authobs-"
        + digest(
            {
                "domain": "pillarmesh-authority-observation-v1",
                "tenant_id": tenant_id,
                "sequence": sequence,
            }
        )[:24]
    )


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
        connection.commit()
    except BaseException:
        with suppress(Exception):
            connection.rollback()
        raise
