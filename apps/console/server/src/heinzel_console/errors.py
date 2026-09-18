from __future__ import annotations

from .contracts import RecoveryAction


class ConsoleError(Exception):
    def __init__(
        self,
        *,
        code: str,
        safe_message: str,
        recovery_action: RecoveryAction,
        field: str | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.recovery_action = recovery_action
        self.field = field


class ConsoleUnauthenticated(ConsoleError):
    pass


class ConsoleNotFound(ConsoleError):
    def __init__(self) -> None:
        super().__init__(
            code="not_found",
            safe_message="The requested resource is unavailable.",
            recovery_action="none",
        )


class ConsoleConflict(ConsoleError):
    pass


class ConsoleUnavailable(ConsoleError):
    pass


class ConsoleInvalidRequest(ConsoleError):
    pass
