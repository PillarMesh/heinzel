from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RunPrivateState:
    run_id: str
    acceptance_key: int | None
    segment_path: Path | None
