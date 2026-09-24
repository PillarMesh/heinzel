from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from heinzel_console.demo.collaborators import (
    DEMO_ARCHITECT_ID,
    DEMO_ARCHITECT_PRINCIPAL_REF,
    DEMO_REQUESTER_ID,
    DEMO_REQUESTER_PRINCIPAL_REF,
    DemoAnswerCandidateProvider,
    DemoAuthorityResolver,
    DemoRoleResolver,
    build_demo_snapshot_resolver,
    demo_clock,
)
from heinzel_console.demo.publication import (
    DEMO_QUESTION,
    DEMO_TENANT_ID,
    DemoPublication,
    build_demo_publication,
)
from heinzel_console.demo.stores import DemoStores
from heinzel_request_management import (
    FulfillmentGroundingSnapshot,
    FulfillmentPolicySnapshot,
    InboxRequest,
    RequestState,
    ResolutionFailure,
    StakeholderAnswerDraft,
)
from heinzel_request_management.models import DataAccessRequest, StakeholderQuestion

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)


def _clock(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


def _question(*, question: str = DEMO_QUESTION) -> InboxRequest:
    return InboxRequest(
        request_id="request-demo-1",
        tenant_id=DEMO_TENANT_ID,
        requester_id=DEMO_REQUESTER_ID,
        payload=StakeholderQuestion(purpose="weekly review", question=question),
        state=RequestState.INVESTIGATING,
        revision=2,
        submitted_at=NOW,
        updated_at=NOW,
    )


def _access_request(*, data_product_id: str) -> InboxRequest:
    return InboxRequest(
        request_id="request-demo-2",
        tenant_id=DEMO_TENANT_ID,
        requester_id=DEMO_REQUESTER_ID,
        payload=DataAccessRequest(
            purpose="weekly review",
            data_product_id=data_product_id,
            requested_fields=("daily-order-count",),
            access_mode="dashboard",
            expires_at=NOW + timedelta(days=1),
        ),
        state=RequestState.INVESTIGATING,
        revision=2,
        submitted_at=NOW,
        updated_at=NOW,
    )


def _resolve(
    stores: DemoStores, published: DemoPublication, request: InboxRequest
) -> tuple[FulfillmentGroundingSnapshot, FulfillmentPolicySnapshot] | ResolutionFailure:
    resolver = build_demo_snapshot_resolver(
        publications=stores.publications, publication=published, clock=_clock(NOW)
    )
    return resolver.resolve(tenant_id=DEMO_TENANT_ID, request=request)


def test_the_role_resolver_grants_only_the_demonstration_pairs() -> None:
    resolver = DemoRoleResolver()
    assert resolver.has_role(
        tenant_id=DEMO_TENANT_ID,
        actor_id=DEMO_REQUESTER_ID,
        authority_ref=DEMO_REQUESTER_PRINCIPAL_REF,
    )
    assert resolver.has_role(
        tenant_id=DEMO_TENANT_ID,
        actor_id=DEMO_ARCHITECT_ID,
        authority_ref=DEMO_ARCHITECT_PRINCIPAL_REF,
    )
    # The pair, not either half, is what carries the authority: crossing them must refuse.
    assert not resolver.has_role(
        tenant_id=DEMO_TENANT_ID,
        actor_id=DEMO_REQUESTER_ID,
        authority_ref=DEMO_ARCHITECT_PRINCIPAL_REF,
    )
    assert not resolver.has_role(
        tenant_id="tenant-other",
        actor_id=DEMO_ARCHITECT_ID,
        authority_ref=DEMO_ARCHITECT_PRINCIPAL_REF,
    )


def test_the_role_resolver_refuses_an_unknown_actor() -> None:
    resolver = DemoRoleResolver()
    assert not resolver.has_role(
        tenant_id=DEMO_TENANT_ID,
        actor_id="intruder-demo",
        authority_ref=DEMO_ARCHITECT_PRINCIPAL_REF,
    )
    assert not resolver.has_role(
        tenant_id=DEMO_TENANT_ID,
        actor_id="intruder-demo",
        authority_ref="role:anything",
    )


def test_the_demonstration_clock_is_timezone_aware() -> None:
    moment = demo_clock()
    assert moment.tzinfo is not None
    assert moment.utcoffset() == datetime.now(UTC).utcoffset()


def test_the_demo_publication_resolves_to_a_fulfillment_snapshot(tmp_path: Path) -> None:
    stores = DemoStores(tmp_path / "state")
    try:
        published = build_demo_publication(stores, clock=_clock(NOW))
        resolver = build_demo_snapshot_resolver(
            publications=stores.publications, publication=published, clock=_clock(NOW)
        )
        resolved = resolver.resolve(tenant_id=DEMO_TENANT_ID, request=_question())
        # A refusal is a value here, not an exception, so it would otherwise pass silently.
        assert not isinstance(resolved, ResolutionFailure), (
            f"the demonstration publication was refused: {resolved!r}"
        )
        grounding, _policy = resolved
        assert [reference.artifact_id for reference in grounding.metric_refs] == [
            "daily-order-count"
        ]
        assert [reference.artifact_id for reference in grounding.classification_refs] == [
            "commercial"
        ]
    finally:
        stores.close()


def test_the_authority_observation_uses_the_demonstration_window(tmp_path: Path) -> None:
    stores = DemoStores(tmp_path / "state")
    try:
        published = build_demo_publication(stores, clock=_clock(NOW))
        observation = DemoAuthorityResolver(
            publications=stores.publications, publication=published, clock=_clock(NOW)
        ).resolve(tenant_id=DEMO_TENANT_ID, request=_question())
        assert not isinstance(observation, ResolutionFailure), (
            f"the demonstration authority was refused: {observation!r}"
        )
        # A shorter window would make the console refuse every request once it elapsed, and a
        # shorter maximum expiry would silently truncate the delivered grant.
        assert observation.valid_until == published.valid_until
        assert observation.maximum_expiry is not None
        assert observation.maximum_expiry >= published.valid_until
    finally:
        stores.close()


def test_a_question_naming_no_published_term_is_refused(tmp_path: Path) -> None:
    stores = DemoStores(tmp_path / "state")
    try:
        published = build_demo_publication(stores, clock=_clock(NOW))
        resolved = _resolve(
            stores, published, _question(question="What is the CEO's home address?")
        )
        assert isinstance(resolved, ResolutionFailure), (
            f"a question naming no published term was grounded anyway: {resolved!r}"
        )
        assert resolved.reason_codes == ("published_semantic_term_not_found",)
    finally:
        stores.close()


def test_a_question_naming_two_published_terms_is_refused_as_ambiguous(tmp_path: Path) -> None:
    stores = DemoStores(tmp_path / "state")
    try:
        published = build_demo_publication(stores, clock=_clock(NOW))
        resolved = _resolve(
            stores,
            published,
            _question(question="Is the daily order count commercial information?"),
        )
        assert isinstance(resolved, ResolutionFailure), (
            f"a question naming two published terms was grounded anyway: {resolved!r}"
        )
        assert resolved.reason_codes == ("published_semantic_term_ambiguous",)
    finally:
        stores.close()


def test_a_data_access_request_for_an_unknown_product_is_refused(tmp_path: Path) -> None:
    stores = DemoStores(tmp_path / "state")
    try:
        published = build_demo_publication(stores, clock=_clock(NOW))
        resolved = _resolve(stores, published, _access_request(data_product_id="payroll_secrets"))
        assert isinstance(resolved, ResolutionFailure), (
            f"an unpublished data product was grounded anyway: {resolved!r}"
        )
        assert resolved.reason_codes == ("published_data_product_not_found",)
    finally:
        stores.close()


def test_the_daily_order_count_question_answers_about_the_metric_and_cites_it(
    tmp_path: Path,
) -> None:
    stores = DemoStores(tmp_path / "state")
    try:
        published = build_demo_publication(stores, clock=_clock(NOW))
        resolved = _resolve(stores, published, _question())
        assert not isinstance(resolved, ResolutionFailure), (
            f"the demonstration question was refused: {resolved!r}"
        )
        grounding, _policy = resolved
        draft = DemoAnswerCandidateProvider(publication=published).propose(
            request=_question(), grounding=grounding
        )
        # The demonstration restates the approved definition and never invents a number.
        assert draft.answer_text == (
            "Daily order count is confirmed customer orders per calendar day."
        )
        assert [reference.artifact_id for reference in draft.metric_refs] == ["daily-order-count"]
        assert draft.disclosure_classifications == ()
    finally:
        stores.close()


def _answer(
    stores: DemoStores, published: DemoPublication, request: InboxRequest
) -> StakeholderAnswerDraft:
    resolved = _resolve(stores, published, request)
    assert not isinstance(resolved, ResolutionFailure), (
        f"the question was refused rather than answered: {resolved!r}"
    )
    grounding, _policy = resolved
    return DemoAnswerCandidateProvider(publication=published).propose(
        request=request, grounding=grounding
    )


def test_a_question_naming_the_classification_answers_about_that_term(tmp_path: Path) -> None:
    stores = DemoStores(tmp_path / "state")
    try:
        published = build_demo_publication(stores, clock=_clock(NOW))
        request = _question(question="Is this commercial?")
        resolved = _resolve(stores, published, request)
        assert not isinstance(resolved, ResolutionFailure), (
            f"the classification question was refused: {resolved!r}"
        )
        grounding, _policy = resolved
        draft = DemoAnswerCandidateProvider(publication=published).propose(
            request=request, grounding=grounding
        )
        assert draft.answer_text == "Commercial is commercially sensitive information."
        # A classification is not a metric, so citing the publication's one metric here
        # would attach a number to an answer that never mentions one.
        assert draft.metric_refs == ()
    finally:
        stores.close()


def test_a_question_naming_the_entity_answers_about_that_term(tmp_path: Path) -> None:
    stores = DemoStores(tmp_path / "state")
    try:
        published = build_demo_publication(stores, clock=_clock(NOW))
        draft = _answer(stores, published, _question(question="What is an order?"))
        assert draft.answer_text == "Order is a confirmed customer order."
        # An entity is not a metric: without the filter this cites the publication's one
        # metric, and asserting only the text would pass blind.
        assert draft.metric_refs == ()
    finally:
        stores.close()


def test_the_demonstration_question_grounds_and_answers(tmp_path: Path) -> None:
    """The question the console seeds must name exactly one published term."""
    stores = DemoStores(tmp_path / "state")
    try:
        published = build_demo_publication(stores, clock=_clock(NOW))
        assert _answer(stores, published, _question(question=DEMO_QUESTION)).answer_text == (
            "Daily order count is confirmed customer orders per calendar day."
        )
    finally:
        stores.close()
