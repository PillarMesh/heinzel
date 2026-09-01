from __future__ import annotations

import time
from collections.abc import Callable

from pillarmesh_provider_sdk import ProviderError

_RETRYABLE_CLASSIFICATIONS = frozenset(
    ("retryable", "throttled", "transient_transport", "transient_unavailable")
)


def retry_bounded[T](
    operation: Callable[[], T],
    *,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> T:
    started = monotonic()
    delays = (1.0, 5.0)
    for attempt in range(3):
        try:
            return operation()
        except ProviderError as error:
            if error.classification not in _RETRYABLE_CLASSIFICATIONS or attempt == 2:
                raise
            if monotonic() - started + delays[attempt] > 120:
                raise ProviderError(
                    "provider retry wall-clock budget exhausted", error.classification
                ) from error
            sleeper(delays[attempt])
    raise AssertionError("unreachable")
