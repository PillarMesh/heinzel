# Plan 2 Catalog and Semantic Formation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provision and validate a real managed OpenMetadata catalog, turn an immutable business-process package into attributable semantic candidates, resolve authority per information kind, complete ontology review through the architect inbox, form an approved semantic version and Integration Contract, and publish the approved result without making OpenMetadata or extraction authoritative.

**Architecture:** PillarMesh's semantic registry remains the system of record; OpenMetadata 1.13.3 is a replaceable managed catalog experience behind provider-neutral catalog contracts. Immutable, digest-bound artifacts connect package input, deterministic extraction, authority observations, review decisions, approved semantics, Integration Contract formation, catalog publication, drift detection, and cleanup evidence. Real local provisioning proves the lifecycle while production-cloud provisioning, external-catalog federation, source acquisition, activation, and AI extraction remain outside this plan.

**Tech Stack:** Python 3.13, frozen Pydantic v2 models, SQLite reference repositories, `uv`, pytest, Hypothesis, Ruff, strict mypy, Docker Compose, OpenMetadata 1.13.3 with the PostgreSQL and Elasticsearch versions that release publishes.

**Spec:** `docs/superpowers/reviews/2026-08-18-plan-2-design-review.md`

## Global Constraints

- `AGENTS.md` governs; read it, the review specification, the managed-platform addendum, ADR-0003, and nearby code before each task.
- Authority is resolved by `(information_kind, source_kind)`, never by one global ranking. A cross-kind conflict is unresolved and requires the business owner.
- For business meaning and process semantics, an approved owner decision is authoritative and the uploaded package is stronger evidence than an imported glossary. For imported glossary and classification, the declared catalog authority is authoritative.
- OpenMetadata is authoritative only for imported glossary/classification observations and catalog presentation. It is never authoritative for PillarMesh process, contract, legality, approval, or execution state.
- Every durable public artifact is frozen, strict about unknown input, tenant-scoped, canonically serializable, and digest-addressable. Every timestamp is timezone-aware UTC.
- IDs derive from a domain tag, tenant ID, and repository-assigned sequence. Clocks never contribute to identity.
- Operational endpoints, credentials, infrastructure IDs, encryption material, backup locations, and OpenMetadata tokens remain in private operational state and never enter semantic artifacts, requests, prompts, logs, or exported evidence.
- Cross-tenant authority is denied before artifact retrieval. Repositories never load a payload and then decide whether the caller owns it.
- A material candidate, authority observation, semantic revision, contract, or catalog observation change invalidates approvals bound to the earlier digest.
- Required unresolved ownership, identity, lifecycle, relationship, constraint, reconciliation, classification, retention, access, or metric meaning yields explicit `No Valid Plan`; no default may weaken the contract.
- OpenMetadata publication is idempotent by stable semantic identity. A partial or ambiguous response must converge without duplicate catalog objects.
- Real infrastructure creation and cleanup use an exact private resource ledger. Cleanup acts only on recorded identifiers and records terminal cleanup status.
- The bundled path provisions OpenMetadata only when no supported catalog exists, per addendum section 9.1. External catalog federation is deferred.
- The deterministic extractor is the only Plan 2 extractor. A future AI adapter must implement the same contract and remains untrusted.
- Plan 2 forms an Integration Contract and its formation verdict. It does not activate the contract, compile a physical plan, access a provider, schedule a run, or mutate a warehouse.
- Pin local acceptance to official OpenMetadata release `1.13.3-release`; record image digests before merging rather than relying on mutable tags.
- Take the PostgreSQL and Elasticsearch versions from OpenMetadata 1.13.3's own published `docker-compose` rather than choosing them independently. OpenMetadata supports a narrow search-engine range, and an independently chosen Elasticsearch major will fail at stack start for a reason unrelated to this design. Record the versions actually used, with digests, in Task 3 Step 1 before writing the Compose file.
- Every behavior change follows RED → GREEN, includes a failure or denial test, and ends with a focused review and one Conventional Commit.

## File and ownership map

- `services/catalog-control/`: public catalog-binding lifecycle, validation artifacts, private resource ledger, and provider-neutral provisioning/publication protocols.
- `providers/openmetadata/`: OpenMetadata-specific REST translation, stable identity mapping, real local provisioning adapter, and provider conformance fixtures.
- `services/semantic-registry/`: candidate sets, authority observations and resolution, review bundles, approved semantic versions, publication intents/receipts, and drift proposals.
- `services/contract/`: consumes an approved semantic version and exact approvals to form the managed Integration Contract; retains process-package ownership.
- `packages/contract-model/`: permanent customer-owned managed Integration Contract v2 models, plus the shared `InformationKind` and `SemanticRuleKind` vocabularies. Existing historical M0 `IntegrationContract` remains compatible and unchanged.
- `services/request-management/`: adds the discriminated `schema_semantic_change` request payload and review decision kinds without weakening existing request payloads or lifecycle transitions.
- `providers/openmetadata/` owns vendor APIs and identifiers; no OpenMetadata response model leaks into services.
- `tests/emulators/openmetadata/`: pinned host-private local stack and lifecycle scripts.
- `tests/conformance/`: addendum-to-code shape and lifecycle checks plus cross-component contract tests.
- `tests/end-to-end/`: package-to-contract-to-catalog journey and `No Valid Plan` path.

---

### Task 1: Ratify Plan 2 contracts and repository boundaries

**Files:**
- Modify: `docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md`
- Modify: `docs/architecture/decisions/ADR-0003-managed-data-engineering-platform.md`
- Modify: `docs/architecture/repository-layout.md`
- Modify: `tests/repository-structure/validate.sh`
- Modify: `tests/conformance/test_specification_conformance.py`

**Interfaces:**
- Consumes: addendum sections 8.4, 9.1-9.3, 10, 13.2, 13.3.1, and 14; the review specification's required corrections.
- Produces: normative `CatalogBinding` fields, catalog transition table, semantic artifact fields, per-information-kind authority table, review request lifecycle, widened decision vocabulary, and managed Integration Contract v2 field list used by Tasks 2-8.

- [ ] **Step 1: Add failing conformance tests for the missing normative sections**

```python
def _fenced_fields(heading: str) -> list[str]:
    return [line.split()[0] for line in _fenced_block(heading).strip().splitlines() if line.strip()]


def test_addendum_defines_catalog_binding_fields() -> None:
    assert _fenced_fields("### 9.1.1") == [
        "CatalogBinding",
        "schema_version",
        "binding_id",
        "tenant_id",
        "provider_kind",
        "deployment_mode",
        "capability_profile_digest",
        "lifecycle_state",
        "revision",
        "created_at",
        "updated_at",
        "provisioned_at",
    ]


def test_addendum_defines_catalog_transition_table() -> None:
    assert _documented_transitions("### 9.1.2") == {
        "draft": {"provisioning", "retired"},
        "provisioning": {"validating", "failed"},
        "validating": {"ready", "failed"},
        "ready": {"suspended", "retiring"},
        "suspended": {"ready", "retiring"},
        "retiring": {"retired"},
        "failed": {"retired"},
        "retired": set(),
    }
```

- [ ] **Step 2: Run the new tests and verify the specification is incomplete**

Run: `uv run pytest tests/conformance/test_specification_conformance.py -q`

Expected: FAIL because sections `9.1.1` and `9.1.2` do not exist.

- [ ] **Step 3: Add the exact normative artifact shapes and behavior to the addendum**

Add section 9.1.1 with this field order:

```text
CatalogBinding
  schema_version             1
  binding_id
  tenant_id
  provider_kind              openmetadata
  deployment_mode            pillarmesh_managed
  capability_profile_digest
  lifecycle_state
  revision
  created_at
  updated_at
  provisioned_at
```

Add section 9.1.2 with the transition table tested above. State that `provisioned_at` remains null until positive and denial validation succeeds, and that private operational state is excluded.

Add normative field lists for:

```text
SemanticCandidateSet
  schema_version set_id tenant_id revision package_id package_version
  original_digest manifest_digest extractor_id extractor_version
  candidates unresolved_questions created_at

AuthorityObservation
  schema_version observation_id tenant_id information_kind source_kind
  subject_ref assertion authority_ref observed_digest observed_at valid_until

OntologyReviewBundle
  schema_version bundle_id tenant_id revision candidate_set_digest
  authority_observation_digests items required_authority_refs status created_at updated_at

ApprovedSemanticVersion
  schema_version semantic_version_id tenant_id version process_package_ref
  candidate_set_digest review_bundle_digest entities events states relationships
  identity_rules constraints metrics classifications authority_bindings approval_ids created_at

ManagedIntegrationContract
  schema_version contract_id tenant_id version formation_status semantic_version_ref
  source_observation_refs mappings integrity_constraints destination_product
  freshness quality trigger_policy access_policy evidence_policy failure_policy approval_ids
```

Document the exact authority rule, `schema_semantic_change` request path, item outcomes `accept`, `reject`, `revise`, `merge`, and `unresolved` as a vocabulary distinct from the unchanged request-level `DecisionKind`, post-publication drift behavior, and private resource-ledger cleanup requirement. State that `unresolved` returns the request to `investigating`; only there may it enter `no_valid_plan`.

- [ ] **Step 4: Register new governed components**

Add `services/catalog-control` and `services/semantic-registry` to the repository layout, ADR-0003, and the exact service allowlist in `tests/repository-structure/validate.sh`. Add `providers/openmetadata` to the ADR provider inventory; provider directories remain governed by the existing provider boundary.

Architecture rationale: separate catalog operations from semantic authority so replacing OpenMetadata cannot change approved meaning or contract legality.

- [ ] **Step 5: Run documentation and structure checks**

Run: `uv run pytest tests/conformance/test_specification_conformance.py -q && ./tests/repository-structure/test.sh`

Expected: conformance tests PASS and structure validation PASS before implementation directories are introduced.

- [ ] **Step 6: Commit the specification ratification**

```bash
git add docs/architecture/specifications/managed-data-engineering-platform-addendum-v0.1.md docs/architecture/decisions/ADR-0003-managed-data-engineering-platform.md docs/architecture/repository-layout.md tests/conformance/test_specification_conformance.py tests/repository-structure/validate.sh
git commit -m "docs: ratify catalog and semantic formation contracts"
```

---

### Task 2: Implement catalog binding lifecycle and private resource ledger

**Files:**
- Create: `services/catalog-control/pyproject.toml`
- Create: `services/catalog-control/src/pillarmesh_catalog_control/__init__.py`
- Create: `services/catalog-control/src/pillarmesh_catalog_control/models.py`
- Create: `services/catalog-control/src/pillarmesh_catalog_control/protocols.py`
- Create: `services/catalog-control/src/pillarmesh_catalog_control/repository.py`
- Create: `services/catalog-control/src/pillarmesh_catalog_control/service.py`
- Create: `services/catalog-control/src/pillarmesh_catalog_control/py.typed`
- Create: `services/catalog-control/tests/test_models.py`
- Create: `services/catalog-control/tests/test_service.py`
- Create: `services/catalog-control/tests/test_resource_ledger.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `tests/conformance/test_specification_conformance.py`

**Interfaces:**
- Consumes: `ArtifactModel`, `digest`, injected UTC clock, addendum sections 9.1.1 and 9.1.2.
- Produces: `CatalogBinding`, `CatalogBindingState`, `CatalogControlService`, `CatalogRepository`, `CatalogProvisioner`, `CatalogValidator`, `PrivateCatalogResource`, and `SQLiteCatalogRepository`.

- [ ] **Step 1: Write failing model and lifecycle tests**

```python
def test_catalog_binding_rejects_unknown_fields_and_non_utc_time() -> None:
    with pytest.raises(ValidationError):
        CatalogBinding.model_validate(binding_payload() | {"endpoint": "http://secret"})
    with pytest.raises(ValidationError):
        CatalogBinding.model_validate(binding_payload() | {"created_at": datetime.now()})


def test_ready_requires_successful_positive_and_denial_validation(
    service: CatalogControlService,
) -> None:
    draft = service.create_draft(tenant_id="tenant-a")
    provisioning = service.transition(
        "tenant-a",
        draft.binding_id,
        CatalogBindingState.PROVISIONING,
        expected_revision=1,
    )
    validating = service.transition(
        "tenant-a",
        draft.binding_id,
        CatalogBindingState.VALIDATING,
        expected_revision=2,
    )
    with pytest.raises(ValueError, match="validation evidence"):
        service.transition(
            "tenant-a",
            validating.binding_id,
            CatalogBindingState.READY,
            expected_revision=3,
        )
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `uv run pytest services/catalog-control/tests -q`

Expected: collection FAIL because `pillarmesh_catalog_control` does not exist.

- [ ] **Step 3: Define strict public models and protocols**

```python
class CatalogBindingState(StrEnum):
    DRAFT = "draft"
    PROVISIONING = "provisioning"
    VALIDATING = "validating"
    READY = "ready"
    SUSPENDED = "suspended"
    RETIRING = "retiring"
    RETIRED = "retired"
    FAILED = "failed"


class CatalogBinding(ArtifactModel):
    schema_version: Literal["1"] = "1"
    binding_id: str
    tenant_id: str
    provider_kind: Literal["openmetadata"] = "openmetadata"
    deployment_mode: Literal["pillarmesh_managed"] = "pillarmesh_managed"
    capability_profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    lifecycle_state: CatalogBindingState
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    provisioned_at: datetime | None = None


class CatalogProvisioner(Protocol):
    def provision(self, *, tenant_id: str, binding_id: str, operation_id: str) -> str: ...
    def suspend(self, *, private_resource_handle: str, operation_id: str) -> None: ...
    def resume(self, *, private_resource_handle: str, operation_id: str) -> None: ...
    def retire(self, *, private_resource_handle: str, operation_id: str) -> None: ...


class CatalogValidator(Protocol):
    def validate(
        self, *, tenant_id: str, binding_id: str, private_resource_handle: str
    ) -> CatalogValidationEvidence: ...
```

`CatalogValidationEvidence` contains only `evidence_id`, `tenant_id`, `binding_id`, `binding_revision`, `provider_version`, `positive_probe_digest`, `denial_probe_digest`, `stable_identity_probe_digest`, `backup_probe_digest`, and `observed_at`. It contains no endpoint or provider object ID.

`CatalogControlService` exposes these exact operations:

```python
def create_draft(self, *, tenant_id: str) -> CatalogBinding: ...
def transition(
    self, tenant_id: str, binding_id: str, state: CatalogBindingState, *, expected_revision: int
) -> CatalogBinding: ...
def record_validation(
    self,
    *,
    tenant_id: str,
    binding_id: str,
    expected_revision: int,
    evidence: CatalogValidationEvidence,
) -> CatalogBinding: ...
def get(self, tenant_id: str, binding_id: str) -> CatalogBinding: ...
```

- [ ] **Step 4: Implement append-only SQLite binding revisions and private resources**

Use one transaction for sequence allocation, binding revision, validation evidence, and the corresponding private resource state when the lifecycle step depends on all of them. Define private state as:

```python
class CatalogResourceKind(StrEnum):
    COMPOSE_PROJECT = "compose_project"
    CATALOG_SERVICE = "catalog_service"
    CATALOG_DATABASE = "catalog_database"
    SEARCH_INDEX = "search_index"
    SERVICE_ACCOUNT = "service_account"
    TENANT_NAMESPACE = "tenant_namespace"
    BACKUP_ARTIFACT = "backup_artifact"


@dataclass(frozen=True, slots=True)
class PrivateCatalogResource:
    tenant_id: str
    binding_id: str
    resource_id: str
    resource_kind: CatalogResourceKind
    provider_ref: str
    creation_state: Literal["planned", "created", "validated", "failed"]
    retention_deadline: datetime
    cleanup_status: Literal["not_started", "in_progress", "complete", "failed"]
    created_at: datetime
    cleaned_at: datetime | None
```

Do not export `PrivateCatalogResource` from the package root. Repository queries always include `tenant_id` in the first lookup.

- [ ] **Step 5: Add stale-writer, rollback, cross-tenant, and cleanup-denial tests**

```python
def test_cleanup_rejects_unrecorded_provider_identifier(
    repository: SQLiteCatalogRepository,
) -> None:
    with pytest.raises(KeyError, match="recorded resource"):
        repository.begin_cleanup("tenant-a", "not-in-ledger")


def test_cross_tenant_load_is_denied_before_validation_payload_is_read(repository) -> None:
    binding = repository.create_draft("tenant-a", fixed_clock())
    with pytest.raises(KeyError, match="another tenant"):
        repository.load("tenant-b", binding.binding_id)
```

- [ ] **Step 6: Pin addendum fields and transitions to implementation**

Extend `tests/conformance/test_specification_conformance.py` to compare section 9.1.1 with `CatalogBinding.model_fields` and section 9.1.2 with catalog-control `_TRANSITIONS` exactly.

- [ ] **Step 7: Run component gates**

Run: `uv run pytest services/catalog-control/tests tests/conformance/test_specification_conformance.py -q && uv run ruff check services/catalog-control tests/conformance && uv run mypy`

Expected: all PASS.

- [ ] **Step 8: Commit catalog control**

```bash
git add services/catalog-control pyproject.toml uv.lock tests/conformance/test_specification_conformance.py
git commit -m "feat(catalog-control): add managed catalog lifecycle"
```

---

### Task 3: Provision and validate real local OpenMetadata

**Files:**
- Create: `providers/openmetadata/pyproject.toml`
- Create: `providers/openmetadata/src/pillarmesh_provider_openmetadata/__init__.py`
- Create: `providers/openmetadata/src/pillarmesh_provider_openmetadata/client.py`
- Create: `providers/openmetadata/src/pillarmesh_provider_openmetadata/provisioner.py`
- Create: `providers/openmetadata/src/pillarmesh_provider_openmetadata/models.py`
- Create: `providers/openmetadata/src/pillarmesh_provider_openmetadata/py.typed`
- Create: `providers/openmetadata/tests/test_client.py`
- Create: `providers/openmetadata/tests/test_provisioner.py`
- Create: `tests/emulators/openmetadata/compose.yaml`
- Create: `tests/emulators/openmetadata/run.sh`
- Create: `tests/emulators/openmetadata/wait_ready.py`
- Create: `tests/emulators/test_openmetadata_harness.py`
- Create: `tests/integration/test_openmetadata_live.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: Task 2 `CatalogProvisioner`, `CatalogValidator`, `CatalogValidationEvidence`, and private resource handles.
- Produces: `OpenMetadataProvisioner`, `OpenMetadataClient`, `OpenMetadataSettings`, and a pinned host-private acceptance stack.

- [ ] **Step 1: Write offline tests for provider exception isolation and host-private Compose**

```python
def test_transport_exception_is_classified_without_leaking_httpx() -> None:
    transport = FailingTransport(ConnectError("refused"))
    client = OpenMetadataClient(settings(), transport=transport)
    with pytest.raises(CatalogProviderError) as captured:
        client.health()
    assert captured.value.classification == "transient"


def test_openmetadata_ports_are_bound_to_loopback(compose_config: dict[str, object]) -> None:
    assert published_hosts(compose_config) == {"127.0.0.1"}


def test_openmetadata_images_are_pinned_to_the_upstream_release(
    compose_config: dict[str, object],
) -> None:
    # UPSTREAM_IMAGES is transcribed in Step 1 from OpenMetadata 1.13.3's own published
    # compose file, with digests, and is not chosen here. Asserting a hand-picked
    # Elasticsearch major would pin a combination upstream never ships.
    assert image_tags(compose_config) == UPSTREAM_IMAGES
    assert all("@sha256:" in image for image in image_tags(compose_config))
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest providers/openmetadata/tests tests/emulators/test_openmetadata_harness.py -q`

Expected: collection FAIL because the provider and Compose stack do not exist.

- [ ] **Step 3: Implement narrow provider operations**

```python
class OpenMetadataClient:
    def health(self) -> ProviderHealth: ...
    def ensure_tenant_namespace(
        self, *, tenant_key: str, idempotency_key: str
    ) -> CatalogObjectRef: ...
    def ensure_glossary_term(
        self, *, tenant_key: str, identity: str, payload: GlossaryTermPayload, idempotency_key: str
    ) -> CatalogObjectRef: ...
    def ensure_classification(
        self,
        *,
        tenant_key: str,
        identity: str,
        payload: ClassificationPayload,
        idempotency_key: str,
    ) -> CatalogObjectRef: ...
    def ensure_lineage(
        self, *, tenant_key: str, identity: str, payload: LineagePayload, idempotency_key: str
    ) -> CatalogObjectRef: ...
    def get_object(self, *, tenant_key: str, identity: str) -> CatalogObjectSnapshot: ...
```

Use these provider-local boundaries:

```python
type CatalogFailureClassification = Literal[
    "transient",
    "throttled",
    "authentication",
    "authorization",
    "conflict",
    "invalid_request",
    "permanent",
]


class CatalogProviderError(RuntimeError):
    def __init__(self, message: str, *, classification: CatalogFailureClassification) -> None: ...


class CatalogObjectRef(ArtifactModel):
    stable_identity: str
    normalized_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class CatalogObjectSnapshot(ArtifactModel):
    stable_identity: str
    object_kind: Literal["namespace", "glossary_term", "classification", "lineage"]
    normalized_payload: dict[str, str | tuple[str, ...]]
    normalized_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProviderHealth(ArtifactModel):
    status: Literal["healthy"]
    provider_version: Literal["1.13.3"]


class GlossaryTermPayload(ArtifactModel):
    name: str
    definition: str
    owner_ref: str
    provenance_ref: str


class ClassificationPayload(ArtifactModel):
    subject_ref: str
    classification_ref: str
    provenance_ref: str


class LineagePayload(ArtifactModel):
    from_ref: str
    to_ref: str
    producer_ref: str
    evidence_ref: str
```

Validate every response into provider-local strict models. Translate transport, availability, throttling, authentication, authorization, conflict, validation, and server failures into `CatalogProviderError` classifications. Never expose raw HTTP response bodies in errors.

- [ ] **Step 4: Implement local provisioning using exact Compose project and private handles**

`OpenMetadataProvisioner.provision()` creates an isolated project name derived from the opaque operation ID, records every Compose resource in Task 2's private ledger, waits for terminal readiness, bootstraps the tenant namespace and separate runtime/admin identities, and returns only the private resource handle. Replaying the same operation ID returns the same handle.

- [ ] **Step 5: Implement positive and denial validation**

Positive probes create and retrieve a temporary tenant-scoped glossary term, classification, ownership association, and lineage edge. Denial probes verify the runtime identity cannot perform administration or retrieve another acceptance tenant's namespace. Delete the temporary objects only through exact recorded IDs; retain their digests in `CatalogValidationEvidence`.

- [ ] **Step 6: Add opt-in real lifecycle test**

```python
@pytest.mark.live
@pytest.mark.emulator
def test_real_catalog_lifecycle_round_trip(local_openmetadata: LocalOpenMetadata) -> None:
    binding = local_openmetadata.provision_and_validate("tenant-a")
    assert binding.lifecycle_state is CatalogBindingState.READY
    local_openmetadata.suspend(binding)
    local_openmetadata.resume(binding)
    restored = local_openmetadata.backup_restore_and_probe(binding)
    assert restored.representative_objects_verified is True
    local_openmetadata.retire(binding)
    assert local_openmetadata.resource_ledger(binding).all_cleaned
```

- [ ] **Step 7: Verify offline and opt-in paths**

Run offline: `uv run pytest providers/openmetadata/tests tests/emulators/test_openmetadata_harness.py -q`

Run real local: `tests/emulators/openmetadata/run.sh pytest tests/integration/test_openmetadata_live.py -q`

Expected: offline tests PASS; real run creates new resources, reaches terminal readiness, exercises positive and denial probes, restores representative metadata, and records complete cleanup.

- [ ] **Step 8: Commit OpenMetadata provider**

```bash
git add providers/openmetadata tests/emulators/openmetadata tests/emulators/test_openmetadata_harness.py tests/integration/test_openmetadata_live.py pyproject.toml uv.lock
git commit -m "feat(openmetadata): provision managed local catalog"
```

---

### Task 4: Extract immutable semantic candidates deterministically

**Files:**
- Create: `services/semantic-registry/pyproject.toml`
- Create: `services/semantic-registry/src/pillarmesh_semantic_registry/__init__.py`
- Create: `services/semantic-registry/src/pillarmesh_semantic_registry/models.py`
- Create: `services/semantic-registry/src/pillarmesh_semantic_registry/extractor.py`
- Create: `services/semantic-registry/src/pillarmesh_semantic_registry/repository.py`
- Create: `services/semantic-registry/src/pillarmesh_semantic_registry/py.typed`
- Create: `services/semantic-registry/tests/test_models.py`
- Create: `services/semantic-registry/tests/test_extractor.py`
- Create: `services/semantic-registry/tests/fixtures/revenue_to_cash.md`
- Modify: `tests/conformance/test_specification_conformance.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: `ProcessPackageReceipt`, `BusinessProcessManifest`, exact original bytes from `ProcessPackageService.get_original()`, canonical `digest()`.
- Produces: `SemanticCandidateSet`, `SemanticCandidate`, `CandidateProvenance`, `CandidateKind`, `SemanticCandidateExtractor`, `DeterministicManifestExtractor`, and `SemanticRepository`.

- [ ] **Step 1: Write golden extraction and rerun immutability tests**

```python
def test_refund_definition_comes_from_attributable_narrative_marker() -> None:
    candidate_set = extractor.extract(package_input())
    refund = next(item for item in candidate_set.candidates if item.name == "Refund")
    assert refund.kind is CandidateKind.ENTITY
    assert refund.proposed_definition == "A repayment with its own lifecycle."
    assert refund.provenance.narrative_line_start == 14
    assert refund.provenance.source_digest == package_input().original_digest


def test_extractor_version_change_appends_candidate_set_revision(repository) -> None:
    first = repository.store(extractor_v1.extract(package_input()))
    second = repository.store(extractor_v2.extract(package_input()))
    assert second.revision == first.revision + 1
    assert repository.load_revision("tenant-a", first.set_id, 1) == first
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest services/semantic-registry/tests/test_models.py services/semantic-registry/tests/test_extractor.py -q`

Expected: collection FAIL because semantic-registry does not exist.

- [ ] **Step 3: Define candidate contracts**

```python
class CandidateKind(StrEnum):
    ENTITY = "entity"
    EVENT = "event"
    STATE = "state"
    RELATIONSHIP = "relationship"
    IDENTITY_RULE = "identity_rule"
    INTEGRITY_CONSTRAINT = "integrity_constraint"
    METRIC = "metric"
    CLASSIFICATION = "classification"


class CandidateProvenance(ArtifactModel):
    source_kind: Literal["manifest", "narrative_marker"]
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_path: str
    narrative_line_start: int | None = Field(default=None, ge=1)
    narrative_line_end: int | None = Field(default=None, ge=1)


class SemanticCandidate(ArtifactModel):
    candidate_id: str
    kind: CandidateKind
    name: str
    proposed_definition: str | None
    related_refs: tuple[str, ...]
    provenance: CandidateProvenance
    confidence: Decimal = Field(ge=0, le=1)


class SemanticCandidateSet(ArtifactModel):
    schema_version: Literal["1"] = "1"
    set_id: str
    tenant_id: str
    revision: int = Field(ge=1)
    package_id: str
    package_version: int = Field(ge=1)
    original_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    extractor_id: str
    extractor_version: str
    candidates: tuple[SemanticCandidate, ...]
    unresolved_questions: tuple[str, ...]
    created_at: datetime


class SemanticCandidateExtractor(Protocol):
    def extract(
        self,
        *,
        tenant_id: str,
        receipt: ProcessPackageReceipt,
        manifest: BusinessProcessManifest,
        original: bytes,
    ) -> SemanticCandidateSet: ...
```

`SemanticCandidateSet` uses the exact addendum field order. Candidate IDs derive from tenant, set identity, candidate kind, and repository sequence; timestamps do not contribute.

- [ ] **Step 4: Implement the bounded narrative format**

The extractor maps manifest names and rules into typed candidates. It enriches them only from explicit Markdown markers:

```markdown
## Definitions
- Entity `Refund`: A repayment with its own lifecycle.

## Relationships
- `Refund` --refunds--> `Payment`

## Metrics
- `Net Revenue`: recognized revenue less approved refunds.
```

Unknown prose remains evidence but is not interpreted. Missing definitions, ambiguous references, and unrecognized markers become unresolved questions. This justifies extraction beyond copying the manifest while keeping behavior deterministic and reviewable.

- [ ] **Step 5: Add malformed-marker, duplicate, digest, and cross-tenant tests**

Run: `uv run pytest services/semantic-registry/tests/test_models.py services/semantic-registry/tests/test_extractor.py -q`

Expected: PASS with malformed markers rejected, exact duplicates collapsed only within one candidate set, conflicting definitions preserved as separate review items, and cross-tenant reads denied.

- [ ] **Step 6: Pin candidate-set fields to the addendum**

Modify `tests/conformance/test_specification_conformance.py` to compare the documented `SemanticCandidateSet` field order with `SemanticCandidateSet.model_fields`.

- [ ] **Step 7: Run package checks and commit**

Run: `uv run pytest tests/conformance/test_specification_conformance.py -q && uv run ruff check services/semantic-registry tests/conformance && uv run mypy`

```bash
git add services/semantic-registry tests/conformance/test_specification_conformance.py pyproject.toml uv.lock
git commit -m "feat(semantic-registry): extract immutable semantic candidates"
```

---

### Task 5: Resolve authority per information kind

**Files:**
- Modify: `packages/contract-model/src/pillarmesh_contract_model/models.py`
- Modify: `packages/contract-model/src/pillarmesh_contract_model/__init__.py`
- Create: `services/semantic-registry/src/pillarmesh_semantic_registry/authority.py`
- Create: `services/semantic-registry/tests/test_authority.py`
- Modify: `services/semantic-registry/src/pillarmesh_semantic_registry/models.py`
- Modify: `services/semantic-registry/src/pillarmesh_semantic_registry/repository.py`
- Modify: `services/semantic-registry/src/pillarmesh_semantic_registry/__init__.py`
- Modify: `tests/conformance/test_specification_conformance.py`

**Interfaces:**
- Consumes: Task 4 candidate sets, catalog snapshots converted to immutable observations, previous approved semantic versions, and owner decisions.
- Produces: `AuthoritySourceKind`, `AuthorityObservation`, `AuthorityResolution`, `AuthorityResolutionStatus`, `ResolutionReasonCode`, and `AuthorityResolver.resolve()`. `InformationKind` and `SemanticRuleKind` are produced in `packages/contract-model`.

- [ ] **Step 1: Encode the required Refund conflict as a failing test**

```python
def test_cross_kind_refund_conflict_escalates_to_business_owner() -> None:
    candidate = entity_candidate("Refund", "A business entity with its own lifecycle")
    # The package observation is essential. Without it there is no admitted
    # business-meaning authority at all, and the assertion below would pass because
    # nothing was resolvable rather than because two kinds disagreed.
    package = observation(
        information_kind=InformationKind.BUSINESS_MEANING,
        source_kind=AuthoritySourceKind.PROCESS_PACKAGE,
        subject_ref="Refund",
        assertion="entity with its own lifecycle",
    )
    catalog = observation(
        information_kind=InformationKind.IMPORTED_CLASSIFICATION,
        source_kind=AuthoritySourceKind.DECLARED_CATALOG_AUTHORITY,
        subject_ref="Refund",
        assertion="attribute of Invoice",
    )

    resolution = resolver.resolve(candidate=candidate, observations=(package, catalog))

    assert resolution.status is AuthorityResolutionStatus.UNRESOLVED
    assert resolution.required_authority_ref == "role:business_owner"
    assert resolution.reason_code == "cross_kind_conflict"
    assert set(resolution.considered_observation_digests) == {
        package.observed_digest,
        catalog.observed_digest,
    }


def test_absent_authority_is_not_reported_as_a_cross_kind_conflict() -> None:
    # Distinguishes the two ways resolution can fail, so the test above cannot pass
    # for the wrong reason.
    candidate = entity_candidate("Refund", "A business entity with its own lifecycle")

    resolution = resolver.resolve(candidate=candidate, observations=())

    assert resolution.status is AuthorityResolutionStatus.UNRESOLVED
    assert resolution.reason_code == "no_admitted_authority"
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest services/semantic-registry/tests/test_authority.py -q`

Expected: FAIL because authority resolution is absent.

- [ ] **Step 3: Define exact authority categories and precedence maps**

`InformationKind` and `SemanticRuleKind` belong to `packages/contract-model`, not to this
service. `AuthorityBinding` and `SemanticRule` are durable contract-model artifacts, and a
package may never import from a service, so defining the vocabulary here would force those
fields to bare `str` and let an unvalidated value into a digest-addressed contract. Define
both in `pillarmesh_contract_model` and import them here.

```python
# packages/contract-model: shared vocabulary, imported by the semantic registry.
class InformationKind(StrEnum):
    BUSINESS_MEANING = "business_meaning"
    PROCESS_SEMANTICS = "process_semantics"
    IMPORTED_GLOSSARY = "imported_glossary"
    IMPORTED_CLASSIFICATION = "imported_classification"
    IDENTITY = "identity"
    RELATIONSHIP = "relationship"
    METRIC = "metric"
    INTEGRITY_CONSTRAINT = "integrity_constraint"


# packages/contract-model: the rule vocabulary SemanticRule.kind is typed with.
class SemanticRuleKind(StrEnum):
    IDENTITY = "identity"
    RELATIONSHIP = "relationship"
    TRANSITION = "transition"
    INTEGRITY_CONSTRAINT = "integrity_constraint"
    METRIC = "metric"


# services/semantic-registry: who may assert, per information kind.
class AuthoritySourceKind(StrEnum):
    OWNER_DECISION = "owner_decision"
    APPROVED_SEMANTIC_VERSION = "approved_semantic_version"
    PROCESS_PACKAGE = "process_package"
    DECLARED_CATALOG_AUTHORITY = "declared_catalog_authority"


_PRECEDENCE: dict[InformationKind, tuple[AuthoritySourceKind, ...]] = {
    InformationKind.BUSINESS_MEANING: (
        AuthoritySourceKind.OWNER_DECISION,
        AuthoritySourceKind.APPROVED_SEMANTIC_VERSION,
        AuthoritySourceKind.PROCESS_PACKAGE,
    ),
    InformationKind.IMPORTED_CLASSIFICATION: (
        AuthoritySourceKind.DECLARED_CATALOG_AUTHORITY,
        AuthoritySourceKind.OWNER_DECISION,
    ),
    InformationKind.PROCESS_SEMANTICS: (
        AuthoritySourceKind.OWNER_DECISION,
        AuthoritySourceKind.APPROVED_SEMANTIC_VERSION,
        AuthoritySourceKind.PROCESS_PACKAGE,
    ),
    InformationKind.IMPORTED_GLOSSARY: (
        AuthoritySourceKind.DECLARED_CATALOG_AUTHORITY,
        AuthoritySourceKind.OWNER_DECISION,
    ),
    InformationKind.IDENTITY: (
        AuthoritySourceKind.OWNER_DECISION,
        AuthoritySourceKind.APPROVED_SEMANTIC_VERSION,
        AuthoritySourceKind.PROCESS_PACKAGE,
    ),
    InformationKind.RELATIONSHIP: (
        AuthoritySourceKind.OWNER_DECISION,
        AuthoritySourceKind.APPROVED_SEMANTIC_VERSION,
        AuthoritySourceKind.PROCESS_PACKAGE,
    ),
    InformationKind.METRIC: (
        AuthoritySourceKind.OWNER_DECISION,
        AuthoritySourceKind.APPROVED_SEMANTIC_VERSION,
        AuthoritySourceKind.PROCESS_PACKAGE,
    ),
    InformationKind.INTEGRITY_CONSTRAINT: (
        AuthoritySourceKind.OWNER_DECISION,
        AuthoritySourceKind.APPROVED_SEMANTIC_VERSION,
        AuthoritySourceKind.PROCESS_PACKAGE,
    ),
}


class AuthorityObservation(ArtifactModel):
    schema_version: Literal["1"] = "1"
    observation_id: str
    tenant_id: str
    information_kind: InformationKind
    source_kind: AuthoritySourceKind
    subject_ref: str
    assertion: str
    authority_ref: str
    observed_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    valid_until: datetime


class AuthorityResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    INVALID = "invalid"


class ResolutionReasonCode(StrEnum):
    RESOLVED_BY_PRECEDENCE = "resolved_by_precedence"
    CROSS_KIND_CONFLICT = "cross_kind_conflict"
    SAME_RANK_DISAGREEMENT = "same_rank_disagreement"
    NO_ADMITTED_AUTHORITY = "no_admitted_authority"
    EXPIRED_OBSERVATION = "expired_observation"
    INADMISSIBLE_SOURCE = "inadmissible_source"


class AuthorityResolution(ArtifactModel):
    resolution_id: str
    tenant_id: str
    candidate_id: str
    status: AuthorityResolutionStatus
    selected_observation_digest: str | None
    considered_observation_digests: tuple[str, ...]
    required_authority_ref: str | None
    reason_code: ResolutionReasonCode


class AuthorityResolver:
    def resolve(
        self,
        *,
        candidate: SemanticCandidate,
        observations: tuple[AuthorityObservation, ...],
    ) -> AuthorityResolution: ...
```

Define every enum member explicitly; never use a fallback ordering. An observation of a source kind not admitted for that information kind is invalid input. Candidate extraction is proposal evidence and never appears as an authority source.

- [ ] **Step 4: Implement deterministic resolution and validity windows**

Every failure path returns a distinct `ResolutionReasonCode`: an absent admitted authority is `no_admitted_authority`, never `cross_kind_conflict`. Reject expired observations. Same-kind same-rank disagreement is unresolved. Cross-kind disagreement is unresolved. An exact higher-precedence same-kind observation resolves with all lower evidence retained in the result. Resolution artifacts bind every observation digest considered.

- [ ] **Step 5: Prove all branches and mutation resistance**

Run: `uv run pytest services/semantic-registry/tests/test_authority.py -q`

Run focused mutation testing: `uv run mutmut run --paths-to-mutate services/semantic-registry/src/pillarmesh_semantic_registry/authority.py`

Expected: tests PASS; inspect every survivor and add a test for any precedence, expiry, equality, or cross-kind branch that survives.

- [ ] **Step 6: Pin authority-observation fields to the addendum**

Modify `tests/conformance/test_specification_conformance.py` to compare `AuthorityObservation.model_fields` with the normative field order.

- [ ] **Step 7: Commit authority resolution**

```bash
git add services/semantic-registry/src/pillarmesh_semantic_registry services/semantic-registry/tests/test_authority.py tests/conformance/test_specification_conformance.py
git commit -m "feat(semantic-registry): resolve authority by information kind"
```

---

### Task 6: Route ontology review through typed inbox contracts

**Files:**
- Modify: `services/request-management/src/pillarmesh_request_management/models.py`
- Modify: `services/request-management/src/pillarmesh_request_management/service.py`
- Modify: `services/request-management/src/pillarmesh_request_management/repository.py`
- Modify: `services/request-management/src/pillarmesh_request_management/__init__.py`
- Create: `services/request-management/tests/test_semantic_review.py`
- Create: `services/semantic-registry/src/pillarmesh_semantic_registry/review.py`
- Create: `services/semantic-registry/tests/test_review.py`
- Modify: `tests/conformance/test_specification_conformance.py`

**Interfaces:**
- Consumes: Task 5 authority resolutions and Plan 1 request revisions, conversations, transition events, and digest-bound decisions.
- Produces: `SchemaSemanticChangeRequest`, `ReviewItemDecision`, `OntologyReviewBundle`, `OntologyReviewItem`, `SemanticRevision`, and `SemanticReviewService`. `DecisionKind` is unchanged.

- [ ] **Step 1: Write failing discriminator and lifecycle tests**

```python
def test_semantic_review_payload_remains_a_strict_discriminated_member() -> None:
    request = InboxRequest.model_validate(semantic_request_payload())
    assert isinstance(request.payload, SchemaSemanticChangeRequest)
    with pytest.raises(ValidationError):
        InboxRequest.model_validate(
            semantic_request_payload() | {"payload": {"request_type": "anything"}}
        )


def test_unresolved_review_returns_to_investigating_before_no_valid_plan(flow) -> None:
    request = flow.awaiting_approval()
    revised = flow.record_review_decision(request, ReviewItemDecision.UNRESOLVED)
    assert revised.state is RequestState.INVESTIGATING
    terminal = flow.mark_no_valid_plan(revised, expected_revision=revised.revision)
    assert terminal.state is RequestState.NO_VALID_PLAN
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest services/request-management/tests/test_semantic_review.py services/semantic-registry/tests/test_review.py -q`

Expected: FAIL because the payload, decisions, and review service do not exist.

- [ ] **Step 3: Widen contracts without changing the transition table**

```python
class SchemaSemanticChangeRequest(ArtifactModel):
    request_type: Literal["schema_semantic_change"] = "schema_semantic_change"
    purpose: str = Field(min_length=1, max_length=512)
    review_bundle_id: str
    review_bundle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_authority_refs: tuple[str, ...]


# DecisionKind is Plan 1's shipped request-level vocabulary and is NOT widened.
# Item-level review outcomes get their own enum: "approve"/"accept" and
# "request_changes"/"revise" would otherwise be near-synonyms inside one enum whose
# values persist in digest-addressed artifacts, where a wrong choice is immutable.
class ReviewItemDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    REVISE = "revise"
    MERGE = "merge"
    UNRESOLVED = "unresolved"


class OntologyReviewItem(ArtifactModel):
    item_id: str
    candidate_ids: tuple[str, ...]
    authority_resolution_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_authority_ref: str
    semantic_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["pending", "accepted", "rejected", "revised", "merged", "unresolved"]
    # Mirrors ReviewItemDecision; pinned against it in Step 7.


class OntologyReviewBundle(ArtifactModel):
    schema_version: Literal["1"] = "1"
    bundle_id: str
    tenant_id: str
    revision: int = Field(ge=1)
    candidate_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_observation_digests: tuple[str, ...]
    items: tuple[OntologyReviewItem, ...]
    required_authority_refs: tuple[str, ...]
    status: Literal["open", "awaiting_approval", "approved", "rejected", "no_valid_plan"]
    created_at: datetime
    updated_at: datetime


class SemanticRevision(ArtifactModel):
    revision_id: str
    tenant_id: str
    bundle_id: str
    item_id: str
    revision: int = Field(ge=1)
    content: str
    parent_candidate_ids: tuple[str, ...]
    actor_id: str
    created_at: datetime
```

`DecisionKind` keeps exactly the three values Plan 1 shipped: an item outcome is a
`ReviewItemDecision`, and a request-level decision remains `approve`, `reject`, or
`request_changes` through the existing `record_decision`. Add `SchemaSemanticChangeRequest`
as a third explicit union member; do not replace the union with a generic dictionary.
`_TRANSITIONS` remains unchanged and conformance-pinned.

- [ ] **Step 4: Implement immutable review bundles and revisions**

Each review item binds one or more candidate IDs, authority-resolution digest, required authority, current semantic revision digest, and status. `revise` and `merge` create a new `SemanticRevision`; they never mutate candidates. A bundle approval is valid only when every required item has a terminal accepted/rejected decision and all required authority refs have exact subject-digest decisions.

`SemanticReviewService` exposes:

```python
def create_bundle(
    self,
    *,
    tenant_id: str,
    candidate_set_id: str,
    candidate_set_revision: int,
    resolutions: tuple[AuthorityResolution, ...],
) -> OntologyReviewBundle: ...
def submit_bundle(
    self, *, tenant_id: str, bundle_id: str, expected_revision: int, requester_id: str
) -> InboxRequest: ...
def decide_item(
    self,
    *,
    tenant_id: str,
    bundle_id: str,
    item_id: str,
    decision: ReviewItemDecision,
    actor_id: str,
    expected_revision: int,
    revised_content: str | None = None,
    merge_candidate_ids: tuple[str, ...] = (),
) -> OntologyReviewBundle: ...
```

- [ ] **Step 5: Implement the exact request path**

Use:

```text
submitted → investigating → proposed → awaiting_approval
awaiting_approval → executing → verifying → delivered
awaiting_approval → investigating → no_valid_plan
awaiting_approval → rejected
```

Conversations may clarify any non-terminal revision. An approval binds `review_bundle_digest`; a material revision changes the digest and makes the earlier approval unusable.

- [ ] **Step 6: Add stale approval, role, cross-tenant, and atomic rollback tests**

Run: `uv run pytest services/request-management/tests/test_semantic_review.py services/semantic-registry/tests/test_review.py tests/conformance/test_specification_conformance.py -q`

Expected: PASS, including denial when a policy-authority decision is supplied by a business-owner-only actor and rollback when decision persistence fails after revision allocation.

- [ ] **Step 7: Pin review-bundle fields and decision vocabulary**

Extend conformance tests to compare `OntologyReviewBundle.model_fields` with the addendum, the documented item outcomes with `ReviewItemDecision`, and `OntologyReviewItem.status` with `ReviewItemDecision` plus `pending`. Assert `DecisionKind` still has exactly Plan 1's three values.

- [ ] **Step 8: Commit ontology review flow**

```bash
git add services/request-management services/semantic-registry tests/conformance/test_specification_conformance.py
git commit -m "feat(request-management): add attributable semantic review"
```

---

### Task 7: Compile approved semantics and form Integration Contract v2

**Files:**
- Modify: `packages/contract-model/src/pillarmesh_contract_model/models.py`
- Modify: `packages/contract-model/src/pillarmesh_contract_model/__init__.py`
- Create: `packages/contract-model/tests/test_managed_contract.py`
- Create: `services/semantic-registry/src/pillarmesh_semantic_registry/approval.py`
- Create: `services/semantic-registry/tests/test_approval.py`
- Create: `services/contract/src/pillarmesh_contract_service/formation.py`
- Create: `services/contract/tests/test_formation.py`
- Modify: `services/contract/src/pillarmesh_contract_service/__init__.py`
- Modify: `tests/conformance/test_specification_conformance.py`

**Interfaces:**
- Consumes: approved review bundle, exact authority observations, process-package receipt, opaque source observation refs, destination product requirement, policy requirements, and decision bindings.
- Produces: `ApprovedSemanticVersion`, `ManagedIntegrationContract`, `ContractFormationStatus`, `ContractFormationResult`, and `IntegrationContractFormationService.form()`.

- [ ] **Step 1: Write failing formation tests**

```python
def test_contract_binds_exact_semantic_and_approval_versions(service) -> None:
    result = service.form(valid_formation_input())
    assert result.status is ContractFormationStatus.READY_TO_ACTIVATE
    assert result.contract.semantic_version_ref.digest == digest(approved_semantics())
    assert result.contract.approval_ids == exact_approval_ids()


def test_unknown_required_identity_returns_no_valid_plan(service) -> None:
    result = service.form(valid_formation_input(identity_rule=Unknown(reason="owner unresolved")))
    assert result.status is ContractFormationStatus.NO_VALID_PLAN
    assert result.no_valid_plan.execution_occurred is False
    assert result.no_valid_plan.constraints == ("identity_rule:customer",)
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest packages/contract-model/tests/test_managed_contract.py services/semantic-registry/tests/test_approval.py services/contract/tests/test_formation.py -q`

Expected: FAIL because approved semantic and managed contract models do not exist.

- [ ] **Step 3: Define permanent managed contract models without changing M0**

```python
class ContractFormationStatus(StrEnum):
    READY_TO_ACTIVATE = "ready_to_activate"
    NEEDS_APPROVAL = "needs_approval"
    NO_VALID_PLAN = "no_valid_plan"


class ArtifactReference(ArtifactModel):
    artifact_id: str
    version: int = Field(ge=1)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class FieldMapping(ArtifactModel):
    source_ref: str
    semantic_ref: str
    transformation: Literal["identity", "normalized", "derived"]


class ContractConstraint(ArtifactModel):
    constraint_id: str
    kind: Literal[
        "identity",
        "referential",
        "cardinality",
        "temporal",
        "lifecycle",
        "reconciliation",
        "freshness",
        "access",
    ]
    expression: str


class DestinationProductRequirement(ArtifactModel):
    product_name: str
    warehouse_binding_id: str
    supported_engines: tuple[Literal["postgresql", "clickhouse"], ...]


class FreshnessRequirement(ArtifactModel):
    maximum_age_seconds: int = Field(gt=0)


class QualityPolicy(ArtifactModel):
    required_constraint_ids: tuple[str, ...]
    quarantine_unresolved: Literal[True] = True


class TriggerRequirement(ArtifactModel):
    cadence: Literal["daily"] = "daily"
    run_now_allowed: bool


class AccessPolicy(ArtifactModel):
    classification_refs: tuple[str, ...]
    required_approver_refs: tuple[str, ...]


class EvidencePolicy(ArtifactModel):
    retain_decision_history: Literal[True] = True
    retain_publication_receipts: Literal[True] = True


class FailurePolicy(ArtifactModel):
    unresolved_meaning: Literal["no_valid_plan"] = "no_valid_plan"
    integrity_violation: Literal["quarantine"] = "quarantine"


class Unknown(ArtifactModel):
    reason: str


class ContractNoValidPlan(ArtifactModel):
    result: Literal["no_valid_plan"] = "no_valid_plan"
    constraints: tuple[str, ...]
    smallest_changes: tuple[str, ...]
    execution_occurred: Literal[False] = False


# A formed contract is a pure function of its approved inputs and therefore carries no
# timestamp, per the ratified rule that deterministic artifacts exclude receipt times.
# ApprovedSemanticVersion does carry created_at: it is a versioned record of when an
# approval was granted, not a derivation, so two identical approvals granted at
# different times are deliberately distinct artifacts.
class ManagedIntegrationContract(ArtifactModel):
    schema_version: Literal["2"] = "2"
    contract_id: str
    tenant_id: str
    version: int = Field(ge=1)
    formation_status: Literal[ContractFormationStatus.READY_TO_ACTIVATE]
    semantic_version_ref: ArtifactReference
    source_observation_refs: tuple[ArtifactReference, ...]
    mappings: tuple[FieldMapping, ...]
    integrity_constraints: tuple[ContractConstraint, ...]
    destination_product: DestinationProductRequirement
    freshness: FreshnessRequirement
    quality: QualityPolicy
    trigger_policy: TriggerRequirement
    access_policy: AccessPolicy
    evidence_policy: EvidencePolicy
    failure_policy: FailurePolicy
    approval_ids: tuple[str, ...]


class SemanticObject(ArtifactModel):
    object_id: str
    name: str
    definition: str
    source_refs: tuple[str, ...]


class SemanticRule(ArtifactModel):
    rule_id: str
    kind: SemanticRuleKind
    expression: str
    source_refs: tuple[str, ...]


class AuthorityBinding(ArtifactModel):
    information_kind: InformationKind
    subject_ref: str
    authority_ref: str
    observation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ApprovedSemanticVersion(ArtifactModel):
    schema_version: Literal["1"] = "1"
    semantic_version_id: str
    tenant_id: str
    version: int = Field(ge=1)
    process_package_ref: ArtifactReference
    candidate_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_bundle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    entities: tuple[SemanticObject, ...]
    events: tuple[SemanticObject, ...]
    states: tuple[SemanticObject, ...]
    relationships: tuple[SemanticObject, ...]
    identity_rules: tuple[SemanticRule, ...]
    constraints: tuple[SemanticRule, ...]
    metrics: tuple[SemanticObject, ...]
    classifications: tuple[SemanticObject, ...]
    authority_bindings: tuple[AuthorityBinding, ...]
    approval_ids: tuple[str, ...]
    created_at: datetime


class ContractFormationResult(ArtifactModel):
    status: ContractFormationStatus
    contract: ManagedIntegrationContract | None
    no_valid_plan: ContractNoValidPlan | None
    missing_approval_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def result_matches_status(self) -> Self:
        if self.status is ContractFormationStatus.READY_TO_ACTIVATE:
            if self.contract is None or self.no_valid_plan is not None:
                raise ValueError("ready result requires only a contract")
        elif self.status is ContractFormationStatus.NO_VALID_PLAN:
            if self.no_valid_plan is None or self.contract is not None:
                raise ValueError("no-valid-plan result requires only constraints")
        elif self.contract is not None or self.no_valid_plan is not None:
            raise ValueError("needs-approval result cannot carry a terminal artifact")
        return self


class ContractFormationInput(ArtifactModel):
    tenant_id: str
    semantic_version_ref: ArtifactReference
    source_observation_refs: tuple[ArtifactReference, ...]
    destination_product: DestinationProductRequirement
    freshness: FreshnessRequirement
    quality: QualityPolicy
    trigger_policy: TriggerRequirement
    access_policy: AccessPolicy
    evidence_policy: EvidencePolicy
    failure_policy: FailurePolicy
    approval_ids: tuple[str, ...]


class IntegrationContractFormationService:
    def form(self, formation_input: ContractFormationInput) -> ContractFormationResult: ...
```

Keep the historical M0 `IntegrationContract` class and serialization unchanged. Export the new type explicitly.

- [ ] **Step 4: Compile an approved semantic version atomically**

`ApprovedSemanticVersion` uses the addendum field order and stores normalized semantic objects, not review UI state. Compilation requires current authority observations and exact approvals for every accepted or revised item. Allocate semantic version and persist its approval bindings in one transaction.

- [ ] **Step 5: Implement deterministic formation verdicts**

`form()` returns:

- `NEEDS_APPROVAL` when all required meaning is known but an exact required approval is missing;
- `NO_VALID_PLAN` with attributable constraints when any required value is `Unknown`, observations are stale, or authorities conflict;
- `READY_TO_ACTIVATE` only when every section 10 prerequisite is represented and approved.

The service creates no signed graph, provider call, schedule, warehouse object, or activation record.

- [ ] **Step 6: Pin approved-semantic and managed-contract fields to the addendum**

Add conformance tests comparing the normative `ApprovedSemanticVersion` and `ManagedIntegrationContract` field lists to their `model_fields`. Add property tests proving canonical serialization is stable under dictionary construction order and unknown fields fail closed.

- [ ] **Step 7: Run focused and package gates**

Run: `uv run pytest packages/contract-model/tests/test_managed_contract.py services/semantic-registry/tests/test_approval.py services/contract/tests/test_formation.py tests/conformance/test_specification_conformance.py -q && uv run ruff check packages/contract-model services/semantic-registry services/contract && uv run mypy`

Expected: all PASS.

- [ ] **Step 8: Commit semantic and contract formation**

```bash
git add packages/contract-model services/semantic-registry services/contract tests/conformance/test_specification_conformance.py
git commit -m "feat(contract): form governed managed integration contracts"
```

---

### Task 8: Publish approved semantics and create drift requests

**Files:**
- Create: `services/semantic-registry/src/pillarmesh_semantic_registry/publication.py`
- Create: `services/semantic-registry/tests/test_publication.py`
- Modify: `providers/openmetadata/src/pillarmesh_provider_openmetadata/client.py`
- Create: `providers/openmetadata/src/pillarmesh_provider_openmetadata/publication.py`
- Create: `providers/openmetadata/tests/test_publication.py`
- Modify: `services/request-management/src/pillarmesh_request_management/service.py`
- Create: `tests/integration/test_openmetadata_publication_live.py`

**Interfaces:**
- Consumes: ready `CatalogBinding`, `ApprovedSemanticVersion`, `ManagedIntegrationContract`, OpenMetadata client, and request-management semantic request intake.
- Produces: `CatalogPublicationIntent`, `CatalogPublicationReceipt`, normalized `AuthorityObservation`, `CatalogDriftProposal`, and `SemanticPublicationService.publish()` / `.observe_drift()`.

- [ ] **Step 1: Write failing idempotency and post-publication drift tests**

```python
def test_lost_publication_response_replay_creates_no_duplicate_identity(service, provider) -> None:
    provider.lose_first_response_after_commit()
    with pytest.raises(CatalogProviderError):
        service.publish(publication_input())
    receipt = service.publish(publication_input())
    assert provider.count_objects(identity=receipt.semantic_identity) == 1


def test_catalog_edit_after_publication_creates_typed_change_request(service) -> None:
    receipt = service.publish(publication_input())
    provider.edit_classification(receipt, "LegacyRefundAttribute")
    proposal = service.observe_drift(receipt.tenant_id, receipt.publication_id)
    assert proposal.request.payload.request_type == "schema_semantic_change"
    assert proposal.auto_applied is False
```

- [ ] **Step 2: Confirm RED**

Run: `uv run pytest services/semantic-registry/tests/test_publication.py providers/openmetadata/tests/test_publication.py -q`

Expected: FAIL because publication does not exist.

- [ ] **Step 3: Define stable publication identities and receipts**

```python
semantic_identity = digest(
    {
        "domain": "pillarmesh-openmetadata-semantic-object-v1",
        "tenant_id": tenant_id,
        "semantic_version_id": semantic_version.semantic_version_id,
        "object_kind": object_kind,
        "object_ref": object_ref,
    }
)
```

Use these intent and drift shapes:

```python
class CatalogPublicationIntent(ArtifactModel):
    schema_version: Literal["1"] = "1"
    operation_id: str
    tenant_id: str
    catalog_binding_id: str
    catalog_binding_revision: int = Field(ge=1)
    semantic_version_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_identities: tuple[str, ...]


class CatalogDriftProposal(ArtifactModel):
    proposal_id: str
    tenant_id: str
    publication_id: str
    information_kind: InformationKind
    before_observation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_observation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    affected_semantic_version_id: str
    affected_contract_id: str
    request_id: str
    auto_applied: Literal[False] = False
```

`CatalogPublicationIntent` binds catalog binding revision, semantic version digest, contract digest, ordered semantic identities, and operation ID. `CatalogPublicationReceipt` binds the intent digest, provider version, returned stable object references, round-trip observation digest, and publication time. Returned provider IDs are opaque and may appear only in provider-local/private mapping state; public receipts use stable PillarMesh object references.

Use this receipt shape:

```python
class CatalogPublicationReceipt(ArtifactModel):
    schema_version: Literal["1"] = "1"
    publication_id: str
    tenant_id: str
    intent_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_version: str
    published_refs: tuple[ArtifactReference, ...]
    round_trip_observation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    round_trip_verified: Literal[True] = True
    published_at: datetime


class SemanticPublicationService:
    def publish(
        self,
        *,
        binding: CatalogBinding,
        semantic_version: ApprovedSemanticVersion,
        contract: ManagedIntegrationContract,
    ) -> CatalogPublicationReceipt: ...

    def observe_drift(self, tenant_id: str, publication_id: str) -> CatalogDriftProposal | None: ...
```

- [ ] **Step 4: Implement idempotent ensure-and-observe publication**

Publish approved entities, glossary terms, classifications, ownership, contract references, and lineage. After every ensure operation, read the object back and compare normalized meaning, ownership, classification, and provenance. Only a complete matching round trip commits the receipt.

- [ ] **Step 5: Implement drift classification**

Cosmetic description drift may synchronize under explicit policy. Meaning, identity, classification, access, relationship, metric, constraint, ownership, or deprecation drift creates a `schema_semantic_change` request in `investigating`, bound to before/after observation digests and affected approved semantic/contract versions. It never rewrites those versions or auto-applies the catalog edit.

- [ ] **Step 6: Add cross-tenant and ambiguous-failure tests**

Run: `uv run pytest services/semantic-registry/tests/test_publication.py providers/openmetadata/tests/test_publication.py -q`

Expected: PASS, including denial when a tenant attempts to publish through another tenant's catalog binding and convergence after timeout, 409 conflict, or repeated operation ID.

- [ ] **Step 7: Run real OpenMetadata publication round trip**

Run: `tests/emulators/openmetadata/run.sh pytest tests/integration/test_openmetadata_publication_live.py -q`

Expected: new tenant namespace, glossary, classification, ownership, contract reference, and lineage objects are created; independent API reads match; one post-publication edit creates an attributable request; teardown uses exact ledger IDs.

- [ ] **Step 8: Commit publication and drift handling**

```bash
git add services/semantic-registry providers/openmetadata services/request-management tests/integration/test_openmetadata_publication_live.py
git commit -m "feat(semantic-registry): publish semantics and detect catalog drift"
```

---

### Task 9: Prove the complete Plan 2 acceptance journey

**Files:**
- Create: `tests/end-to-end/test_catalog_semantic_formation.py`
- Create: `tests/acceptance/plan2_orchestration.py`
- Create: `tests/acceptance/run_plan2.py`
- Create: `docs/plan2/setup.md`
- Create: `docs/plan2/acceptance-run.md`
- Create: `docs/plan2/teardown.md`
- Create: `docs/plan2/evidence-package.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: Tasks 2-8 public interfaces only.
- Produces: one offline deterministic journey, one opt-in real OpenMetadata witnessed journey, sanitized evidence package, and exact setup/teardown runbooks.

- [ ] **Step 1: Write the failing offline end-to-end test**

```python
def test_process_package_forms_approved_contract_and_catalog_publication(tmp_path: Path) -> None:
    result = run_plan2_offline(
        tenant_id="tenant-a",
        process_package=revenue_to_cash_package(),
        catalog=fake_that_validates_real_models(),
        database_path=tmp_path / "plan2.db",
    )
    assert result.catalog_binding.lifecycle_state is CatalogBindingState.READY
    assert result.semantic_version.process_package_ref.version == 1
    assert result.contract.formation_status == "ready_to_activate"
    assert result.publication_receipt.round_trip_verified is True
    assert result.request.state is RequestState.DELIVERED
```

- [ ] **Step 2: Add the required negative journey**

```python
def test_refund_cross_kind_conflict_ends_in_no_valid_plan(tmp_path: Path) -> None:
    result = run_plan2_offline(
        tenant_id="tenant-a",
        process_package=refund_entity_package(),
        catalog=legacy_refund_attribute_catalog(),
        owner_decisions=(),
        database_path=tmp_path / "plan2.db",
    )
    assert result.authority_resolution.reason_code == "cross_kind_conflict"
    assert result.request.state is RequestState.NO_VALID_PLAN
    assert result.execution_occurred is False
    assert result.publication_receipt is None
```

- [ ] **Step 3: Confirm RED**

Run: `uv run pytest tests/end-to-end/test_catalog_semantic_formation.py -q`

Expected: FAIL because Plan 2 orchestration does not exist.

- [ ] **Step 4: Implement orchestration using only public service contracts**

The orchestrator must not reach into SQLite tables, provider internals, private resource mappings, or request `_TRANSITIONS`. It records correlation IDs and public digests, keeps sensitive operational evidence in the private resource ledger, and exports only allowlisted evidence fields.

- [ ] **Step 5: Add tenant isolation and replay acceptance cases**

Prove two tenants can upload the same bytes without shared deletion authority; tenant B cannot read tenant A candidates, observations, review bundle, semantic version, contract, catalog binding, publication, or request; replaying extraction, review submission, contract formation, and publication does not create duplicate semantic effects.

- [ ] **Step 6: Write real acceptance and teardown runbooks**

`setup.md` pins prerequisites and OpenMetadata 1.13.3 images. `acceptance-run.md` requires a new transaction traced through terminal catalog readiness, exact package version, candidate set, owner resolution, delivered review request, ready contract, independent catalog reads, post-publication drift request, backup, isolated restore, and representative query. `teardown.md` resolves only resource-ledger IDs, records cleanup results, and refuses broad or unrecorded targets.

- [ ] **Step 7: Run the offline Plan 2 acceptance tests**

Run: `uv run pytest tests/end-to-end/test_catalog_semantic_formation.py -q`

Expected: PASS for successful formation, `No Valid Plan`, tenant isolation, stale approval, replay, and cleanup-denial cases.

- [ ] **Step 8: Run every required offline gate**

```bash
uv sync --locked --all-packages
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -m "not live"
./tests/repository-structure/test.sh
```

Expected: all commands PASS. A skipped or failed gate remains a reported gap.

- [ ] **Step 9: Run the opt-in witnessed local acceptance journey**

Run: `tests/emulators/openmetadata/run.sh python -m tests.acceptance.run_plan2`

Expected: the command produces a new sanitized evidence package for one successful tenant and one unresolved-conflict tenant, verifies intended and denied access, restores representative catalog metadata in an isolated target, then completes exact ledger-driven teardown. Existing catalog rows or an HTTP 200 alone are not acceptance evidence.

- [ ] **Step 10: Review secrets and external effects**

Run:

```bash
rg -n "token|password|secret|authorization|endpoint|provider_ref" tests/acceptance docs/plan2
git diff --check
git status --short
```

Inspect every match. Expected: no credential value, private endpoint, provider ID, raw process narrative, or unredacted catalog payload appears in committed fixtures or exported evidence.

- [ ] **Step 11: Commit Plan 2 acceptance**

```bash
git add tests/end-to-end/test_catalog_semantic_formation.py tests/acceptance/plan2_orchestration.py tests/acceptance/run_plan2.py docs/plan2 README.md
git commit -m "test: prove catalog and semantic formation journey"
```

---

## Final review checklist

- Verify every durable artifact field documented in the addendum is checked against the implementation in `tests/conformance/`.
- Verify the request transition table remains unchanged and the new request payload remains one strict discriminated-union member.
- Verify authority resolution has no global ranking and the Refund cross-kind conflict cannot auto-resolve.
- Verify candidate extraction does not interpret unrestricted prose and no AI dependency entered the offline path.
- Verify OpenMetadata-specific types and IDs do not cross the provider boundary.
- Verify private operational state, cleanup identifiers, credentials, endpoints, and backup locations are absent from public artifacts and evidence exports.
- Verify semantic approval and contract formation bind exact candidate, authority-observation, review-bundle, policy, and approval digests.
- Verify `No Valid Plan` is reached only from `investigating` and no provider, publication, activation, or warehouse effect occurs on that path.
- Verify post-publication material drift creates a typed request and cannot mutate approved semantics automatically.
- Verify real lifecycle evidence proves creation, positive and denial probes, publication round trip, backup, isolated restore, and exact cleanup—not only health checks.
- Verify live/emulator tests remain opt-in and every offline gate passes without network access.
