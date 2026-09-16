from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from .answer_models import (
    AnswerIntentCandidate,
    AnswerQuestion,
    AnswerQuestionIntent,
    AnswerQuestionValidationResult,
)
from .answer_policy import AnswerScopePolicy
from .answer_validation import AnswerValidationContext, validate_answer_intent
from .answer_validation_repository import AnswerValidationRepository


class AnswerInterpreter(Protocol):
    def interpret(self, question: AnswerQuestion) -> AnswerIntentCandidate: ...


class AnswerInterpretationDenied(ValueError):
    pass


class AnswerCurrentPolicyResolver(Protocol):
    def resolve_current(
        self, *, tenant_id: str, principal_ref: str, purpose: str
    ) -> AnswerScopePolicy | None: ...


class AnswerQuestionService:
    def __init__(
        self,
        interpreter: AnswerInterpreter,
        repository: AnswerValidationRepository,
        *,
        policy_resolver: AnswerCurrentPolicyResolver,
        intent_identifier: Callable[[], str],
        validation_identifier: Callable[[], str],
        clock: Callable[[], datetime],
    ) -> None:
        self._interpreter = interpreter
        self._repository = repository
        self._policy_resolver = policy_resolver
        self._intent_identifier = intent_identifier
        self._validation_identifier = validation_identifier
        self._clock = clock

    def interpret_and_validate(
        self,
        *,
        question: AnswerQuestion,
        policy: AnswerScopePolicy,
        context: AnswerValidationContext,
    ) -> AnswerQuestionValidationResult:
        question = AnswerQuestion.model_validate(question.model_dump(mode="python"), strict=True)
        policy = AnswerScopePolicy.model_validate(policy.model_dump(mode="python"), strict=True)
        context = AnswerValidationContext.model_validate(
            context.model_dump(mode="python"), strict=True
        )
        policy = self._require_current_policy(
            question=question, expected_policy=policy, context=context
        )
        now = self._clock()
        if not (
            now.tzinfo is not None
            and now.utcoffset() == timedelta(0)
            and question.tenant_id == policy.tenant_id
            and context.requester_principal_ref in policy.principal_scope
            and context.purpose in policy.purposes
            and context.latest_policy_revision == policy.revision
            and context.semantic_version_digest == policy.semantic_version_ref.digest
            and not context.authority_conflict
            and policy.valid_from <= now < policy.valid_until
            and not (context.acting_as_agent and policy.agent_access == "denied")
            and not (question.interpreter == "model" and policy.model_disclosure == "none")
        ):
            raise AnswerInterpretationDenied("answer interpretation is not authorized")
        candidate = self._interpreter.interpret(question)
        created_at = self._clock()
        policy = self._require_current_policy(
            question=question, expected_policy=policy, context=context
        )
        intent = AnswerQuestionIntent(
            intent_id=self._intent_identifier(),
            tenant_id=question.tenant_id,
            request_id=question.request_id,
            request_revision=question.request_revision,
            question_digest=question.question_digest,
            interpreter=question.interpreter,
            interpreter_ref=question.interpreter_ref,
            created_at=created_at,
            **candidate.model_dump(),
        )
        validation = validate_answer_intent(
            validation_id=self._validation_identifier(),
            intent=intent,
            policy=policy,
            context=context,
            created_at=created_at,
        )
        result = AnswerQuestionValidationResult(
            intent=intent,
            validation=validation,
            restatement_confirmation_required=policy.requires_restatement_confirmation(
                interpreter=question.interpreter
            ),
        )
        return self._repository.save(result)

    def _require_current_policy(
        self,
        *,
        question: AnswerQuestion,
        expected_policy: AnswerScopePolicy,
        context: AnswerValidationContext,
    ) -> AnswerScopePolicy:
        current = self._policy_resolver.resolve_current(
            tenant_id=question.tenant_id,
            principal_ref=context.requester_principal_ref,
            purpose=context.purpose,
        )
        if current != expected_policy:
            raise AnswerInterpretationDenied("answer interpretation is not authorized")
        return current
