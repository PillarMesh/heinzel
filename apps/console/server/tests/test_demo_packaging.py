from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
DEMO = ROOT / "apps/console/server/src/heinzel_console/demo"

_DISTRIBUTION_BY_MODULE = {
    "heinzel_access_control": "heinzel-access-control",
    "heinzel_authoring_mcp": "heinzel-authoring-mcp",
    "heinzel_bi_control": "heinzel-bi-control",
    "heinzel_catalog_control": "heinzel-catalog-control",
    "heinzel_compiler": "heinzel-compiler",
    "heinzel_connection_broker": "heinzel-connection-broker",
    "heinzel_context_exposure": "heinzel-context-exposure",
    "heinzel_contract_model": "heinzel-contract-model",
    "heinzel_contract_service": "heinzel-contract-service",
    "heinzel_dbt_adapter": "heinzel-dbt-adapter",
    "heinzel_evidence": "heinzel-evidence",
    "heinzel_execution_graph": "heinzel-execution-graph",
    "heinzel_iir": "heinzel-iir",
    "heinzel_knowledge_graph": "heinzel-knowledge-graph",
    "heinzel_provider_clickhouse": "heinzel-provider-clickhouse",
    "heinzel_provider_openmetadata": "heinzel-provider-openmetadata",
    "heinzel_provider_postgresql": "heinzel-provider-postgresql",
    "heinzel_provider_sdk": "heinzel-provider-sdk",
    "heinzel_provider_snowflake": "heinzel-provider-snowflake",
    "heinzel_provider_stripe": "heinzel-provider-stripe",
    "heinzel_provider_superset": "heinzel-provider-superset",
    "heinzel_request_management": "heinzel-request-management",
    "heinzel_runtime": "heinzel-runtime",
    "heinzel_semantic_registry": "heinzel-semantic-registry",
    "heinzel_state": "heinzel-state",
    "heinzel_trigger": "heinzel-trigger",
    "heinzel_warehouse_control": "heinzel-warehouse-control",
}


def _imported_top_level_modules() -> set[str]:
    modules: set[str] = set()
    for path in sorted(DEMO.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])
    return modules


def _declared_distributions() -> set[str]:
    project = tomllib.loads(
        (ROOT / "apps/console/server/pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    return {
        entry.split(">")[0].split("=")[0].split("[")[0].split("<")[0].strip()
        for entry in project["dependencies"]
    }


def test_every_workspace_package_the_demonstration_imports_is_declared() -> None:
    required = {
        _DISTRIBUTION_BY_MODULE[module]
        for module in _imported_top_level_modules()
        if module in _DISTRIBUTION_BY_MODULE
    }
    assert required <= _declared_distributions(), sorted(required - _declared_distributions())


def test_the_demonstration_imports_no_workspace_package_this_check_does_not_know() -> None:
    unknown = {
        module
        for module in _imported_top_level_modules()
        if module.startswith("heinzel_") and module not in _DISTRIBUTION_BY_MODULE
    }
    assert unknown == set(), sorted(unknown)


def test_the_demonstration_imports_nothing_from_the_test_tree() -> None:
    assert "tests" not in _imported_top_level_modules()
