from __future__ import annotations

from pillarmesh_request_management.intake import RequestIntakeContent
from pillarmesh_request_management.models import DataAccessRequest, StakeholderQuestion

from .contracts import CreateRequestCommand


def request_intake_content(command: CreateRequestCommand) -> RequestIntakeContent:
    request = command.request
    payload = (
        StakeholderQuestion(purpose=request.purpose, question=request.question)
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
