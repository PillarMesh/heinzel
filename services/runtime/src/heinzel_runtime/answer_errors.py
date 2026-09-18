from __future__ import annotations


class AnswerExecutionError(RuntimeError):
    pass


class AnswerExecutionIntegrityError(AnswerExecutionError):
    pass


class AnswerExecutionAuthorizationError(AnswerExecutionError):
    pass


class AnswerExecutionConflict(AnswerExecutionError):
    pass


class QueryResultNotFound(AnswerExecutionError):
    pass


class AnswerQueryTimedOut(AnswerExecutionError):
    pass


class AnswerQueryAborted(AnswerExecutionError):
    pass
