"""Every workspace member declares the sibling packages it imports at runtime.

The whole workspace is installed into the development virtual environment, so an
import of a sibling package resolves there whether or not the importing member
declares it. Nothing in `ruff`, `mypy` or the offline suite can see the omission.
The quickstart image is where it surfaces: it runs
`uv sync --locked --no-dev --package heinzel-console`, which installs that member
and its declared dependencies alone, and the undeclared import then fails at
startup -- in the one build no gate exercises.

An import reached only for type checking is deliberately not a runtime dependency:
it costs an install that the running code never needs. Everything else is, so
`if TYPE_CHECKING:` blocks are exempt and nothing else is -- a function body
included. Deferring an import does not remove the dependency, it moves the
`ImportError` from start-up to whenever that function is first called, which is
the worse of the two places to find out.
"""

from __future__ import annotations

import ast
import tomllib
from collections.abc import Iterable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# The directories a workspace member can sit in, as `pyproject.toml` lists them.
_MEMBER_GLOBS = (
    "packages/*/pyproject.toml",
    "providers/*/pyproject.toml",
    "services/*/pyproject.toml",
    "apps/*/*/pyproject.toml",
)

# A dependency entry down to its distribution name: `heinzel-runtime>=1,<2` and
# `heinzel-runtime[extra]` both name `heinzel-runtime`.
_SEPARATORS = ("[", "<", ">", "=", "!", "~", ";", " ")


def _distribution(entry: str) -> str:
    """The distribution a `project.dependencies` entry names."""
    cut = len(entry)
    for separator in _SEPARATORS:
        position = entry.find(separator)
        if position != -1:
            cut = min(cut, position)
    return entry[:cut].strip()


# The fields any compound statement carries its own statement lists under. `handlers`
# (`try`) and `cases` (`match`) hold clause objects rather than statements and are
# followed separately.
_BODY_FIELDS = ("body", "orelse", "finalbody")


def _guards_type_checking(test: ast.expr) -> bool:
    """Whether an `if` test is the `TYPE_CHECKING` guard.

    Both spellings the standard library sanctions are recognised. Any other test --
    a negation, a conjunction, a version comparison -- is not a guard this file
    honours, so the imports beneath it count as runtime imports. That is the safe
    direction: it can ask for a declaration that is not strictly needed, never miss
    one that is.
    """
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _runtime_imports(source: str) -> frozenset[str]:
    """The top-level packages a module imports to run, whenever it reaches the import.

    A `TYPE_CHECKING` block is left out, because nothing there executes; a relative
    import names no distribution and is left out too. A function body is counted: the
    import runs when the function is called, and a dependency that fails on the first
    call rather than at start-up is still a dependency -- found later and further from
    the manifest that omitted it.

    Everything else is descended into by field name rather than by statement type, so
    that a kind this file never thought of -- a module-level `for`, `while` or `match`,
    or a class body -- still has its imports counted. Enumerating the compound
    statements instead would make each one this file forgot a silent gap, which is the
    one failure this check cannot afford.
    """
    found: set[str] = set()

    def visit(body: Iterable[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module is not None:
                    found.add(node.module.split(".")[0])
            elif isinstance(node, ast.If) and _guards_type_checking(node.test):
                visit(node.orelse)
            else:
                for field in _BODY_FIELDS:
                    visit(getattr(node, field, ()))
                for handler in getattr(node, "handlers", ()):
                    visit(handler.body)
                for case in getattr(node, "cases", ()):
                    visit(case.body)

    visit(ast.parse(source).body)
    return frozenset(found)


def _members() -> dict[str, tuple[Path, frozenset[str]]]:
    """Each workspace member by distribution name, with its directory and declared dependencies."""
    members: dict[str, tuple[Path, frozenset[str]]] = {}
    for pattern in _MEMBER_GLOBS:
        for manifest in ROOT.glob(pattern):
            project = tomllib.loads(manifest.read_text(encoding="utf-8")).get("project", {})
            name = project.get("name")
            if name is None:
                continue
            declared = frozenset(_distribution(entry) for entry in project.get("dependencies", ()))
            members[str(name)] = (manifest.parent, declared)
    return members


def _owners(members: dict[str, tuple[Path, frozenset[str]]]) -> dict[str, str]:
    """The distribution that ships each importable `heinzel_*` package."""
    owners: dict[str, str] = {}
    for name, (directory, _) in members.items():
        for package in (directory / "src").glob("heinzel*"):
            if package.is_dir():
                owners[package.name] = name
    return owners


def test_the_workspace_members_are_all_found() -> None:
    """A glob that stops matching would turn every check below into a silent pass."""
    members = _members()
    assert len(members) >= 25, sorted(members)
    assert "heinzel-console" in members
    assert len(_owners(members)) == len(members), sorted(members)


def test_every_member_declares_the_sibling_packages_it_imports_at_runtime() -> None:
    members = _members()
    owners = _owners(members)
    undeclared: list[str] = []
    for name, (directory, declared) in sorted(members.items()):
        own = {package.name for package in (directory / "src").glob("heinzel*")}
        for module in sorted((directory / "src").rglob("*.py")):
            imported = _runtime_imports(module.read_text(encoding="utf-8"))
            for package in sorted(imported):
                if package in own or package not in owners:
                    continue
                if owners[package] not in declared:
                    relative = module.relative_to(ROOT)
                    undeclared.append(
                        f"{name} imports {package} at runtime ({relative}) "
                        f"without declaring {owners[package]}"
                    )
    assert undeclared == []


def test_an_undeclared_runtime_import_would_be_caught() -> None:
    """The check above is only worth its cost if an unguarded import counts."""
    assert "heinzel_compiler" in _runtime_imports("from heinzel_compiler import a\n")
    assert "heinzel_compiler" in _runtime_imports("import heinzel_compiler.b\n")
    assert "heinzel_compiler" in _runtime_imports(
        "if some_other_condition:\n    import heinzel_compiler\n"
    )


def test_a_type_checking_import_is_not_counted_as_a_runtime_dependency() -> None:
    """Members import siblings for annotations alone; that must stay free of an install."""
    guarded = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from heinzel_compiler import GovernedQueryPlan\n"
    )
    assert "heinzel_compiler" not in _runtime_imports(guarded)
    assert "typing" in _runtime_imports(guarded)
    qualified = "import typing\nif typing.TYPE_CHECKING:\n    import heinzel_compiler\n"
    assert "heinzel_compiler" not in _runtime_imports(qualified)


def test_an_import_under_any_compound_statement_is_counted() -> None:
    """A statement kind this file never enumerated must not become a silent gap.

    Each of these runs on import, so each is a real dependency. An earlier version
    descended only into `if`, `try` and `with`, and would have missed every one.
    """
    for source in (
        "for _ in range(1):\n    import heinzel_compiler\n",
        "while True:\n    import heinzel_compiler\n    break\n",
        "class Holder:\n    import heinzel_compiler\n",
        "match 1:\n    case 1:\n        import heinzel_compiler\n",
        "try:\n    pass\nfinally:\n    import heinzel_compiler\n",
        "with open('x'):\n    import heinzel_compiler\n",
        "if a:\n    pass\nelse:\n    import heinzel_compiler\n",
    ):
        assert "heinzel_compiler" in _runtime_imports(source), source


def test_an_import_inside_a_function_is_counted() -> None:
    """A deferred import is a dependency whose absence is found on the first call.

    Counted because the member really does import the sibling to do its work. An
    estimator that built a compiler model inside its one method passed every gate
    while its manifest named no compiler: installed on its own, it would have
    imported, constructed and then raised `ImportError` from the call.
    """
    for deferred in (
        "def build():\n    import heinzel_compiler\n    return heinzel_compiler\n",
        "def build():\n    from heinzel_compiler import QueryScanEstimate\n",
        "async def build():\n    import heinzel_compiler\n",
        "class Holder:\n    def build(self):\n        import heinzel_compiler\n",
    ):
        assert "heinzel_compiler" in _runtime_imports(deferred), deferred


def test_a_type_checking_import_inside_a_function_is_still_not_counted() -> None:
    """The exemption is about what executes, not about where the import is written."""
    guarded = (
        "from typing import TYPE_CHECKING\n"
        "def build():\n"
        "    if TYPE_CHECKING:\n"
        "        import heinzel_compiler\n"
    )
    assert "heinzel_compiler" not in _runtime_imports(guarded)


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("heinzel-runtime", "heinzel-runtime"),
        ("heinzel-runtime>=1,<2", "heinzel-runtime"),
        ("heinzel-runtime[extra]", "heinzel-runtime"),
        ("pydantic>=2.13.5,<3", "pydantic"),
        ("uvicorn>=0.52,<1", "uvicorn"),
    ],
)
def test_a_dependency_entry_resolves_to_its_distribution(entry: str, expected: str) -> None:
    assert _distribution(entry) == expected
