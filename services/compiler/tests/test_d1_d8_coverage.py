from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import cast

_FIXTURE_DIRECTORY = Path(__file__).parents[1] / "legality" / "product-sql" / "fixtures"
_COVERAGE_PATH = _FIXTURE_DIRECTORY / "d1-d8-coverage.json"
_CONFORMANCE_PATH = _FIXTURE_DIRECTORY / "generation-scoped-json-conformance.json"
_RULE_ID = "PRODUCT-SQL-V2-PROJECT-SUM-001-CANDIDATE"
_DIMENSIONS = ("D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8")
# A case's polarity is a property of the fixture, not of the row that cites it. Citing a
# passing fixture as negative evidence would otherwise satisfy every guard here.
_POSITIVE_OUTCOMES = frozenset({"equivalent_rows", "equivalent_row_set"})
_NEGATIVE_OUTCOMES = frozenset({"invalid_input", "provider_specific_rounding"})


def _resolve_repository_path(relative: str) -> Path | None:
    """Find a repository-relative path without assuming a fixed checkout depth.

    Mutation runs and tooling copies relocate this file, so counting parents is
    not sound. Search the ancestors instead and accept the first real file.
    """
    for ancestor in Path(__file__).resolve().parents:
        candidate = ancestor / relative
        if candidate.is_file():
            return candidate
    return None


def _load(path: Path) -> dict[str, object]:
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    assert all(isinstance(key, str) for key in loaded)
    return cast(dict[str, object], loaded)


def _coverage_rows() -> tuple[dict[str, object], ...]:
    rows = _load(_COVERAGE_PATH)["dimensions"]
    assert isinstance(rows, list)
    return tuple(row for row in rows if isinstance(row, dict))


def _conformance_cases() -> tuple[dict[str, object], ...]:
    cases = _load(_CONFORMANCE_PATH)["cases"]
    assert isinstance(cases, list)
    return tuple(case for case in cases if isinstance(case, dict))


def _string_tuple(row: dict[str, object], key: str) -> tuple[str, ...]:
    values = row.get(key, [])
    assert isinstance(values, list)
    assert all(isinstance(value, str) for value in values)
    return tuple(cast(list[str], values))


def _defined_test_names(module_path: Path) -> frozenset[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    return frozenset(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    )


def test_coverage_map_declares_every_dimension_exactly_once() -> None:
    rows = _coverage_rows()

    assert tuple(row["dimension"] for row in rows) == _DIMENSIONS
    assert all(row["title"] for row in rows)
    assert all(row["carried_by"] in {"engine_cases", "compiler_boundary"} for row in rows)


def test_every_conformance_case_is_mapped_and_every_mapped_case_exists() -> None:
    cases = _conformance_cases()
    declared_case_ids = {case["case_id"] for case in cases}

    for case in cases:
        dimensions = _string_tuple(case, "dimensions")
        assert dimensions, f"{case['case_id']} declares no dimension"
        assert set(dimensions) <= set(_DIMENSIONS)

    mapped: set[object] = set()
    for row in _coverage_rows():
        referenced = set(_string_tuple(row, "positive_cases")) | set(
            _string_tuple(row, "negative_cases")
        )
        unknown = referenced - cast(set[str], declared_case_ids)
        assert not unknown, f"{row['dimension']} references unknown cases {sorted(unknown)}"
        mapped |= referenced

    unmapped = declared_case_ids - mapped
    assert not unmapped, f"conformance cases carried by no dimension: {sorted(map(str, unmapped))}"


def test_case_dimension_tags_agree_with_the_coverage_map() -> None:
    by_dimension: dict[str, set[str]] = {dimension: set() for dimension in _DIMENSIONS}
    for case in _conformance_cases():
        case_id = case["case_id"]
        assert isinstance(case_id, str)
        for dimension in _string_tuple(case, "dimensions"):
            by_dimension[dimension].add(case_id)

    for row in _coverage_rows():
        dimension = row["dimension"]
        assert isinstance(dimension, str)
        referenced = set(_string_tuple(row, "positive_cases")) | set(
            _string_tuple(row, "negative_cases")
        )
        assert referenced == by_dimension[dimension], (
            f"{dimension} coverage map and case tags disagree: "
            f"{sorted(referenced ^ by_dimension[dimension])}"
        )


def test_every_referenced_compiler_test_exists() -> None:
    for row in _coverage_rows():
        for reference in _string_tuple(row, "compiler_tests"):
            module_reference, _, test_name = reference.partition("::")
            assert test_name, f"{reference} does not name a test"
            module_path = _resolve_repository_path(module_reference)
            assert module_path is not None, f"{reference} names a missing module"
            assert test_name in _defined_test_names(module_path), (
                f"{reference} names a test that does not exist"
            )


def test_every_dimension_carries_evidence_or_declares_why_not() -> None:
    for row in _coverage_rows():
        dimension = row["dimension"]
        positives = _string_tuple(row, "positive_cases")
        negatives = _string_tuple(row, "negative_cases")
        compiler_tests = _string_tuple(row, "compiler_tests")

        if row.get("open") is True:
            assert row.get("negative_not_applicable_reason"), (
                f"{dimension} is open and must say what is still missing"
            )
            continue

        assert positives or compiler_tests, f"{dimension} carries no positive evidence"
        if row["negatives_excluded_at_compiler_boundary"] is True:
            assert compiler_tests, (
                f"{dimension} excludes its negatives at the compiler boundary "
                "and must name the tests that prove it"
            )
        if negatives:
            continue
        assert row.get("negative_not_applicable_reason"), (
            f"{dimension} has no negative case and no reason why none applies"
        )


def test_divergence_motivated_exclusions_survive_single_engine_activation() -> None:
    exclusions = {
        row["dimension"]: _string_tuple(row, "divergence_motivated_exclusions")
        for row in _coverage_rows()
    }

    assert exclusions["D2"], "the excess-scale exclusion must stay recorded"
    assert exclusions["D7"], "the numeric-JSON exclusion must stay recorded"

    negatives = {
        row["dimension"]: set(_string_tuple(row, "negative_cases")) for row in _coverage_rows()
    }
    assert "decimal_excess_scale" in negatives["D2"]
    assert "wrong_type_decimal" in negatives["D7"]


def test_postgresql_activation_claims_nothing_about_clickhouse() -> None:
    coverage = _load(_COVERAGE_PATH)

    assert coverage["rule_id"] == _RULE_ID
    assert coverage["activation_scope"] == "postgresql"
    assert coverage["cross_engine_equivalence_claimed"] is False
    for row in _coverage_rows():
        assert row["clickhouse_status"] == "not_claimed", (
            f"{row['dimension']} claims ClickHouse without its own evidence and review"
        )


def test_unenforced_exclusions_are_exactly_the_negatives_postgresql_still_accepts() -> None:
    """Which exclusions are unenforced is derived from live results, not declared by hand.

    A negative case whose recorded PostgreSQL outcome is still "rows" is an exclusion nothing
    refuses. The pre-review found the map declared only one such case while excess-scale rounding
    was a second, undeclared one; a hand-maintained list cannot notice that. Every such case must
    be declared, nothing else may be, and a row's status must say whether it carries one.
    """
    unenforced_live = {
        cast(str, case["case_id"])
        for case in _conformance_cases()
        if case["business_outcome"] in _NEGATIVE_OUTCOMES
        and cast(dict[str, dict[str, object]], case["expected"])["postgresql"]["classification"]
        == "rows"
    }
    declared: set[str] = set()
    for row in _coverage_rows():
        entries = row.get("unenforced_exclusions") or []
        assert isinstance(entries, list)
        for entry in entries:
            assert isinstance(entry, dict)
            assert "enforced_by" in entry and entry["enforced_by"] is None, row["dimension"]
            assert entry.get("live_evidence"), row["dimension"]
            declared.add(cast(str, entry["case_id"]))
        expected_status = "claimed_with_unenforced_exclusion" if entries else "claimed"
        assert row["postgresql_status"] == expected_status, row["dimension"]

    assert declared == unenforced_live


def test_each_cited_case_has_the_polarity_the_row_cites_it_under() -> None:
    """A row must cite passing fixtures as positives and refused ones as negatives.

    The map/tag agreement test compares only the union of the two lists, so without this
    check a dimension could move its one passing fixture into `negative_cases` and still
    satisfy every guard while citing no positive evidence at all.
    """
    outcomes: dict[str, str] = {}
    for case in _conformance_cases():
        case_id = case["case_id"]
        outcome = case["business_outcome"]
        assert isinstance(case_id, str)
        assert isinstance(outcome, str)
        assert outcome in _POSITIVE_OUTCOMES | _NEGATIVE_OUTCOMES, (
            f"{case_id} declares an unclassified business outcome {outcome!r}"
        )
        outcomes[case_id] = outcome

    for row in _coverage_rows():
        dimension = row["dimension"]
        for case_id in _string_tuple(row, "positive_cases"):
            assert outcomes[case_id] in _POSITIVE_OUTCOMES, (
                f"{dimension} cites {case_id} as positive evidence, but the fixture records "
                f"it as {outcomes[case_id]}"
            )
        for case_id in _string_tuple(row, "negative_cases"):
            assert outcomes[case_id] in _NEGATIVE_OUTCOMES, (
                f"{dimension} cites {case_id} as negative evidence, but the fixture records "
                f"it as {outcomes[case_id]}"
            )
