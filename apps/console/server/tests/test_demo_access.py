"""The demonstration's applied access grants, driven through the journey that produces one.

These tests do not ask whether `build_demo_access_control` returns an object. They put a data
access request through the real request-management journey -- intake, clarification, a compiled
proposal, the owner's approval, admission -- and then hold access-control to what it did with the
admitted proposal: a durable grant, provider effects recorded per surface, a verified delivery,
the authorization the console asks for before it shows the requester a dashboard, and a revocation
that undoes all of it.

The entitlement is the demonstration's own signed policy authority over loopback TLS, resolved
through the real `CurrentEntitlementResolver`, so no test here asserts an entitlement nothing
authenticated. Where a test needs the authority to assert `dashboard`, it says so and says why --
see `test_dashboard_access_is_refused_while_the_demonstration_asserts_no_dashboard_permission`,
which pins what the demonstration's own entitlement withholds today.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from heinzel_access_control import (
    AccessEffectAction,
    AccessGrant,
    AccessGrantDenied,
    AccessMode,
    CurrentEntitlementResolver,
    EntitlementPermission,
    SignedEntitlementBody,
    SQLiteAccessGrantRepository,
    SQLiteEntitlementRepository,
)
from heinzel_console.answers import (
    LOCAL_CONNECTED_AUTHORITY_REF,
    local_signed_policy_authority,
)
from heinzel_console.demo.access import (
    DemoAccessControl,
    DemoAccessEffectProvider,
    build_demo_access_control,
)
from heinzel_console.demo.answers import (
    DEMO_ENTITLEMENT_PERMISSIONS,
    demo_answer_bindings,
    demo_entitlement_body,
    demo_product_reference,
)
from heinzel_console.demo.collaborators import (
    DEMO_ARCHITECT_ID,
    DEMO_ARCHITECT_PRINCIPAL_REF,
    DEMO_REQUESTER_ID,
    DEMO_REQUESTER_PRINCIPAL_REF,
    DemoAnswerCandidateProvider,
    DemoRoleResolver,
    build_demo_policy_compiler,
    build_demo_snapshot_resolver,
    demo_clock,
)
from heinzel_console.demo.publication import (
    DEMO_PRODUCT_NAME,
    DEMO_PURPOSE,
    DEMO_TENANT_ID,
    build_demo_publication,
)
from heinzel_console.demo.stores import DemoStores
from heinzel_contract_model import ApprovedSemanticVersion, ArtifactReference, digest
from heinzel_provider_sdk import AccessEffectCommand, AccessEffectProviderError
from heinzel_request_management import (
    FulfillmentAccessDeliveryReceipt,
    FulfillmentAdmissionReceipt,
    FulfillmentAuthorityError,
    FulfillmentGroundingError,
    FulfillmentProposal,
    FulfillmentService,
    RequestManagementService,
    SQLiteFulfillmentRepository,
)

# How long the demonstration's entitlement stays valid, matching `demo_governed_answer`.
_VALIDITY = timedelta(days=365)

# The two approved terms the demonstration's product carries, and one field it does not. The
# published terms are the metric and the dimension `demo_answer_bindings` binds; `customer_email`
# names nothing the publication approved, so a least-privilege scope must exclude it.
_PUBLISHED_FIELDS = ("daily-order-value", "order_day")
_UNPUBLISHED_FIELD = "customer_email"

# What the demonstration's entitlement authority asserts. `DEMO_ENTITLEMENT_PERMISSIONS` carries
# `dashboard`, which dashboard mode requires, so these tests assert over the demonstration's own
# permissions rather than a set assembled here -- a set assembled here would keep passing if the
# demonstration stopped asserting one.
_WITH_DASHBOARD: tuple[EntitlementPermission, ...] = DEMO_ENTITLEMENT_PERMISSIONS


@dataclass(frozen=True)
class _Demonstration:
    """The demonstration's own services, over one published product and one live authority."""

    requests: RequestManagementService
    fulfillment: FulfillmentService
    access: DemoAccessControl
    product_ref: ArtifactReference
    grants: SQLiteAccessGrantRepository


def _entitlement_body(
    *,
    publication_semantic_version: ApprovedSemanticVersion,
    product_ref: ArtifactReference,
    permissions: tuple[EntitlementPermission, ...],
) -> SignedEntitlementBody:
    """The demonstration's entitlement body, with its asserted permissions named by the test.

    Built from `demo_entitlement_body` and then re-signed over the permissions under test, so
    every other claim -- tenant, principal, purpose, product and the exact approved terms -- stays
    the demonstration's own rather than something this test invented.
    """
    body = demo_entitlement_body(
        principal_ref=DEMO_REQUESTER_PRINCIPAL_REF,
        purpose=DEMO_PURPOSE,
        semantic_version=publication_semantic_version,
        product_ref=product_ref,
        now=demo_clock(),
        valid_for=_VALIDITY,
    )
    claims = body.model_dump(mode="python")
    claims.pop("source_payload_digest")
    claims["permissions"] = tuple(sorted(permissions))
    return SignedEntitlementBody.model_validate(
        claims
        | {"source_payload_digest": SignedEntitlementBody.compute_source_payload_digest(claims)}
    )


@contextmanager
def _demonstration(
    root: Path, *, permissions: tuple[EntitlementPermission, ...] = _WITH_DASHBOARD
) -> Iterator[_Demonstration]:
    """Stand the demonstration's access journey up over real stores and a real authority."""
    with ExitStack() as opened:
        stores = opened.enter_context(closing(DemoStores(root / "state")))
        publication = build_demo_publication(stores, clock=demo_clock)
        product_ref = demo_product_reference(
            publication.contract.version, digest(publication.contract.destination_product)
        )
        bindings = demo_answer_bindings(publication.semantic_version, product_ref=product_ref)
        authority = opened.enter_context(
            local_signed_policy_authority(
                root / "policy",
                body=_entitlement_body(
                    publication_semantic_version=publication.semantic_version,
                    product_ref=product_ref,
                    permissions=permissions,
                ),
            )
        )
        entitlements = CurrentEntitlementResolver(
            repository=opened.enter_context(
                closing(SQLiteEntitlementRepository.open(root / "entitlements.sqlite3"))
            ),
            connected_authority=authority.reader,
            connected_authority_ref=LOCAL_CONNECTED_AUTHORITY_REF,
            clock=demo_clock,
        )
        grants = SQLiteAccessGrantRepository(
            opened.enter_context(closing(sqlite3.connect(root / "access-grants.sqlite3")))
        )
        requests = RequestManagementService(stores.requests, clock=demo_clock)
        fulfillment_repository = SQLiteFulfillmentRepository(stores.requests)
        access = build_demo_access_control(
            grants=grants,
            result_access_connection=opened.enter_context(
                closing(sqlite3.connect(root / "result-access.sqlite3"))
            ),
            requests=requests,
            fulfillment_repository=fulfillment_repository,
            entitlements=entitlements,
            product_ref=product_ref,
            bindings=bindings,
            clock=demo_clock,
        )
        yield _Demonstration(
            requests=requests,
            fulfillment=FulfillmentService(
                request_service=requests,
                repository=fulfillment_repository,
                snapshot_resolver=build_demo_snapshot_resolver(
                    publications=stores.publications,
                    publication=publication,
                    clock=demo_clock,
                ),
                answer_candidate_provider=DemoAnswerCandidateProvider(publication=publication),
                access_candidate_provider=access.scope_previews,
                data_product_owner_resolver=access.product_owners,
                access_grant_admission_resolver=access.grant_admission,
                access_grant_activation_reader=access.activation,
                policy_compiler=build_demo_policy_compiler(),
                authority_role_resolver=DemoRoleResolver(),
                clock=demo_clock,
            ),
            access=access,
            product_ref=product_ref,
            grants=grants,
        )


def _submit(
    demonstration: _Demonstration,
    *,
    access_mode: AccessMode = "dashboard",
    requested_fields: tuple[str, ...] = (*_PUBLISHED_FIELDS, _UNPUBLISHED_FIELD),
    purpose: str = DEMO_PURPOSE,
) -> str:
    """Submit the requester's access request and clarify it, leaving it ready to propose."""
    submitted = demonstration.requests.submit_access_request(
        tenant_id=DEMO_TENANT_ID,
        requester_id=DEMO_REQUESTER_ID,
        title="Daily order value",
        purpose=purpose,
        data_product_id=DEMO_PRODUCT_NAME,
        requested_fields=requested_fields,
        access_mode=access_mode,
        expires_at=demo_clock() + timedelta(days=7),
    )
    demonstration.fulfillment.clarify_outcome(
        tenant_id=DEMO_TENANT_ID,
        request_id=submitted.request_id,
        actor_id=DEMO_ARCHITECT_ID,
        restated_request="Give the requester the approved daily order value fields.",
        in_scope_summary="Daily order value, by order day.",
        out_of_scope_summary="Anything the publication does not approve.",
        expected_revision=submitted.revision,
    )
    return submitted.request_id


def _propose_and_approve(demonstration: _Demonstration, request_id: str) -> FulfillmentProposal:
    """Compile the access proposal and record every approval it requires."""
    investigating = demonstration.requests.get(DEMO_TENANT_ID, request_id)
    proposal = demonstration.fulfillment.propose_access(
        tenant_id=DEMO_TENANT_ID,
        request_id=request_id,
        actor_id=DEMO_ARCHITECT_ID,
        expected_revision=investigating.revision,
    )
    assert isinstance(proposal, FulfillmentProposal)
    awaiting = demonstration.fulfillment.submit_proposal(
        tenant_id=DEMO_TENANT_ID,
        request_id=request_id,
        actor_id=DEMO_ARCHITECT_ID,
        expected_revision=proposal.request_revision,
    )
    # Two approvals, and two actors: the architect stands as the product owner the disclosure
    # needs, and the requester acknowledges the narrowed scope they are being given. Approving
    # both as one actor is exactly the thing `DemoRoleResolver` refuses.
    approvers = {
        DEMO_ARCHITECT_PRINCIPAL_REF: DEMO_ARCHITECT_ID,
        DEMO_REQUESTER_PRINCIPAL_REF: DEMO_REQUESTER_ID,
    }
    for requirement in proposal.required_approvals:
        demonstration.fulfillment.record_approval(
            tenant_id=DEMO_TENANT_ID,
            request_id=request_id,
            actor_id=approvers[requirement.authority_ref],
            authority_ref=requirement.authority_ref,
            subject_digest=requirement.subject_digest,
            decision="approve",
            expected_revision=awaiting.revision,
        )
    return proposal


def _admit(demonstration: _Demonstration, request_id: str) -> FulfillmentAdmissionReceipt:
    awaiting = demonstration.requests.get(DEMO_TENANT_ID, request_id)
    admission = demonstration.fulfillment.admit(
        tenant_id=DEMO_TENANT_ID,
        request_id=request_id,
        actor_id=DEMO_ARCHITECT_ID,
        expected_revision=awaiting.revision,
    )
    assert isinstance(admission, FulfillmentAdmissionReceipt)
    return admission


def _apply_and_deliver(
    demonstration: _Demonstration, request_id: str
) -> tuple[AccessGrant, FulfillmentAccessDeliveryReceipt]:
    """Apply the admitted grant and let request-management verify the delivery over it."""
    admission = _admit(demonstration, request_id)
    assert admission.access_grant_binding is not None
    grant = demonstration.access.grant_commands.apply(
        tenant_id=DEMO_TENANT_ID,
        request_id=request_id,
        grant_id=admission.access_grant_binding.grant_id,
    )
    delivery = demonstration.fulfillment.execute_access(
        tenant_id=DEMO_TENANT_ID,
        request_id=request_id,
        actor_id="heinzel-access-control",
        expected_revision=admission.resulting_request_revision,
    )
    return grant, delivery


def _succeeded_surfaces(
    demonstration: _Demonstration,
    grant: AccessGrant,
    *,
    action: AccessEffectAction,
    revision: int,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            receipt.surface
            for receipt in demonstration.grants.effect_receipts(
                grant.tenant_id,
                grant.grant_id,
                revision,
                action=action,
            )
            if receipt.outcome == "succeeded"
        )
    )


def test_an_admitted_dashboard_request_produces_an_active_grant_the_dashboard_read_authorizes(
    tmp_path: Path,
) -> None:
    with _demonstration(tmp_path) as demonstration:
        request_id = _submit(demonstration)
        _propose_and_approve(demonstration, request_id)
        grant, delivery = _apply_and_deliver(demonstration, request_id)

        assert grant.state == "active"
        assert grant.access_mode == "dashboard"
        assert grant.permissions == ("dashboard", "view")
        assert grant.principal_ref == DEMO_REQUESTER_PRINCIPAL_REF
        assert grant.data_product_version_ref == demonstration.product_ref
        # Least privilege: the field the publication never approved is not in the grant.
        assert grant.fields == _PUBLISHED_FIELDS
        assert _UNPUBLISHED_FIELD not in grant.fields
        assert delivery.fields == _PUBLISHED_FIELDS

        # Every surface the dashboard mode requires recorded a successful apply effect, the
        # Superset one included: that receipt is the dashboard access effect.
        assert _succeeded_surfaces(
            demonstration, grant, action="apply", revision=grant.revision - 1
        ) == ("result", "superset", "warehouse")

        # The exact call the console makes before it shows a requester a published dashboard.
        authorized = demonstration.access.grant_commands.authorize(
            tenant_id=DEMO_TENANT_ID,
            grant_id=grant.grant_id,
            principal_ref=DEMO_REQUESTER_PRINCIPAL_REF,
            purpose=DEMO_PURPOSE,
            permission="dashboard",
            product_version_ref=demonstration.product_ref,
        )
        assert authorized.grant_id == grant.grant_id
        assert authorized.state == "active"

        # And the requester may read the answer's result back, which is the one surface the
        # demonstration enacts for real.
        assert demonstration.access.result_access.allows(
            tenant_id=DEMO_TENANT_ID,
            principal_ref=DEMO_REQUESTER_PRINCIPAL_REF,
            result_ref=f"result:{DEMO_PRODUCT_NAME}",
            permission="view",
        )


def test_a_revocation_revokes_every_surface_and_stops_authorizing_the_dashboard(
    tmp_path: Path,
) -> None:
    with _demonstration(tmp_path) as demonstration:
        request_id = _submit(demonstration)
        _propose_and_approve(demonstration, request_id)
        active, _delivery = _apply_and_deliver(demonstration, request_id)

        revoked = demonstration.access.revocation_commands.revoke_for_request(
            tenant_id=DEMO_TENANT_ID,
            request_id=request_id,
            actor_id=DEMO_REQUESTER_ID,
            expected_revision=active.revision,
            reason="The requester no longer needs the dashboard.",
        )

        assert revoked.state == "revoked"
        assert revoked.manual_revocation is not None
        assert revoked.manual_revocation.actor_id == DEMO_REQUESTER_ID
        assert _succeeded_surfaces(
            demonstration, revoked, action="revoke", revision=revoked.revision - 1
        ) == ("result", "superset", "warehouse")
        assert demonstration.grants.load_current_for_request(DEMO_TENANT_ID, request_id) == revoked
        with pytest.raises(AccessGrantDenied):
            demonstration.access.grant_commands.authorize(
                tenant_id=DEMO_TENANT_ID,
                grant_id=revoked.grant_id,
                principal_ref=DEMO_REQUESTER_PRINCIPAL_REF,
                purpose=DEMO_PURPOSE,
                permission="dashboard",
                product_version_ref=demonstration.product_ref,
            )
        assert not demonstration.access.result_access.allows(
            tenant_id=DEMO_TENANT_ID,
            principal_ref=DEMO_REQUESTER_PRINCIPAL_REF,
            result_ref=f"result:{DEMO_PRODUCT_NAME}",
            permission="view",
        )


def test_a_grant_is_never_revoked_by_an_actor_who_neither_asked_for_it_nor_governs_it(
    tmp_path: Path,
) -> None:
    with _demonstration(tmp_path) as demonstration:
        request_id = _submit(demonstration)
        _propose_and_approve(demonstration, request_id)
        active, _delivery = _apply_and_deliver(demonstration, request_id)

        with pytest.raises(AccessGrantDenied):
            demonstration.access.revocation_commands.revoke_for_request(
                tenant_id=DEMO_TENANT_ID,
                request_id=request_id,
                actor_id="someone-else",
                expected_revision=active.revision,
                reason="Not mine to revoke.",
            )

        assert demonstration.grants.load_current_for_request(DEMO_TENANT_ID, request_id) == active


def test_dashboard_access_is_refused_by_an_authority_that_asserts_no_dashboard_permission(
    tmp_path: Path,
) -> None:
    """An entitlement withholding `dashboard` refuses the grant, whatever was approved.

    The demonstration now asserts `dashboard`, so this constructs an authority that does not. The
    property is the one that matters whoever the tenant is: a grant's permissions must be a subset
    of what the authority asserts, and an approval cannot supply one it withheld. Nothing here
    depends on the demonstration's own permission set, so this keeps holding if that set changes
    again.
    """
    without_dashboard = tuple(
        permission for permission in DEMO_ENTITLEMENT_PERMISSIONS if permission != "dashboard"
    )
    assert "dashboard" not in without_dashboard
    with _demonstration(tmp_path, permissions=without_dashboard) as demonstration:
        request_id = _submit(demonstration)
        _propose_and_approve(demonstration, request_id)

        with pytest.raises(FulfillmentAuthorityError, match="does not assert dashboard"):
            _admit(demonstration, request_id)

        assert demonstration.grants.load_current_for_request(DEMO_TENANT_ID, request_id) is None


def test_dashboard_access_is_granted_under_the_permissions_the_demonstration_asserts(
    tmp_path: Path,
) -> None:
    """The journey the demonstration exists to show, in dashboard mode, end to end.

    The counterpart of the refusal above: with `dashboard` asserted, the same approved proposal
    reaches an active grant carrying exactly `dashboard` and `view`.
    """
    with _demonstration(tmp_path, permissions=DEMO_ENTITLEMENT_PERMISSIONS) as demonstration:
        request_id = _submit(demonstration)
        _propose_and_approve(demonstration, request_id)
        grant, _ = _apply_and_deliver(demonstration, request_id)

        assert grant.state == "active"
        assert grant.access_mode == "dashboard"
        assert grant.permissions == ("dashboard", "view")


def test_query_access_is_granted_under_the_permissions_the_demonstration_does_assert(
    tmp_path: Path,
) -> None:
    """The same journey in query mode, against the demonstration's published permissions.

    This is what the demonstration can grant today without any further change, and it is the
    control for the test above: the refusal there is about the missing `dashboard` permission and
    not about the composition of this module.
    """
    with _demonstration(tmp_path, permissions=DEMO_ENTITLEMENT_PERMISSIONS) as demonstration:
        request_id = _submit(demonstration, access_mode="query")
        _propose_and_approve(demonstration, request_id)
        grant, delivery = _apply_and_deliver(demonstration, request_id)

        assert grant.state == "active"
        assert grant.permissions == ("query", "view")
        assert delivery.fields == _PUBLISHED_FIELDS
        # No Superset effect: a query grant names no dashboard to read.
        assert _succeeded_surfaces(
            demonstration, grant, action="apply", revision=grant.revision - 1
        ) == ("result", "warehouse")


def test_access_is_never_proposed_for_a_field_no_approved_term_names(tmp_path: Path) -> None:
    with _demonstration(tmp_path) as demonstration:
        request_id = _submit(demonstration, requested_fields=(_UNPUBLISHED_FIELD,))

        with pytest.raises(FulfillmentGroundingError):
            _propose_and_approve(demonstration, request_id)


def test_access_is_never_granted_for_a_purpose_the_authority_asserts_nothing_for(
    tmp_path: Path,
) -> None:
    """A purpose the local authority holds no entitlement for fails closed at admission.

    The demonstration's authority asserts one purpose. ADR-0007 requires a missing authority to
    deny rather than fall back on anything local, and the admission is where that denial lands.
    """
    with _demonstration(tmp_path) as demonstration:
        request_id = _submit(demonstration, purpose="an unasserted purpose")
        _propose_and_approve(demonstration, request_id)

        with pytest.raises(FulfillmentAuthorityError, match="authority_missing"):
            _admit(demonstration, request_id)

        assert demonstration.grants.load_current_for_request(DEMO_TENANT_ID, request_id) is None


def test_a_demonstration_effect_provider_never_enacts_a_command_its_grant_did_not_ask_for(
    tmp_path: Path,
) -> None:
    """The warehouse stand-in refuses a command whose resource the grant never named.

    A provider that accepted whatever it was handed would let a composition mistake read as an
    applied effect. The grant here is active rather than pending as well, so this also covers a
    command replayed after the revision that issued it moved on.
    """
    with _demonstration(tmp_path) as demonstration:
        request_id = _submit(demonstration)
        _propose_and_approve(demonstration, request_id)
        grant, _delivery = _apply_and_deliver(demonstration, request_id)
        provider = DemoAccessEffectProvider(surface="warehouse", grants=demonstration.grants)

        with pytest.raises(AccessEffectProviderError) as refusal:
            provider.enact(
                AccessEffectCommand(
                    tenant_id=grant.tenant_id,
                    grant_id=grant.grant_id,
                    grant_revision=grant.revision,
                    surface="warehouse",
                    action="apply",
                    idempotency_key="pm-access-tampered",
                    principal_ref=grant.principal_ref,
                    provider_resource_ref="relation:somebody_elses_product",
                    fields=grant.fields,
                    permissions=grant.permissions,
                    effective_at=grant.effective_at,
                    expires_at=grant.expires_at,
                    scope_digest=digest({"tampered": True}),
                )
            )

        assert refusal.value.outcome == "permanent_failure"


def test_the_result_surface_is_never_stood_in_for_by_the_demonstration_provider() -> None:
    """The result surface has a real durable provider, so the stand-in refuses to take it.

    Composed over `result`, this provider would report an applied effect while the rows the
    requester's own result read checks were never written.
    """
    with (
        closing(sqlite3.connect(":memory:")) as connection,
        pytest.raises(ValueError, match="result surface"),
    ):
        DemoAccessEffectProvider(surface="result", grants=SQLiteAccessGrantRepository(connection))
