from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from pillarmesh_access_control import (
    CurrentEntitlementResolver,
    CurrentEntitlementSnapshot,
    EntitlementResolutionDenied,
)
from pillarmesh_compiler import GovernedQueryPlan, ProductGenerationReference
from pillarmesh_contract_model import ArtifactModel, ArtifactReference, digest
from pillarmesh_request_management import (
    AnswerDeliveryAuthorization,
    AnswerIntentValidation,
    AnswerProductGenerationReference,
    AnswerScopePolicy,
    FilterDomain,
    FreshnessDisposition,
    GovernedAnswerDeliveryAuthorizationUnavailable,
    GovernedAnswerExecutionAuthorizationDenied,
    GovernedAnswerExecutionAuthorizationUnavailable,
    GovernedAnswerNotVisible,
    InboxRequest,
    PolicyAdmissionReceipt,
    RequestState,
    StakeholderQuestion,
)
from pillarmesh_runtime import AnswerExecutionAuthorization
from pydantic import ConfigDict, Field, field_validator


class AnswerAuthorityDenied(RuntimeError):
    pass


class AnswerAuthorityUnavailable(AnswerAuthorityDenied):
    pass


class PrincipalAuthorityReader(Protocol):
    def resolve_principal(self, *, tenant_id: str, actor_id: str) -> str | None: ...


class RequestAuthorityRepository(Protocol):
    def load_owned_request(self, tenant_id: str, request_id: str) -> InboxRequest: ...


class AdmissionAuthorityRepository(Protocol):
    def list_admissions(
        self, tenant_id: str, request_id: str
    ) -> tuple[PolicyAdmissionReceipt, ...]: ...


class PlanAuthorityRepository(Protocol):
    def read(self, tenant_id: str, plan_digest: str) -> GovernedQueryPlan | None: ...


class PolicyAuthorityRepository(Protocol):
    def list_revisions(self, tenant_id: str, policy_id: str) -> tuple[AnswerScopePolicy, ...]: ...


class ValidationAuthorityReader(Protocol):
    def read_validation(
        self, *, tenant_id: str, request_id: str, validation_digest: str
    ) -> AnswerIntentValidation | None: ...


class ProductAnswerAuthority(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    tenant_id: str = Field(min_length=1)
    product_generation_refs: tuple[ProductGenerationReference, ...] = Field(min_length=1)
    freshness_observation_ref: str = Field(min_length=1)
    quality_observation_ref: str = Field(min_length=1)
    freshness_disposition: FreshnessDisposition
    quality_blocked: bool
    material_quality_limitations: tuple[ArtifactReference, ...]
    lineage_refs: tuple[ArtifactReference, ...] = Field(min_length=1)
    as_of: datetime
    approved_narrative_terms: tuple[str, ...]

    @field_validator("as_of")
    @classmethod
    def as_of_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("as_of must be timezone-aware UTC")
        return value.astimezone(UTC)


class ProductAnswerAuthorityReader(Protocol):
    def read_current(
        self,
        *,
        tenant_id: str,
        product_generation_refs: tuple[ProductGenerationReference, ...],
    ) -> ProductAnswerAuthority | None: ...


class _ResolvedAnswerAuthority(ArtifactModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    request: InboxRequest
    admission: PolicyAdmissionReceipt
    plan: GovernedQueryPlan
    validation: AnswerIntentValidation
    policy: AnswerScopePolicy
    entitlement: CurrentEntitlementSnapshot
    product: ProductAnswerAuthority


class CurrentAnswerAuthority:
    def __init__(
        self,
        *,
        requests: RequestAuthorityRepository,
        admissions: AdmissionAuthorityRepository,
        plans: PlanAuthorityRepository,
        policies: PolicyAuthorityRepository,
        validations: ValidationAuthorityReader,
        entitlements: CurrentEntitlementResolver,
        principals: PrincipalAuthorityReader,
        products: ProductAnswerAuthorityReader,
        clock: Callable[[], datetime],
    ) -> None:
        self._requests = requests
        self._admissions = admissions
        self._plans = plans
        self._policies = policies
        self._validations = validations
        self._entitlements = entitlements
        self._principals = principals
        self._products = products
        self._clock = clock

    def runtime_rechecker(self) -> RuntimeAnswerAuthorizationRechecker:
        return RuntimeAnswerAuthorizationRechecker(self)

    def delivery_rechecker(self) -> DeliveryAnswerAuthorizationRechecker:
        return DeliveryAnswerAuthorizationRechecker(self)

    def _resolve(
        self,
        *,
        tenant_id: str,
        request_id: str,
        plan_digest: str,
        required_permission: str,
    ) -> _ResolvedAnswerAuthority:
        now = self._now()
        try:
            request = self._requests.load_owned_request(tenant_id, request_id)
            admissions = self._admissions.list_admissions(tenant_id, request_id)
        except KeyError:
            raise AnswerAuthorityDenied("answer authority is not visible") from None
        matching = tuple(item for item in admissions if item.plan_digest == plan_digest)
        if len(matching) != 1:
            raise AnswerAuthorityDenied("answer authority is not visible")
        admission = matching[0]
        plan = self._plans.read(tenant_id, plan_digest)
        if plan is None:
            raise AnswerAuthorityDenied("answer authority is not visible")
        validation = self._validations.read_validation(
            tenant_id=tenant_id,
            request_id=request_id,
            validation_digest=admission.validation_digest,
        )
        if validation is None:
            raise AnswerAuthorityDenied("answer authority is not visible")
        revisions = self._policies.list_revisions(tenant_id, admission.policy_id)
        if not revisions:
            raise AnswerAuthorityDenied("answer authority is not visible")
        policy = max(revisions, key=lambda item: item.revision)

        if not isinstance(request.payload, StakeholderQuestion):
            raise AnswerAuthorityDenied("answer authority is not visible")
        purpose_digest = digest(request.payload.purpose)
        principal_ref = self._principals.resolve_principal(
            tenant_id=tenant_id, actor_id=request.requester_id
        )
        if principal_ref is None:
            raise AnswerAuthorityDenied("answer authority is not visible")
        try:
            entitlement = self._entitlements.resolve_current(
                tenant_id=tenant_id,
                principal_ref=principal_ref,
                purpose_digest=purpose_digest,
            )
        except EntitlementResolutionDenied as error:
            if error.reason_code == "authority_unavailable":
                raise AnswerAuthorityUnavailable("answer authority is unavailable") from None
            raise AnswerAuthorityDenied("answer authority is not visible") from None
        product = self._products.read_current(
            tenant_id=tenant_id,
            product_generation_refs=plan.product_generation_refs,
        )
        if product is None:
            raise AnswerAuthorityDenied("answer authority is not visible")

        self._verify_identity(
            request=request,
            admission=admission,
            plan=plan,
            validation=validation,
            policy=policy,
            entitlement=entitlement,
            product=product,
            tenant_id=tenant_id,
            request_id=request_id,
            plan_digest=plan_digest,
            purpose_digest=purpose_digest,
            principal_ref=principal_ref,
            required_permission=required_permission,
            now=now,
        )
        self._verify_scope(plan, validation, policy, entitlement)
        self._verify_product(policy, product, now)
        return _ResolvedAnswerAuthority(
            request=request,
            admission=admission,
            plan=plan,
            validation=validation,
            policy=policy,
            entitlement=entitlement,
            product=product,
        )

    @staticmethod
    def _verify_identity(
        *,
        request: InboxRequest,
        admission: PolicyAdmissionReceipt,
        plan: GovernedQueryPlan,
        validation: AnswerIntentValidation,
        policy: AnswerScopePolicy,
        entitlement: CurrentEntitlementSnapshot,
        product: ProductAnswerAuthority,
        tenant_id: str,
        request_id: str,
        plan_digest: str,
        purpose_digest: str,
        principal_ref: str,
        required_permission: str,
        now: datetime,
    ) -> None:
        valid_request_state = (
            request.state is RequestState.EXECUTING
            and request.revision == admission.request_revision + 1
        ) or (
            request.state in (RequestState.VERIFYING, RequestState.DELIVERED)
            and request.revision in (admission.request_revision + 2, admission.request_revision + 3)
        )
        if not (
            request.tenant_id
            == admission.tenant_id
            == plan.tenant_id
            == validation.tenant_id
            == policy.tenant_id
            == entitlement.tenant_id
            == product.tenant_id
            == tenant_id
            and request.request_id == admission.request_id == validation.request_id == request_id
            and admission.plan_digest == plan.plan_digest == plan_digest
            and product.product_generation_refs == plan.product_generation_refs
            and plan.validation_digest == admission.validation_digest == digest(validation)
            and validation.outcome == "admitted"
            and validation.request_revision == admission.request_revision
            and validation.policy_id == admission.policy_id == policy.policy_id
            and validation.policy_revision == admission.policy_revision == policy.revision
            and validation.policy_digest == admission.policy_digest == policy.canonical_digest()
            and validation.semantic_version_digest == policy.semantic_version_ref.digest
            and validation.entitlement_snapshot_digest
            == admission.entitlement_snapshot_digest
            == entitlement.snapshot_digest
            and entitlement.principal_ref == principal_ref
            and entitlement.purpose_digest == purpose_digest
            and principal_ref in policy.principal_scope
            and request.payload.purpose in policy.purposes
            and required_permission in entitlement.permissions
            and policy.valid_from <= now < policy.valid_until
            and valid_request_state
        ):
            raise AnswerAuthorityDenied("answer authority is not visible")

    @staticmethod
    def _verify_scope(
        plan: GovernedQueryPlan,
        validation: AnswerIntentValidation,
        policy: AnswerScopePolicy,
        entitlement: CurrentEntitlementSnapshot,
    ) -> None:
        plan_generations = tuple(
            AnswerProductGenerationReference.model_validate(
                item.model_dump(mode="python"), strict=True
            )
            for item in plan.product_generation_refs
        )
        validation_generations = validation.product_generation_refs
        plan_products = tuple(item.product_ref for item in plan_generations)
        if (
            len(plan_generations) != len(set(plan_generations))
            or len(validation_generations) != len(set(validation_generations))
            or set(plan_generations) != set(validation_generations)
            or any(item not in policy.data_product_version_refs for item in plan_products)
            or any(item not in entitlement.product_version_refs for item in plan_products)
        ):
            raise AnswerAuthorityDenied("answer product scope is no longer authorized")

        filter_dimensions = tuple(item.dimension_ref for item in validation.bound_filters)
        required_semantics = {
            policy.semantic_version_ref,
            *validation.bound_metric_versions,
            *validation.bound_dimensions,
            *filter_dimensions,
        }
        if (
            plan.minimum_group_size != policy.minimum_group_size
            or any(
                item not in policy.metric_version_refs for item in validation.bound_metric_versions
            )
            or any(item not in policy.dimension_refs for item in validation.bound_dimensions)
            or any(item not in policy.dimension_refs for item in filter_dimensions)
            or not required_semantics.issubset(entitlement.semantic_refs)
        ):
            raise AnswerAuthorityDenied("answer semantic scope is no longer authorized")

        policy_domains = {item.dimension_ref: item for item in policy.filter_domains}
        entitlement_domains = {item.dimension_ref: item for item in entitlement.filter_domains}
        if any(dimension not in filter_dimensions for dimension in entitlement_domains):
            raise AnswerAuthorityDenied("answer filter scope is no longer authorized")
        for binding in validation.bound_filters:
            policy_domain = _policy_domain(policy_domains, binding.dimension_ref)
            entitlement_domain = entitlement_domains.get(binding.dimension_ref)
            if (
                policy_domain is None
                or entitlement_domain is None
                or not set(binding.values).issubset(policy_domain.values)
                or not set(binding.values).issubset(entitlement_domain.values)
            ):
                raise AnswerAuthorityDenied("answer filter scope is no longer authorized")

    @staticmethod
    def _verify_product(
        policy: AnswerScopePolicy, product: ProductAnswerAuthority, now: datetime
    ) -> None:
        if product.as_of > now:
            raise AnswerAuthorityDenied("answer product evidence is not current")
        staleness = int((now - product.as_of).total_seconds())
        if staleness > policy.max_staleness or product.freshness_disposition != "current":
            raise AnswerAuthorityDenied("answer product evidence is not current")
        if product.quality_blocked and policy.quality_disposition == "block":
            raise AnswerAuthorityDenied("answer product quality is blocked")
        if product.quality_blocked and not product.material_quality_limitations:
            raise AnswerAuthorityDenied("answer product quality evidence is incomplete")

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("clock must return a timezone-aware UTC timestamp")
        return now.astimezone(UTC)


class RuntimeAnswerAuthorizationRechecker:
    def __init__(self, authority: CurrentAnswerAuthority) -> None:
        self._authority = authority

    def recheck(
        self,
        *,
        tenant_id: str,
        request_id: str,
        validation_digest: str,
        plan_digest: str,
    ) -> AnswerExecutionAuthorization:
        try:
            resolved = self._authority._resolve(
                tenant_id=tenant_id,
                request_id=request_id,
                plan_digest=plan_digest,
                required_permission="query",
            )
        except AnswerAuthorityUnavailable as error:
            raise GovernedAnswerExecutionAuthorizationUnavailable from error
        except AnswerAuthorityDenied as error:
            raise GovernedAnswerExecutionAuthorizationDenied from error
        if resolved.admission.validation_digest != validation_digest:
            raise AnswerAuthorityDenied("answer authority is not visible")
        return AnswerExecutionAuthorization(
            tenant_id=tenant_id,
            request_id=request_id,
            validation_digest=validation_digest,
            plan_digest=plan_digest,
            policy_revision=resolved.policy.revision,
            entitlement_digest=resolved.entitlement.snapshot_digest,
            row_ceiling=resolved.policy.row_ceiling,
            byte_ceiling=resolved.policy.byte_ceiling,
            statement_timeout_seconds=resolved.policy.statement_timeout,
            result_retention_seconds=resolved.policy.result_retention,
            freshness_observation_ref=resolved.product.freshness_observation_ref,
            quality_observation_ref=resolved.product.quality_observation_ref,
        )


class DeliveryAnswerAuthorizationRechecker:
    def __init__(self, authority: CurrentAnswerAuthority) -> None:
        self._authority = authority

    def recheck(
        self,
        *,
        tenant_id: str,
        request_id: str,
        requester_id: str,
        plan_digest: str,
        required_permission: Literal["view", "download"],
    ) -> AnswerDeliveryAuthorization:
        try:
            resolved = self._authority._resolve(
                tenant_id=tenant_id,
                request_id=request_id,
                plan_digest=plan_digest,
                required_permission=required_permission,
            )
        except AnswerAuthorityUnavailable as error:
            raise GovernedAnswerDeliveryAuthorizationUnavailable from error
        except AnswerAuthorityDenied as error:
            raise GovernedAnswerNotVisible("answer delivery authority is not visible") from error
        if resolved.request.requester_id != requester_id:
            raise GovernedAnswerNotVisible("answer delivery authority is not visible")
        return AnswerDeliveryAuthorization(
            tenant_id=tenant_id,
            request_id=request_id,
            requester_id=requester_id,
            request_revision=resolved.admission.request_revision + 2,
            plan_digest=plan_digest,
            policy_id=resolved.policy.policy_id,
            policy_revision=resolved.policy.revision,
            policy_snapshot_digest=resolved.policy.canonical_digest(),
            entitlement_snapshot_digest=resolved.entitlement.snapshot_digest,
            policy_current=True,
            entitlement_current=True,
            valid_until=min(resolved.policy.valid_until, resolved.entitlement.valid_until),
            row_ceiling=resolved.policy.row_ceiling,
            byte_ceiling=resolved.policy.byte_ceiling,
            minimum_group_size=resolved.policy.minimum_group_size,
            freshness_observation_ref=resolved.product.freshness_observation_ref,
            quality_observation_ref=resolved.product.quality_observation_ref,
            freshness_disposition=resolved.product.freshness_disposition,
            metric_version_refs=resolved.validation.bound_metric_versions,
            material_quality_limitations=resolved.product.material_quality_limitations,
            lineage_refs=resolved.product.lineage_refs,
            as_of=resolved.product.as_of,
            restatement=resolved.validation.restatement,
            approved_narrative_terms=resolved.product.approved_narrative_terms,
        )


def _policy_domain(
    domains: dict[str, FilterDomain], reference: ArtifactReference
) -> FilterDomain | None:
    return domains.get(reference.artifact_id)
