from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import TypedDict

import pytest
from pillarmesh_console.auth import TrustedActorContext
from pillarmesh_console.contracts import (
    ActorRole,
    ClarifiedOutcomeAcceptanceCommand,
    DecisionCommand,
    ResetCommand,
    RetryOperationCommand,
    SetupView,
    WarehouseBindingCommand,
    setup_snapshot_digest,
)
from pillarmesh_console.errors import ConsoleConflict, ConsoleNotFound, ConsoleUnavailable
from pillarmesh_console.fixture_backend import FixtureConsoleBackend
from pillarmesh_console.fixture_data import build_fixture_seed

_SETUP_DIGEST = build_fixture_seed().setup.setup_digest
_BLOCKED_REQUEST_DIGEST = "b" * 64


class _ContextOverride(TypedDict, total=False):
    """The `_context` keywords a parametrized case may override.

    `dict[str, object]` erased the role vocabulary, so a case naming a role the
    console does not define would have read as a legitimate authority rejection.
    """

    actor_id: str
    tenant_id: str
    active_role: ActorRole
    roles: tuple[ActorRole, ...] | None


def _context(
    *,
    actor_id: str = "actor-architect",
    tenant_id: str = "tenant-primary",
    active_role: ActorRole = "data_architect",
    roles: tuple[ActorRole, ...] | None = None,
) -> TrustedActorContext:
    return TrustedActorContext(
        tenant_id=tenant_id,
        actor_id=actor_id,
        roles=roles or (active_role,),
        active_role=active_role,
        session_id=f"session-{actor_id}",
    )


def _warehouse_command(engine: str = "postgresql") -> WarehouseBindingCommand:
    return WarehouseBindingCommand.model_validate(
        {
            "expected_revision": 1,
            "reviewed_digest": _SETUP_DIGEST,
            "active_role": "data_architect",
            "engine": engine,
            "region": "us-west-2",
            "capacity": "fixed-small",
        }
    )


def _reset_command(setup: SetupView, **updates: object) -> ResetCommand:
    return ResetCommand.model_validate(
        {
            "expected_revision": setup.revision,
            "setup_digest": setup.setup_digest,
            "reset_token": setup.reset_token,
            "active_role": "data_architect",
        }
        | updates
    )


def test_fresh_fixture_setup_starts_at_foundation_with_complete_scenario_inventory() -> None:
    backend = FixtureConsoleBackend()

    setup = backend.get_setup(_context())
    workspace = backend.get_workspace(_context())
    inbox = backend.get_inbox(_context())

    assert setup.active_stage == "foundation"
    assert [stage.stage for stage in setup.stages] == [
        "foundation",
        "managed_services",
        "sources",
        "business_process",
        "meaning",
        "data_product",
        "activation",
    ]
    assert [option.engine for option in setup.warehouse_options] == ["postgresql", "clickhouse"]
    assert setup.pending_review_refs == (
        "review-meaning",
        "review-data-product",
        "review-activation",
    )
    assert any(capability.state == "not_delivered" for capability in workspace.capabilities)
    assert {item.kind for item in inbox.items} == {"stakeholder_question", "data_access"}
    assert {item.request_id for item in inbox.items} >= {
        "request-stale",
        "request-no-valid-plan",
        "request-blocked-acceptance",
    }


def test_postgresql_confirmation_is_accepted_and_exact_replay_returns_same_handle() -> None:
    backend = FixtureConsoleBackend()
    command = _warehouse_command()

    first = backend.confirm_warehouse_binding(_context(), command)
    replay = backend.confirm_warehouse_binding(_context(), command)

    # `== "accepted"` already excludes every other state, "succeeded" included.
    assert first.state == "accepted"
    assert replay == first
    assert replay.operation_id == first.operation_id
    assert replay.evidence_ref is None
    assert "provider" not in replay.model_dump_json()


def test_material_setup_change_advances_revision_and_rebinds_the_exact_digest() -> None:
    backend = FixtureConsoleBackend()
    before = backend.get_setup(_context())

    backend.confirm_warehouse_binding(_context(), _warehouse_command())
    after = backend.get_setup(_context())

    assert after.revision == before.revision + 1
    assert after.setup_digest != before.setup_digest
    assert after.setup_digest == setup_snapshot_digest(after)


def test_concurrent_domain_replay_mints_only_one_console_operation_handle() -> None:
    backend = FixtureConsoleBackend()
    command = _warehouse_command()

    with ThreadPoolExecutor(max_workers=8) as executor:
        operations = tuple(
            executor.map(
                lambda _: backend.confirm_warehouse_binding(_context(), command),
                range(16),
            )
        )

    assert {operation.operation_id for operation in operations} == {"operation-0001"}
    assert {operation.state for operation in operations} == {"accepted"}


def test_changing_an_immutable_warehouse_engine_is_rejected() -> None:
    backend = FixtureConsoleBackend()
    backend.confirm_warehouse_binding(_context(), _warehouse_command("postgresql"))

    with pytest.raises(ConsoleConflict) as raised:
        backend.confirm_warehouse_binding(_context(), _warehouse_command("clickhouse"))

    assert raised.value.code == "immutable_warehouse_binding"
    assert "postgresql" not in raised.value.safe_message.lower()


def test_requester_projection_excludes_proposal_authority_and_evidence_details() -> None:
    backend = FixtureConsoleBackend()

    requests = backend.get_requester_requests(
        _context(actor_id="actor-requester", active_role="requester")
    )
    serialized = "\n".join(request.model_dump_json() for request in requests)

    for forbidden in (
        "candidate",
        "effective_scope",
        "required_authorities",
        "reviewer",
        "evidence",
    ):
        assert forbidden not in serialized
    assert "request-blocked-acceptance" in serialized


def test_unaccepted_clarification_blocks_the_architect_decision() -> None:
    backend = FixtureConsoleBackend()
    detail = backend.get_request_detail(_context(), "request-blocked-acceptance")
    assert detail.proposal_digest == _BLOCKED_REQUEST_DIGEST

    command = DecisionCommand(
        expected_revision=detail.revision,
        reviewed_digest=_BLOCKED_REQUEST_DIGEST,
        active_role="data_architect",
        decision="approve",
    )

    with pytest.raises(ConsoleConflict) as raised:
        backend.decide_request(_context(), "request-blocked-acceptance", command)

    assert raised.value.code == "requester_acceptance_required"


def test_fixture_contains_stale_and_no_valid_plan_failure_cases() -> None:
    backend = FixtureConsoleBackend()
    no_valid_plan = backend.get_request_detail(_context(), "request-no-valid-plan")
    stale = backend.get_request_detail(_context(), "request-stale")

    assert no_valid_plan.state == "investigating"
    assert no_valid_plan.available_actions == ()
    assert no_valid_plan.evidence.quality_summary == "No Valid Plan"
    assert stale.revision == 3

    with pytest.raises(ConsoleConflict) as raised:
        backend.decide_request(
            _context(),
            "request-stale",
            DecisionCommand(
                expected_revision=2,
                reviewed_digest=stale.proposal_digest or ("c" * 64),
                active_role="data_architect",
                decision="approve",
            ),
        )

    assert raised.value.code == "stale_revision"


def test_fixture_evidence_is_typed_unavailable_and_wrong_tenant_is_not_enumerating() -> None:
    backend = FixtureConsoleBackend()

    with pytest.raises(ConsoleUnavailable) as unavailable:
        backend.get_evidence(_context(), "evidence-synthetic")
    with pytest.raises(ConsoleNotFound) as wrong_tenant:
        backend.get_setup(_context(tenant_id="tenant-other"))

    assert unavailable.value.code == "fixture_evidence_unavailable"
    assert wrong_tenant.value.code == "not_found"


def test_reset_restores_the_same_frozen_seed_and_sequence() -> None:
    backend = FixtureConsoleBackend()
    first_operation = backend.confirm_warehouse_binding(_context(), _warehouse_command())
    changed_setup = backend.get_setup(_context())

    backend.reset(_context(), _reset_command(changed_setup))

    setup = backend.get_setup(_context())
    replayed_first_operation = backend.confirm_warehouse_binding(
        _context(),
        _warehouse_command().model_copy(update={"reviewed_digest": setup.setup_digest}),
    )
    assert setup.active_stage == "foundation"
    assert setup.warehouse_binding is None
    assert setup.reset_token != changed_setup.reset_token
    # Reset restores the frozen seed, so the digest returns to the seed's - that
    # the restoration is observable is the point. Only the reset token differs,
    # because each reset must be separately authorized.
    assert setup.setup_digest == _SETUP_DIGEST
    assert setup.setup_digest == setup_snapshot_digest(setup)
    assert replayed_first_operation == first_operation


def test_reset_exact_command_replays_the_committed_setup_after_state_replacement() -> None:
    backend = FixtureConsoleBackend()
    backend.confirm_warehouse_binding(_context(), _warehouse_command())
    before = backend.get_setup(_context())
    command = _reset_command(before)

    committed = backend.reset(_context(), command)
    next_committed = backend.reset(_context(), _reset_command(committed))
    replay = backend.reset(_context(), command)

    assert replay == committed
    assert backend.get_setup(_context()) == next_committed
    assert committed.reset_token != command.reset_token


def test_reset_replay_history_retains_the_immediate_bound_without_growing_forever() -> None:
    backend = FixtureConsoleBackend()
    initial = backend.get_setup(_context())
    first_command = _reset_command(initial)
    first_committed = backend.reset(_context(), first_command)
    current = first_committed

    for _ in range(15):
        current = backend.reset(_context(), _reset_command(current))

    assert backend.reset(_context(), first_command) == first_committed
    assert backend.get_setup(_context()) == current

    backend.reset(_context(), _reset_command(current))
    with pytest.raises(ConsoleConflict) as expired:
        backend.reset(_context(), first_command)

    assert expired.value.code == "stale_reset_token"


def test_reset_requires_the_reloaded_token_for_each_new_intentional_reset() -> None:
    backend = FixtureConsoleBackend()
    initial = backend.get_setup(_context())

    first = backend.reset(_context(), _reset_command(initial))
    second = backend.reset(_context(), _reset_command(first))

    assert first.reset_token != initial.reset_token
    assert second.reset_token != first.reset_token
    # Each reset needs a freshly reloaded token, which is what makes it
    # intentional and single-use. The digest identifies the setup state, so two
    # resets to the same frozen seed agree - it is not a per-read nonce.
    assert second.setup_digest == first.setup_digest
    assert second.setup_digest == setup_snapshot_digest(second)


def test_consumed_reset_token_rejects_changed_canonical_command_without_mutation() -> None:
    backend = FixtureConsoleBackend()
    before = backend.get_setup(_context())
    command = _reset_command(before)
    committed = backend.reset(_context(), command)

    with pytest.raises(ConsoleConflict) as mismatch:
        backend.reset(
            _context(),
            _reset_command(before, setup_digest="f" * 64),
        )

    assert mismatch.value.code == "command_identity_mismatch"
    assert backend.get_setup(_context()) == committed


def test_consumed_reset_token_is_bound_to_the_original_principal() -> None:
    backend = FixtureConsoleBackend()
    before = backend.get_setup(_context())
    command = _reset_command(before)
    committed = backend.reset(_context(), command)

    with pytest.raises(ConsoleNotFound):
        backend.reset(_context(actor_id="actor-other-architect"), command)

    assert backend.get_setup(_context()) == committed


@pytest.mark.parametrize(
    ("command_update", "context_update"),
    (
        ({"expected_revision": 999}, {}),
        ({"setup_digest": "f" * 64}, {}),
        ({"reset_token": "reset_token_fixture_sequence-9999"}, {}),
        ({}, {"actor_id": "actor-other-architect"}),
        ({}, {"active_role": "data_owner"}),
        ({}, {"tenant_id": "tenant-other"}),
    ),
    ids=(
        "stale-revision",
        "wrong-digest",
        "wrong-token",
        "wrong-principal",
        "wrong-role",
        "wrong-tenant",
    ),
)
def test_reset_rejects_stale_or_untrusted_authority_without_changing_state(
    command_update: dict[str, object], context_update: _ContextOverride
) -> None:
    backend = FixtureConsoleBackend()
    backend.confirm_warehouse_binding(_context(), _warehouse_command())
    before = backend.get_setup(_context())
    command = _reset_command(before, **command_update)

    with pytest.raises((ConsoleConflict, ConsoleNotFound)):
        backend.reset(_context(**context_update), command)

    assert backend.get_setup(_context()) == before


def test_reset_conflicts_preserve_exact_reload_recovery_classification() -> None:
    backend = FixtureConsoleBackend()
    setup = backend.get_setup(_context())
    base = {
        "expected_revision": setup.revision,
        "setup_digest": setup.setup_digest,
        "reset_token": setup.reset_token,
        "active_role": "data_architect",
    }

    with pytest.raises(ConsoleConflict) as stale_revision:
        backend.reset(
            _context(),
            ResetCommand.model_validate(base | {"expected_revision": setup.revision + 1}),
        )
    with pytest.raises(ConsoleConflict) as stale_digest:
        backend.reset(
            _context(),
            ResetCommand.model_validate(base | {"setup_digest": "f" * 64}),
        )

    assert stale_revision.value.code == "stale_revision"
    assert stale_revision.value.safe_message == "The setup changed; reload before resetting it."
    assert stale_revision.value.recovery_action == "reload"
    assert stale_digest.value.code == "stale_digest"
    assert stale_digest.value.safe_message == "The setup changed; reload before resetting it."
    assert stale_digest.value.recovery_action == "reload"


def test_retry_is_constructible_from_the_public_operation_and_exact_replay_is_stable() -> None:
    backend = FixtureConsoleBackend()
    context = _context()
    operation = backend.get_operation(context, "operation-retryable")
    assert operation.operation_digest is not None
    assert operation.retry_token is not None

    command = RetryOperationCommand(
        expected_revision=operation.revision,
        operation_digest=operation.operation_digest,
        retry_token=operation.retry_token,
        active_role="data_architect",
    )
    first = backend.retry_operation(context, operation.operation_id, command)
    replay = backend.retry_operation(context, operation.operation_id, command)

    assert operation.operation_digest == "9" * 64
    assert operation.operation_id not in operation.retry_token
    assert first == replay
    assert first.state == "accepted"
    assert first.phase == "retry_reconciliation"
    assert backend.get_operation(context, first.operation_id) == first
    retired = backend.get_operation(context, operation.operation_id)
    assert retired.revision == operation.revision + 1
    assert retired.operation_digest is None
    assert retired.retry_token is None
    assert retired.recovery_actions == ()
    second_operation = backend.confirm_warehouse_binding(context, _warehouse_command())
    assert second_operation.operation_id == "operation-0002"

    changed_command = RetryOperationCommand.model_validate(
        command.model_dump() | {"expected_revision": command.expected_revision + 1}
    )
    with pytest.raises(ConsoleNotFound):
        backend.retry_operation(context, operation.operation_id, changed_command)


def test_domain_replay_rejects_changed_command_content_for_the_same_identity() -> None:
    backend = FixtureConsoleBackend()
    context = _context(actor_id="actor-owner", active_role="data_owner")
    review = backend.get_review(context, "review-data-product")
    command_values = {
        "expected_revision": review.revision,
        "reviewed_digest": review.reviewed_digest,
        "active_role": "data_owner",
    }
    backend.decide_review(
        context,
        review.review_id,
        DecisionCommand.model_validate({**command_values, "decision": "approve"}),
    )

    with pytest.raises(ConsoleConflict) as mismatch:
        backend.decide_review(
            context,
            review.review_id,
            DecisionCommand.model_validate({**command_values, "decision": "reject"}),
        )

    assert mismatch.value.code == "command_identity_mismatch"


@pytest.mark.parametrize(
    "context",
    (
        _context(tenant_id="tenant-other"),
        _context(actor_id="actor-other"),
        _context(
            actor_id="actor-architect",
            active_role="data_owner",
            roles=("data_architect", "data_owner"),
        ),
    ),
    ids=("wrong-tenant", "wrong-actor", "wrong-role"),
)
def test_retry_token_authority_denials_are_non_enumerating(context: TrustedActorContext) -> None:
    backend = FixtureConsoleBackend()
    operation = backend.get_operation(_context(), "operation-retryable")
    assert operation.operation_digest is not None
    assert operation.retry_token is not None
    command = RetryOperationCommand(
        expected_revision=operation.revision,
        operation_digest=operation.operation_digest,
        retry_token=operation.retry_token,
        active_role=context.active_role,
    )

    with pytest.raises(ConsoleNotFound) as raised:
        backend.retry_operation(context, operation.operation_id, command)

    assert raised.value.code == "not_found"


def test_retry_token_expires_at_the_exact_private_binding_boundary() -> None:
    from pillarmesh_console.fixture_data import FIXED_TIME

    class MutableClock:
        current = FIXED_TIME

        def __call__(self):  # type: ignore[no-untyped-def]
            return self.current

    clock = MutableClock()
    backend = FixtureConsoleBackend(clock=clock)
    context = _context()
    operation = backend.get_operation(context, "operation-retryable")
    assert operation.operation_digest is not None
    assert operation.retry_token is not None
    command = RetryOperationCommand(
        expected_revision=operation.revision,
        operation_digest=operation.operation_digest,
        retry_token=operation.retry_token,
        active_role="data_architect",
    )
    clock.current = FIXED_TIME + timedelta(days=1)

    with pytest.raises(ConsoleNotFound):
        backend.retry_operation(context, operation.operation_id, command)

    expired = backend.get_operation(context, operation.operation_id)
    assert expired.operation_digest is None
    assert expired.retry_token is None


def test_retry_rejects_mismatched_token_without_enumeration_and_stale_revision_as_conflict() -> (
    None
):
    backend = FixtureConsoleBackend()
    operation = backend.get_operation(_context(), "operation-retryable")
    command_values = {
        "expected_revision": operation.revision,
        "operation_digest": operation.operation_digest,
        "retry_token": operation.retry_token,
        "active_role": "data_architect",
    }

    with pytest.raises(ConsoleNotFound) as mismatched:
        backend.retry_operation(
            _context(),
            operation.operation_id,
            RetryOperationCommand.model_validate(
                {**command_values, "retry_token": "opaque_retry_" + ("8" * 52)}
            ),
        )
    with pytest.raises(ConsoleConflict) as stale:
        backend.retry_operation(
            _context(),
            operation.operation_id,
            RetryOperationCommand.model_validate(
                {**command_values, "expected_revision": operation.revision + 1}
            ),
        )

    assert mismatched.value.code == "not_found"
    assert stale.value.code == "stale_revision"


def test_review_reads_and_commands_require_the_bound_authority() -> None:
    backend = FixtureConsoleBackend()
    owner = _context(actor_id="actor-owner", active_role="data_owner")
    review = backend.get_review(owner, "review-data-product")

    assert review.required_authorities[0].role == "data_owner"
    with pytest.raises(ConsoleNotFound):
        backend.get_review(_context(), "review-data-product")
    with pytest.raises(ConsoleNotFound):
        backend.decide_review(
            _context(),
            "review-data-product",
            DecisionCommand(
                expected_revision=review.revision,
                reviewed_digest=review.reviewed_digest,
                active_role="data_architect",
                decision="approve",
            ),
        )


def test_requester_lists_are_owner_filtered_and_wrong_owner_actions_do_not_enumerate() -> None:
    backend = FixtureConsoleBackend()
    owner = _context(actor_id="actor-requester", active_role="requester")
    stranger = _context(actor_id="actor-other-requester", active_role="requester")

    assert backend.get_requester_requests(owner)
    assert backend.get_requester_requests(stranger) == ()
    with pytest.raises(ConsoleNotFound):
        backend.get_conversation(stranger, "request-blocked-acceptance")
    with pytest.raises(ConsoleNotFound):
        backend.accept_clarified_outcome(
            stranger,
            "request-blocked-acceptance",
            ClarifiedOutcomeAcceptanceCommand(
                expected_revision=2,
                clarified_outcome_digest=_BLOCKED_REQUEST_DIGEST,
                active_role="requester",
                decision="approve",
            ),
        )


def test_requester_acceptance_advances_every_projection_and_stales_prepared_architect_action() -> (
    None
):
    backend = FixtureConsoleBackend()
    architect = _context()
    requester = _context(actor_id="actor-requester", active_role="requester")
    prepared_detail = backend.get_request_detail(architect, "request-blocked-acceptance")
    prepared = DecisionCommand(
        expected_revision=prepared_detail.revision,
        reviewed_digest=_BLOCKED_REQUEST_DIGEST,
        active_role="data_architect",
        decision="approve",
    )

    accepted = backend.accept_clarified_outcome(
        requester,
        "request-blocked-acceptance",
        ClarifiedOutcomeAcceptanceCommand(
            expected_revision=2,
            clarified_outcome_digest=_BLOCKED_REQUEST_DIGEST,
            active_role="requester",
            decision="approve",
        ),
    )
    detail = backend.get_request_detail(architect, "request-blocked-acceptance")
    requester_view = next(
        item
        for item in backend.get_requester_requests(requester)
        if item.request_id == "request-blocked-acceptance"
    )

    assert accepted.revision == detail.revision == requester_view.revision == 3
    assert detail.state == requester_view.state == "proposed"
    assert requester_view.clarified_outcome == accepted
    with pytest.raises(ConsoleConflict) as stale:
        backend.decide_request(architect, "request-blocked-acceptance", prepared)
    assert stale.value.code == "stale_revision"


def test_clock_failure_leaves_all_acceptance_projections_and_replay_unchanged() -> None:
    class SwitchableClock:
        fail = False

        def __call__(self):  # type: ignore[no-untyped-def]
            if self.fail:
                raise RuntimeError("clock failure canary")
            from pillarmesh_console.fixture_data import FIXED_TIME

            return FIXED_TIME

    clock = SwitchableClock()
    backend = FixtureConsoleBackend(clock=clock)
    architect = _context()
    requester = _context(actor_id="actor-requester", active_role="requester")
    command = ClarifiedOutcomeAcceptanceCommand(
        expected_revision=2,
        clarified_outcome_digest=_BLOCKED_REQUEST_DIGEST,
        active_role="requester",
        decision="approve",
    )
    before_detail = backend.get_request_detail(architect, "request-blocked-acceptance")
    before_outcome = backend.get_clarified_outcome(requester, "request-blocked-acceptance")
    before_requester = backend.get_requester_requests(requester)

    clock.fail = True
    with pytest.raises(RuntimeError, match="clock failure canary"):
        backend.accept_clarified_outcome(requester, "request-blocked-acceptance", command)
    clock.fail = False

    assert backend.get_request_detail(architect, "request-blocked-acceptance") == before_detail
    assert backend.get_clarified_outcome(requester, "request-blocked-acceptance") == before_outcome
    assert backend.get_requester_requests(requester) == before_requester
    assert backend.accept_clarified_outcome(
        requester, "request-blocked-acceptance", command
    ).accepted
