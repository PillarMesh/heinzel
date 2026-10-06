from __future__ import annotations

from heinzel_request_management.intake import RequestIntakeContent
from heinzel_request_management.models import (
    DataAccessRequest,
    QuestionTermSelection,
    StakeholderQuestion,
)

from .contracts import CreateRequestCommand, QuestionTermSelectionInput


def _selection(selection: QuestionTermSelectionInput | None) -> QuestionTermSelection | None:
    """The governed term selection the command carried, as the artifact that owns it.

    Rebuilt through `QuestionTermSelection` rather than trusted: a duplicated dimension, or a
    metric also named as a dimension, is refused by the artifact rather than stored and discovered
    by the interpreter. `None` stays `None`, which is a question submitted with no selection and
    is exactly what a request carried before the builder existed.
    """
    if selection is None:
        return None
    return QuestionTermSelection(
        metric_ref=selection.metric_ref, dimension_refs=tuple(selection.dimension_refs)
    )


def request_intake_content(command: CreateRequestCommand) -> RequestIntakeContent:
    request = command.request
    payload = (
        StakeholderQuestion(
            purpose=request.purpose,
            question=request.question,
            selection=_selection(request.selection),
        )
        if request.kind == "stakeholder_question"
        else DataAccessRequest(
            purpose=request.purpose,
            data_product_id=request.data_product_ref,
            requested_fields=tuple(request.requested_fields),
            access_mode=request.access_mode,
            expires_at=request.expires_at,
        )
    )
    return RequestIntakeContent(title=command.title, payload=payload)
