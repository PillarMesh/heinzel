from __future__ import annotations

from typing import Literal


class GovernedAnswerError(Exception):
    pass


class GovernedAnswerNotVisible(GovernedAnswerError):
    pass


class GovernedAnswerVerificationError(GovernedAnswerError):
    pass


class GovernedAnswerConflict(GovernedAnswerError):
    pass


class GovernedAnswerStaleRevision(GovernedAnswerError):
    pass


class GovernedAnswerExecutionAuthorizationDenied(GovernedAnswerError):
    pass


class GovernedAnswerExecutionAuthorizationUnavailable(GovernedAnswerError):
    pass


class GovernedAnswerDeliveryAuthorizationUnavailable(GovernedAnswerError):
    pass


class GovernedAnswerExecutionDeferred(GovernedAnswerError):
    def __init__(self, stage: Literal["authorization", "incident projection"]) -> None:
        super().__init__(f"governed answer {stage} unavailable")
        self.stage = stage


class GovernedAnswerExecutionFailed(GovernedAnswerError):
    def __init__(self, stage: Literal["authorization", "execution", "verification"]) -> None:
        super().__init__(f"governed answer {stage} failed")
        self.stage = stage
