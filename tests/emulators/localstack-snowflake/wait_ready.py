from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.emulators.localstack_support import (  # noqa: E402
    localstack_provider,
    wait_for_expected_observation,
)


def main() -> int:
    provider = localstack_provider()
    try:
        wait_for_expected_observation(
            provider.observe,
            deadline=time.monotonic() + 90,
        )
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("LocalStack Snowflake schema initialized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
