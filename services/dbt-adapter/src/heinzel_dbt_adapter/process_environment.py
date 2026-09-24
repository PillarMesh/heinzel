from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

_CREDENTIAL_NAME = re.compile(r"^(?:HEINZEL_DBT_|DBT_ENV_SECRET_)[A-Z0-9_]+$")
_LOADER_KEYS = frozenset(
    {
        "BASH_ENV",
        "ENV",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "NODE_OPTIONS",
        "PYTHONHOME",
        "PYTHONPATH",
        "RUBYOPT",
        "ZDOTDIR",
    }
)


def build_dbt_process_environment(
    *,
    source_environment: Mapping[str, str],
    credential_names: tuple[str, ...],
    temporary_directory: Path,
) -> dict[str, str]:
    if not temporary_directory.is_absolute() or not temporary_directory.is_dir():
        raise ValueError("dbt temporary directory must be an existing absolute directory")
    if len(credential_names) != len(set(credential_names)):
        raise ValueError("dbt credential names must be unique")

    resolved_temporary_directory = str(temporary_directory.resolve())
    environment = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": resolved_temporary_directory,
        "TMPDIR": resolved_temporary_directory,
        "TMP": resolved_temporary_directory,
        "TEMP": resolved_temporary_directory,
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    for name in credential_names:
        if name in _LOADER_KEYS or name.startswith("DYLD_"):
            raise ValueError("process loader controls cannot be dbt credentials")
        if _CREDENTIAL_NAME.fullmatch(name) is None:
            raise ValueError("dbt credential name must use an approved secret prefix")
        value = source_environment.get(name)
        if not value:
            raise ValueError(f"dbt credential {name} is missing or empty")
        environment[name] = value
    return environment
