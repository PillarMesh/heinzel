from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from heinzel_dbt_adapter import (
    CompiledDbtModel,
    DbtColumnTest,
    DbtInvocationSpec,
    DbtSubprocessSettings,
    SubprocessDbtRunner,
)


def test_subprocess_runner_requires_existing_private_boundaries(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="dbt executable"):
        SubprocessDbtRunner(
            DbtSubprocessSettings(
                executable=tmp_path / "missing-dbt",
                profiles_directory=tmp_path / "missing-profiles",
                workspace_directory=tmp_path,
                timeout_seconds=30,
            )
        )


def test_subprocess_runner_disables_dbt_telemetry_for_every_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "dbt"
    executable.touch()
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.delenv("DBT_SEND_ANONYMOUS_USAGE_STATS", raising=False)
    runner = SubprocessDbtRunner(
        DbtSubprocessSettings(
            executable=executable,
            profiles_directory=profiles,
            workspace_directory=tmp_path,
            timeout_seconds=30,
        )
    )
    invocation = DbtInvocationSpec(
        required_dbt_version="1.10.13",
        arguments=("dbt", "build"),
        model=CompiledDbtModel(
            model_name="product_orders",
            contract_digest="1" * 64,
            provider="postgresql",
            input_generation_digests=("2" * 64,),
            target_schema="contract_" + "1" * 54,
            compiled_sql="select 1",
        ),
    )

    with patch(
        "heinzel_dbt_adapter.invocation.subprocess.run",
        side_effect=(
            SimpleNamespace(stdout="Core installed: 1.10.13", stderr="", returncode=0),
            SimpleNamespace(stdout=b"", stderr=b"", returncode=0),
        ),
    ) as run:
        runner.run(invocation)

    assert len(run.call_args_list) == 2
    assert all(
        call.kwargs["env"]["DBT_SEND_ANONYMOUS_USAGE_STATS"] == "false"
        for call in run.call_args_list
    )


def test_subprocess_runner_generates_declared_tests_and_only_forwards_approved_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "dbt"
    executable.touch()
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setenv("HEINZEL_DBT_PASSWORD", "approved-secret")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")
    observed: dict[str, object] = {}

    def run_process(*arguments: object, **options: object) -> SimpleNamespace:
        if arguments[0] == (str(executable), "--version"):
            return SimpleNamespace(stdout="Core installed: 1.10.13", stderr="", returncode=0)
        operation_directory = Path(str(options["cwd"]))
        observed["schema"] = (operation_directory / "models" / "schema.yml").read_text()
        observed["environment"] = options["env"]
        return SimpleNamespace(stdout=b"", stderr=b"", returncode=0)

    runner = SubprocessDbtRunner(
        DbtSubprocessSettings(
            executable=executable,
            profiles_directory=profiles,
            workspace_directory=tmp_path,
            timeout_seconds=30,
            credential_environment_names=("HEINZEL_DBT_PASSWORD",),
        )
    )
    invocation = DbtInvocationSpec(
        required_dbt_version="1.10.13",
        arguments=("dbt", "build"),
        model=CompiledDbtModel(
            model_name="product_orders",
            contract_digest="1" * 64,
            provider="postgresql",
            input_generation_digests=("2" * 64,),
            target_schema="contract_" + "1" * 54,
            quality_tests=(
                DbtColumnTest(column_name="order_id", kind="not_null"),
                DbtColumnTest(column_name="order_id", kind="unique"),
            ),
            compiled_sql="select 1",
        ),
    )

    with patch("heinzel_dbt_adapter.invocation.subprocess.run", side_effect=run_process):
        runner.run(invocation)

    environment = observed["environment"]
    assert isinstance(environment, dict)
    assert environment["HEINZEL_DBT_PASSWORD"] == "approved-secret"
    assert "UNRELATED_SECRET" not in environment
    assert observed["schema"] == (
        "version: 2\n"
        "models:\n"
        "  - name: product_orders\n"
        "    columns:\n"
        "      - name: order_id\n"
        "        data_tests:\n"
        "          - not_null\n"
        "          - unique\n"
    )


def test_subprocess_runner_rejects_unapproved_credential_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "dbt"
    executable.touch()
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setenv("PYTHONPATH", "unsafe")
    runner = SubprocessDbtRunner(
        DbtSubprocessSettings(
            executable=executable,
            profiles_directory=profiles,
            workspace_directory=tmp_path,
            timeout_seconds=30,
            credential_environment_names=("PYTHONPATH",),
        )
    )
    invocation = DbtInvocationSpec(
        required_dbt_version="1.10.13",
        arguments=("dbt", "build"),
        model=CompiledDbtModel(
            model_name="product_orders",
            contract_digest="1" * 64,
            provider="postgresql",
            input_generation_digests=("2" * 64,),
            target_schema="contract_" + "1" * 54,
            compiled_sql="select 1",
        ),
    )

    with pytest.raises(ValueError, match="loader controls"):
        runner.run(invocation)
