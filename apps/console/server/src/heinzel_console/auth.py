from __future__ import annotations

import hmac
import secrets
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition, RLock
from time import monotonic

from .contracts import ActorRole
from .errors import ConsoleConflict


@dataclass(frozen=True, slots=True)
class TrustedActorContext:
    tenant_id: str
    actor_id: str
    roles: tuple[ActorRole, ...]
    active_role: ActorRole
    session_id: str

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.actor_id or not self.session_id:
            raise ValueError("trusted actor context identifiers must be non-empty")
        if not self.roles or self.active_role not in self.roles:
            raise ValueError("trusted actor context active role must be held")


class SessionCsrfTokens:
    """Session-bound CSRF tokens, bounded in number.

    One entry per (tenant, actor, session) was never evicted, so a long-lived
    process accumulated a binding for every session it had ever seen. The map is
    ordered by issue, and the oldest binding is dropped once it is full; a caller
    whose binding was evicted simply issues a new token on its next read.
    """

    def __init__(self, *, maximum_bindings: int = 4096) -> None:
        if maximum_bindings < 1:
            raise ValueError("the CSRF binding limit must be positive")
        self._lock = RLock()
        self._maximum_bindings = maximum_bindings
        self._tokens: OrderedDict[tuple[str, str, str], str] = OrderedDict()

    @staticmethod
    def _binding(context: TrustedActorContext) -> tuple[str, str, str]:
        return (context.tenant_id, context.actor_id, context.session_id)

    def issue(self, context: TrustedActorContext) -> str:
        binding = self._binding(context)
        with self._lock:
            token = self._tokens.get(binding)
            if token is None:
                token = secrets.token_urlsafe(32)
                self._tokens[binding] = token
                while len(self._tokens) > self._maximum_bindings:
                    self._tokens.popitem(last=False)
            else:
                self._tokens.move_to_end(binding)
            return token

    def tracked_bindings(self) -> int:
        with self._lock:
            return len(self._tokens)

    def matches(self, context: TrustedActorContext, supplied_token: str) -> bool:
        binding = self._binding(context)
        with self._lock:
            expected_token = self._tokens.get(binding)
        return expected_token is not None and hmac.compare_digest(expected_token, supplied_token)


class InFlightCommandKeys:
    """Serialize concurrent submissions of one command identity.

    The wait is bounded. An unbounded one let a hung command hold every duplicate
    of itself, and each duplicate holds a threadpool worker, so a handful of
    retries - which the browser makes when a command's response is lost - could
    stop every synchronous route in the console, not just the stuck one.
    """

    def __init__(self, *, wait_timeout_seconds: float = 15.0) -> None:
        if wait_timeout_seconds <= 0:
            raise ValueError("the in-flight wait timeout must be positive")
        self._condition = Condition(RLock())
        self._active: set[tuple[str, str, str]] = set()
        self._wait_timeout_seconds = wait_timeout_seconds

    @contextmanager
    def serialize(self, context: TrustedActorContext, idempotency_key: str) -> Iterator[None]:
        identity = (context.tenant_id, context.actor_id, idempotency_key)
        deadline = monotonic() + self._wait_timeout_seconds
        with self._condition:
            while identity in self._active:
                remaining = deadline - monotonic()
                expired = remaining <= 0 or not self._condition.wait(timeout=remaining)
                if expired and identity in self._active:
                    raise ConsoleConflict(
                        code="command_in_flight",
                        safe_message=(
                            "An identical command is still running. "
                            "Reload the resource before retrying."
                        ),
                        recovery_action="reload",
                    )
            self._active.add(identity)
        try:
            yield
        finally:
            with self._condition:
                self._active.remove(identity)
                self._condition.notify_all()
