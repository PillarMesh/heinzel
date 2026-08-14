from typing import Literal


class ProviderError(RuntimeError):
    classification: Literal["retryable", "throttled", "authorization", "permanent", "ambiguous"]

    def __init__(
        self,
        message: str,
        classification: Literal[
            "retryable", "throttled", "authorization", "permanent", "ambiguous"
        ],
    ) -> None:
        super().__init__(message)
        self.classification = classification
