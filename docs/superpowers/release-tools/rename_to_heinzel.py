#!/usr/bin/env python3
"""One-off: move the workspace to the Heinzel namespace and retire internal milestone names.

Run from the repository root on a clean working tree (``--dry-run`` first). It moves files with
``git mv`` and rewrites tracked text files in place; review the result with ``git diff --stat``
before committing. Every check runs before the first change: a dirty tree, a path collision, or a
tracked text file that is not valid UTF-8 stops the run with a non-zero exit and nothing moved.

Allowed company references (the exact forms the release scanner permits, plus ``pillarmesh.com``
wherever it appears) are masked with placeholder tokens before the renames and restored after,
so they survive byte for byte. ``github.com/PillarMesh/`` protects only the organisation prefix:
``github.com/PillarMesh/pillarmesh`` becomes ``github.com/PillarMesh/heinzel``.

Paths in ``EXCLUDED_PATHS`` are never moved or rewritten.

Lockfiles:

* ``uv.lock`` is excluded and must be regenerated with ``uv lock`` after the run. Its package
  entries are sorted by name, so a textual ``pillarmesh-*`` -> ``heinzel-*`` rewrite would leave
  them out of order and ``uv lock --check`` would reject the file anyway.
* ``apps/console/package-lock.json`` is rewritten in place, not regenerated. Its only company
  references are the two root ``"name": "@pillarmesh/console"`` fields, which carry no integrity
  hash and no ordering constraint. An in-place rewrite is offline and changes exactly those two
  fields, whereas ``npm install --package-lock-only`` contacts the registry and can re-resolve or
  reformat unrelated entries depending on the local npm version. Verify afterwards with
  ``npm ci`` (it fails if the lockfile and ``package.json`` disagree).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

# Entries ending in "/" exclude a directory prefix; the others exclude one exact path.
EXCLUDED_PATHS: tuple[str, ...] = (
    "docs/superpowers/",
    "tests/release/",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
    "apps/console/web/public/licenses/",
    "uv.lock",
)

PATH_RENAMES: dict[str, str] = {
    "tests/acceptance/plan2_orchestration.py": (
        "tests/acceptance/semantic_formation_orchestration.py"
    ),
    "tests/acceptance/run_plan2.py": "tests/acceptance/run_semantic_formation.py",
    "tests/acceptance/test_run_plan2.py": "tests/acceptance/test_run_semantic_formation.py",
    "tests/acceptance/plan3a_fault_matrix.py": (
        "tests/acceptance/warehouse_lifecycle_fault_matrix.py"
    ),
    "tests/acceptance/plan3a_orchestration.py": (
        "tests/acceptance/warehouse_lifecycle_orchestration.py"
    ),
    "tests/acceptance/run_plan3a.py": "tests/acceptance/run_warehouse_lifecycle.py",
    "tests/acceptance/test_run_plan3a.py": "tests/acceptance/test_run_warehouse_lifecycle.py",
    "tests/acceptance/run_plan3b.py": "tests/acceptance/run_request_fulfillment.py",
    "tests/acceptance/test_run_plan3b.py": "tests/acceptance/test_run_request_fulfillment.py",
    "tests/acceptance/run_plan4a.py": "tests/acceptance/run_source_acquisition.py",
    "tests/acceptance/test_run_plan4a.py": "tests/acceptance/test_run_source_acquisition.py",
    "tests/ci/run_plan3a_witness.py": "tests/ci/run_warehouse_lifecycle_witness.py",
    "tests/ci/test_plan3a_witness_runner.py": "tests/ci/test_warehouse_lifecycle_witness_runner.py",
    "tests/end-to-end/test_plan4a_postgresql_acquisition.py": (
        "tests/end-to-end/test_postgresql_source_acquisition.py"
    ),
    "services/compiler/legality/rules/M0-PG-SNAPSHOT-SNOWFLAKE-001.json": (
        "services/compiler/legality/rules/SNAPSHOT-POSTGRESQL-SNOWFLAKE-001.json"
    ),
    # The rule id is renamed in text, so its proof note and review record must move with it or
    # the proof note's relative link to the review breaks.
    "services/compiler/legality/proof-notes/M0-PG-SNAPSHOT-SNOWFLAKE-001.md": (
        "services/compiler/legality/proof-notes/SNAPSHOT-POSTGRESQL-SNOWFLAKE-001.md"
    ),
    "services/compiler/legality/reviews/M0-PG-SNAPSHOT-SNOWFLAKE-001-review.md": (
        "services/compiler/legality/reviews/SNAPSHOT-POSTGRESQL-SNOWFLAKE-001-review.md"
    ),
}

# Whole milestone-named directories. Every tracked file under the old prefix moves.
DIRECTORY_RENAMES: dict[str, str] = {
    "docs/plan2/": "docs/semantic-formation/",
    "docs/plan3a/": "docs/warehouse-lifecycle/",
    "docs/plan3b/": "docs/request-fulfillment/",
}

# The descriptive name for each retired milestone, in the case styles the tree uses.
_MILESTONES: tuple[tuple[str, str, str], ...] = (
    # (lower-case milestone token, CamelCase token, snake_case descriptive name)
    ("plan2", "Plan2", "semantic_formation"),
    ("plan3a", "Plan3A", "warehouse_lifecycle"),
    ("plan3b", "Plan3B", "request_fulfillment"),
    ("plan4a", "Plan4A", "source_acquisition"),
)


def _milestone_rules() -> tuple[tuple[str, str], ...]:
    rules: list[tuple[str, str]] = []
    for lower, camel, snake in _MILESTONES:
        camel_name = "".join(word.capitalize() for word in snake.split("_"))
        kebab = snake.replace("_", "-")
        rules += [
            # Must run before the generic PILLARMESH_ rule, which would otherwise leave PLAN2.
            (f"PILLARMESH_{lower.upper()}_", f"HEINZEL_{snake.upper()}_"),
            (camel, camel_name),
            (f"{lower}_", f"{snake}_"),
            (f"_{lower}", f"_{snake}"),
            (f"{lower}-", f"{kebab}-"),
        ]
    return tuple(rules)


# Applied in order: a specific rule must precede every more general rule that also matches it.
TEXT_RENAMES: tuple[tuple[str, str], ...] = (
    ("M0-PG-SNAPSHOT-SNOWFLAKE-001", "SNAPSHOT-POSTGRESQL-SNOWFLAKE-001"),
    *((old, new) for old, new in DIRECTORY_RENAMES.items()),
    ("plan2_orchestration", "semantic_formation_orchestration"),
    ("test_run_plan2", "test_run_semantic_formation"),
    ("run_plan2", "run_semantic_formation"),
    ("plan3a_fault_matrix", "warehouse_lifecycle_fault_matrix"),
    ("plan3a_orchestration", "warehouse_lifecycle_orchestration"),
    ("test_plan3a_witness_runner", "test_warehouse_lifecycle_witness_runner"),
    ("run_plan3a_witness", "run_warehouse_lifecycle_witness"),
    ("test_run_plan3a", "test_run_warehouse_lifecycle"),
    ("run_plan3a", "run_warehouse_lifecycle"),
    ("test_run_plan3b", "test_run_request_fulfillment"),
    ("run_plan3b", "run_request_fulfillment"),
    ("test_plan4a_postgresql_acquisition", "test_postgresql_source_acquisition"),
    ("test_run_plan4a", "test_run_source_acquisition"),
    ("run_plan4a", "run_source_acquisition"),
    ("OfflinePlan4AHarness", "OfflineSourceAcquisitionHarness"),
    *_milestone_rules(),
    ("pillarmesh-m0-workspace", "heinzel-workspace"),
    # Hash domains, tags and temporary names ("pillarmesh-m0-batch-v1"): drop the milestone.
    # Without this rule the CLI rename below would turn them into "heinzel-authoring-batch-v1".
    ("pillarmesh-m0-", "heinzel-"),
    ("pillarmesh-m0", "heinzel-authoring"),
    ("PILLARMESH_", "HEINZEL_"),
    ("pillarmesh_", "heinzel_"),
    ("@pillarmesh/", "@heinzel/"),
    ("X-PillarMesh-", "X-Heinzel-"),
    ("pillarmesh:record", "heinzel:record"),
    ("pillarmesh-", "heinzel-"),
    ("PillarMesh", "Heinzel"),
    ("Pillarmesh", "Heinzel"),
    ("PILLARMESH", "HEINZEL"),
    ("pillarmesh", "heinzel"),
)

# The company name may survive only in these forms. They mirror the release scanner's allowed
# references (tests/release/test_public_tree.py), except that the domain is protected wherever
# it appears so that no real URL or address is ever rewritten to a domain PillarMesh does not own.
PROTECTED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'"name":\s*"PillarMesh"'),
    re.compile(r'(?<![\w.-])name\s*=\s*"PillarMesh"'),
    re.compile(r"Copyright 2026 PillarMesh"),
    re.compile(r"a product of PillarMesh"),
    re.compile(r"github\.com/PillarMesh/"),
    re.compile(r"ghcr\.io/pillarmesh/"),
    re.compile(r"(?i)pillarmesh\.com(?![a-z0-9-])"),
)

# Placeholders use NUL, which no rewritten file may contain (NUL marks a file as binary), and
# digits, which no rename rule matches.
_PLACEHOLDER = re.compile(r"\x00(\d+)\x00")

_COMPANY_PATTERN = re.compile(r"(?i)pillar[\s_.-]*mesh")
_SCANNER_ALLOWED = (
    re.compile(r"Copyright 2026 PillarMesh"),
    re.compile(r"github\.com/PillarMesh/"),
    re.compile(r"ghcr\.io/pillarmesh/"),
    re.compile(r"(?i)(?<![a-z0-9.-])(?:www\.)?pillarmesh\.com(?![a-z0-9-]|\.[a-z0-9])"),
    re.compile(r"a product of PillarMesh"),
    re.compile(r'"name":\s*"PillarMesh"'),
    re.compile(r'(?<![\w.-])name\s*=\s*"PillarMesh"'),
)
_MILESTONE_PATTERNS = (
    re.compile(r"(?i)(?<![a-z0-9])plan[\s_-]?[234][ab]?(?![0-9])"),
    re.compile(r"(?i)(?<![a-z0-9])m[0-8](?![0-9a-z])"),
    re.compile(r"(?:Plan[234][AB]?|plan[234][ab]?)"),
)


class RenameError(Exception):
    """The repository is not in a state the rename can safely run against."""


@dataclass(frozen=True)
class Move:
    old: str
    new: str
    reason: str  # "milestone" (PATH_RENAMES / DIRECTORY_RENAMES) or "namespace"


@dataclass
class RenamePlan:
    moves: list[Move] = field(default_factory=list)
    rewrites: dict[str, str] = field(default_factory=dict)  # old path -> new text
    rule_replacements: Counter[tuple[str, str]] = field(default_factory=Counter)
    rule_files: Counter[tuple[str, str]] = field(default_factory=Counter)
    protected_spans: Counter[str] = field(default_factory=Counter)
    missing_sources: list[str] = field(default_factory=list)
    binary_mentions: list[str] = field(default_factory=list)
    residual_company: Counter[str] = field(default_factory=Counter)
    residual_milestones: Counter[str] = field(default_factory=Counter)
    residual_milestone_files: set[str] = field(default_factory=set)


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
    )
    return os.fsdecode(completed.stdout)


def _tracked(root: Path) -> tuple[str, ...]:
    output = _git(root, "ls-files", "-z")
    return tuple(path for path in output.split("\0") if path)


def is_excluded(path: str) -> bool:
    return any(
        path.startswith(entry) if entry.endswith("/") else path == entry for entry in EXCLUDED_PATHS
    )


def require_clean_tree(root: Path) -> None:
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    if status.strip():
        raise RenameError("working tree is not clean; commit or stash first:\n" + status)


def rename_text(
    text: str,
    *,
    replacements: Counter[tuple[str, str]] | None = None,
    protected: Counter[str] | None = None,
) -> str:
    """Apply TEXT_RENAMES in order while leaving every protected span byte-for-byte intact."""
    if "\x00" in text:
        raise RenameError("text contains NUL, which is reserved for placeholders")
    spans: list[str] = []

    def _mask(match: re.Match[str]) -> str:
        spans.append(match.group(0))
        if protected is not None:
            protected[match.re.pattern] += 1
        return f"\x00{len(spans) - 1}\x00"

    masked = text
    for pattern in PROTECTED_PATTERNS:
        masked = pattern.sub(_mask, masked)
    for old, new in TEXT_RENAMES:
        occurrences = masked.count(old)
        if occurrences:
            masked = masked.replace(old, new)
            if replacements is not None:
                replacements[(old, new)] += occurrences
    restored = _PLACEHOLDER.sub(lambda match: spans[int(match.group(1))], masked)
    if "\x00" in restored:
        raise RenameError("a protected-span placeholder survived the rename")
    return restored


def rename_path(path: str) -> tuple[str, str | None]:
    """Return the new path and the reason it moved, or (path, None) when it stays."""
    new_path = PATH_RENAMES.get(path, path)
    reason: str | None = "milestone" if new_path != path else None
    if reason is None:
        for old_prefix, new_prefix in DIRECTORY_RENAMES.items():
            if path.startswith(old_prefix):
                new_path = new_prefix + path[len(old_prefix) :]
                reason = "milestone"
                break
    components = new_path.split("/")
    renamed = [
        rename_text(component) if "pillarmesh" in component.lower() else component
        for component in components
    ]
    if renamed != components:
        new_path = "/".join(renamed)
        reason = reason or "namespace"
    return new_path, reason


def _residuals(plan: RenamePlan, path: str, text: str) -> None:
    for line in text.splitlines():
        cleaned = line
        for pattern in _SCANNER_ALLOWED:
            cleaned = pattern.sub("", cleaned)
        for match in _COMPANY_PATTERN.finditer(cleaned):
            plan.residual_company[f"{path}: {match.group(0)}"] += 1
        tokens = {
            match.group(0) for pattern in _MILESTONE_PATTERNS for match in pattern.finditer(line)
        }
        for token in tokens:
            plan.residual_milestones[token] += 1
            plan.residual_milestone_files.add(path)


def build_plan(root: Path) -> RenamePlan:
    """Compute every move and rewrite without changing anything; raise on any unsafe input."""
    tracked = _tracked(root)
    tracked_set = set(tracked)
    plan = RenamePlan()
    plan.missing_sources = sorted(source for source in PATH_RENAMES if source not in tracked_set)
    plan.missing_sources += sorted(
        prefix
        for prefix in DIRECTORY_RENAMES
        if not any(path.startswith(prefix) for path in tracked)
    )

    errors: list[str] = []
    for path in tracked:
        if is_excluded(path):
            continue
        new_path, reason = rename_path(path)
        if reason is not None:
            plan.moves.append(Move(path, new_path, reason))

        full_path = root / path
        if full_path.is_symlink() or not full_path.is_file():
            continue
        data = full_path.read_bytes()
        if b"\x00" in data:
            if re.search(rb"(?i)pillar[\s_.-]*mesh", data):
                plan.binary_mentions.append(path)
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            errors.append(f"{path}: not valid UTF-8 ({error.reason} at byte {error.start})")
            continue
        file_replacements: Counter[tuple[str, str]] = Counter()
        new_text = rename_text(text, replacements=file_replacements, protected=plan.protected_spans)
        plan.rule_replacements.update(file_replacements)
        plan.rule_files.update(file_replacements.keys())
        if new_text != text:
            plan.rewrites[path] = new_text
        _residuals(plan, new_path, new_text)

    targets: dict[str, str] = {}
    moving = {move.old for move in plan.moves}
    for move in plan.moves:
        if move.new in targets:
            errors.append(f"collision: {targets[move.new]} and {move.old} both map to {move.new}")
        targets[move.new] = move.old
        if (move.new in tracked_set and move.new not in moving) or (
            move.new not in tracked_set and (root / move.new).exists()
        ):
            errors.append(f"collision: {move.old} -> {move.new}, which already exists")
        for parent in Path(move.new).parents:
            if str(parent) != "." and (root / parent).is_file():
                errors.append(f"collision: {move.new} needs directory {parent}, which is a file")
    if errors:
        raise RenameError("refusing to rename:\n  " + "\n  ".join(errors))
    return plan


def apply_plan(root: Path, plan: RenamePlan) -> None:
    new_path_of = {move.old: move.new for move in plan.moves}
    for move in plan.moves:
        (root / move.new).parent.mkdir(parents=True, exist_ok=True)
        _git(root, "mv", "--", move.old, move.new)
        _remove_empty_parents(root, Path(move.old).parent)
    for old_path, text in plan.rewrites.items():
        (root / new_path_of.get(old_path, old_path)).write_bytes(text.encode("utf-8"))


def _remove_empty_parents(root: Path, directory: Path) -> None:
    while str(directory) != ".":
        full = root / directory
        if not full.is_dir() or any(full.iterdir()):
            return
        full.rmdir()
        directory = directory.parent


def report(plan: RenamePlan, *, list_moves: bool) -> str:
    milestone = sum(1 for move in plan.moves if move.reason == "milestone")
    lines = [
        f"Path moves: {len(plan.moves)} "
        f"(namespace: {len(plan.moves) - milestone}, milestone: {milestone})",
    ]
    if list_moves:
        lines += [f"  {move.old} -> {move.new}" for move in plan.moves]
    lines.append("Collisions: none")
    lines.append(
        "Missing PATH_RENAMES/DIRECTORY_RENAMES sources: "
        + (", ".join(plan.missing_sources) if plan.missing_sources else "none")
    )
    lines.append(f"Files to rewrite: {len(plan.rewrites)}")
    lines.append("Replacements per rule (replacements, files, rule):")
    for (old, new), count in plan.rule_replacements.most_common():
        lines.append(f"  {count:6d} {plan.rule_files[(old, new)]:5d}  {old!r} -> {new!r}")
    lines.append("Protected spans kept:")
    for pattern, count in plan.protected_spans.most_common():
        lines.append(f"  {count:6d}  {pattern}")
    lines.append(
        "Binary files naming the company (not rewritten): "
        + (", ".join(plan.binary_mentions) if plan.binary_mentions else "none")
    )
    lines.append(
        f"Residual company-name occurrences outside allowed forms: "
        f"{sum(plan.residual_company.values())}"
    )
    lines += [f"  {count:4d}  {where}" for where, count in sorted(plan.residual_company.items())]
    lines.append(
        f"Residual milestone tokens (lines, by token) in "
        f"{len(plan.residual_milestone_files)} files:"
    )
    lines += [f"  {count:5d}  {token!r}" for token, count in plan.residual_milestones.most_common()]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--dry-run", action="store_true", help="print the plan and change nothing")
    arguments = parser.parse_args(argv)
    repository = (root or Path.cwd()).resolve()
    try:
        require_clean_tree(repository)
        plan = build_plan(repository)
    except RenameError as error:
        print(f"rename_to_heinzel: {error}", file=sys.stderr)
        return 1
    if arguments.dry_run:
        print(report(plan, list_moves=True))
        return 0
    apply_plan(repository, plan)
    print(report(plan, list_moves=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
