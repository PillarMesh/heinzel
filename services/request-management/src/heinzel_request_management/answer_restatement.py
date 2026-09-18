from __future__ import annotations

from typing import Protocol

from .answer_models import AnswerIntentValidation


class RestatementAcceptanceReader(Protocol):
    def confirmed_revision(
        self,
        *,
        acceptance_id: str,
        tenant_id: str,
        request_id: str,
        requester_id: str,
        validation: AnswerIntentValidation,
    ) -> int | None: ...
