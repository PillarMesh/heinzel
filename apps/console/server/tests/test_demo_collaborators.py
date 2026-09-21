from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from heinzel_console.demo.collaborators import (
    DEMO_ARCHITECT_ID,
    DEMO_ARCHITECT_PRINCIPAL_REF,
    DEMO_REQUESTER_ID,
    DEMO_REQUESTER_PRINCIPAL_REF,
    DemoAuthorityResolver,
    DemoRoleResolver,
    build_demo_snapshot_resolver,
    demo_clock,
)
from heinzel_console.demo.publication import DEMO_TENANT_ID, build_demo_publication
from heinzel_console.demo.stores import DemoStores
from heinzel_request_management import InboxRequest, RequestState, ResolutionFailure
from heinzel_request_management.models import StakeholderQuestion

NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)


def _clock(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


def _question(*, question: str = "How many orders per day?") -> InboxRequest:
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
