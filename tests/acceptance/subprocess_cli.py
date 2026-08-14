from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from .config import (
    PASSTHROUGH_VARIABLES,
    PRODUCT_VARIABLES,
    REPOSITORY_ROOT,
    HarnessError,
)

SUBPROCESS_ENVIRONMENT_VARIABLES = frozenset(
    (*PASSTHROUGH_VARIABLES, *PRODUCT_VARIABLES, "PILLARMESH_SCAN_INPUT_JSON")
)


class CliTimeout(HarnessError):
    pass


class SubprocessCli:
    def __init__(
        self,
        repository_root: Path = REPOSITORY_ROOT,
        *,
        command_prefix: tuple[str, ...] = ("uv", "run", "pillarmesh-m0"),
        timeout_seconds: float = 300,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._repository_root = repository_root
        self._command_prefix = command_prefix
        self._timeout_seconds = timeout_seconds

    def invoke(
        self,
        arguments: tuple[str, ...],
        *,
        stdin: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> object:
        supplied = os.environ if environment is None else environment
        child_environment = {
            name: value
            for name, value in supplied.items()
            if name in SUBPROCESS_ENVIRONMENT_VARIABLES
        }
        try:
            completed = subprocess.run(
                (*self._command_prefix, *arguments),
                cwd=self._repository_root,
                env=child_environment,
                input=stdin,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._timeout_seconds,
                umask=0o077,
            )
        except subprocess.TimeoutExpired:
            raise CliTimeout(f"CLI command timed out: {arguments[0]}") from None
        if completed.returncode != 0:
            raise HarnessError(f"CLI command failed: {arguments[0]}")
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError:
            raise HarnessError(f"CLI command returned invalid JSON: {arguments[0]}") from None
