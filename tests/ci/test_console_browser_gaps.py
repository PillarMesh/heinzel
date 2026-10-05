"""The browser suites' disabled tests are a closed, explained set.

`live.yml` fails its run on any skip, so a journey that does not execute is
visible there. The console job has no equivalent, and six `test.fixme` tests sat
in the suite that does run -- reported as `6 skipped, 48 passed` and read by
nobody. `AGENTS.md` is explicit that a quarantined test is a gap, not a pass.

A count in CI would be the obvious guard and is the weaker one: it says how many
tests are disabled, not which, so swapping a newly broken test for a fixed one
keeps the count and hides both. This file pins the set by name instead, and runs
in the offline suite rather than needing the browser job's output parsed.

None of these six is a broken test. Each names a capability `docs/status.md`
records as undelivered, and the registry below says which. Two tripwires keep
that honest: the capability sentence they rest on is asserted here, so
delivering it fails this file and forces the gaps to be revisited; and no other
way of disabling a test may be used, so the registry cannot be routed around.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONSOLE = ROOT / "apps/console"
STATUS = ROOT / "docs/status.md"

# Every spec directory the three browser suites run, so a gap cannot hide in a
# suite this file forgot.
_SPEC_DIRECTORIES = ("e2e", "e2e-governed", "e2e-native")

# `test.fixme("title"` and the same split across lines. The title is the first
# double-quoted argument, with escapes allowed inside it.
_FIXME = re.compile(r"test\.fixme\(\s*\"((?:[^\"\\]|\\.)*)\"")

# Every `test.fixme` call, whatever its title is quoted with. A title written with
# single quotes or a template literal is one `_FIXME` cannot read, and a registry
# that silently skips what it cannot parse is worse than no registry: the test
# disappears from the suite and from this file at once. The two counts are compared
# below so that such a call fails rather than vanishes.
_FIXME_CALL = re.compile(r"test\.fixme\(")

# The other ways a test stops running. None is in use, and a new one would take a
# test out of the suite without passing through the registry below.
_OTHER_DISABLERS = (
    "test.skip(",
    "test.describe.skip(",
    "test.describe.fixme(",
    "test.only(",
    "test.describe.only(",
)

# The sentence in `docs/status.md` that these gaps rest on. Both halves cause
# them: no identity but the architect's, and no managed warehouse binding for a
# provisioning effect to land in.
#
# This fired once already, when the demonstration gained a PostgreSQL database of
# its own and the sentence stopped saying "no warehouse". Every entry below was
# re-examined then: all six survived, because none of them rests on the absence of a
# database. Three rest on the absence of a *binding* -- `_warehouse_bindings` is
# unwired, so `_stage_states` reads no binding and the setup stage never leaves
# `foundation` -- and the wording here and in the registry now says that instead.
_CAPABILITY_CLAIM = "no authentication and no managed warehouse binding"


@dataclass(frozen=True)
class _Gap:
    spec: str
    blocked_by: str


# Each entry is a capability that is not delivered, not a test that is broken.
_REGISTRY: dict[str, _Gap] = {
    "step 8: submit a stakeholder question, answer one clarification, "
    "and accept the clarified outcome": _Gap(
        spec="e2e/requester-journey.spec.ts",
        blocked_by=(
            "No authentication. `create_app` installs a fixed `data_architect` context and "
            "nothing can change it, while every requester command authorizes `('requester',)`. "
            "The journey itself is covered by the end-to-end requester test, the requester "
            "surface component tests, and the governed browser suite."
        ),
    ),
    "step 9: the requester acceptance appears on the architect's proposal "
    "as a satisfied required authority": _Gap(
        spec="e2e/requester-journey.spec.ts",
        blocked_by=(
            "No authentication, as above, and the fixture proposal carries a `data_owner` "
            "required authority alone -- no requester authority appears in it at all, so the "
            "panel this would assert on is unreachable in fixture mode."
        ),
    ),
    "steps 3 to 6: managed services, sources, the process package, "
    "and the three approval gates": _Gap(
        spec="e2e/architect-journey.spec.ts",
        blocked_by=(
            "No managed warehouse binding. `SetupWorkbench` renders by `SetupView.active_stage`, "
            "which stays `foundation` because the demonstration composes no warehouse-control "
            "reader, so `_stage_states` sees no binding and no provisioning effect ever lands -- "
            "the sibling test `step 2 honesty` asserts exactly that, and advancing the stage "
            "would make the demonstration claim a managed effect it does not have. The database "
            "it provisions for its own product is not that binding."
        ),
    ),
    "meaning-review": _Gap(
        spec="e2e/screenshots.spec.ts",
        blocked_by=(
            "No managed warehouse binding, as above: `/reviews/review-meaning` renders the "
            "foundation stage for the same reason, so no honest image of this screen can be "
            "produced."
        ),
    ),
    "activation-review": _Gap(
        spec="e2e/screenshots.spec.ts",
        blocked_by=(
            "No managed warehouse binding, as above, and no authentication: `get_review` admits "
            "a review only to a role in its `required_authorities`, and `review-data-product` "
            "(`data_owner`) and `review-activation` (`budget_approver`) therefore answer 404."
        ),
    ),
    "keyboard-only request intake and clarified-outcome acceptance": _Gap(
        spec="e2e/accessibility.spec.ts",
        blocked_by="No authentication: both live on the requester surface. See step 8.",
    ),
}


def _spec_files() -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for directory in _SPEC_DIRECTORIES
            for path in (CONSOLE / directory).glob("*.spec.ts")
        )
    )


def _declared_fixmes() -> dict[str, str]:
    """Every `test.fixme` title in the browser suites, with the spec that holds it."""
    found: dict[str, str] = {}
    for path in _spec_files():
        text = path.read_text(encoding="utf-8")
        for title in _FIXME.findall(text):
            found[title] = str(path.relative_to(CONSOLE))
    return found


def test_the_spec_files_are_all_found() -> None:
    """A glob that stops matching would turn every check below into a silent pass."""
    specs = _spec_files()
    names = {str(path.relative_to(CONSOLE)) for path in specs}

    assert len(specs) >= 6, names
    assert "e2e-native/result.spec.ts" in names
    assert "e2e-governed/unsupported-question.spec.ts" in names


def test_every_fixme_is_written_in_a_form_this_registry_can_read() -> None:
    """A gap this file cannot parse is a gap it cannot pin.

    `_FIXME` reads a double-quoted title, which is how every spec here is written.
    A single-quoted title or a template literal would leave the test disabled and
    unregistered, and no other assertion would notice -- the silent failure this
    whole file exists to prevent, reproduced inside it.
    """
    for path in _spec_files():
        text = path.read_text(encoding="utf-8")
        calls = len(_FIXME_CALL.findall(text))
        titles = len(_FIXME.findall(text))
        assert calls == titles, (
            f"{path.relative_to(CONSOLE)} has {calls} `test.fixme` calls but "
            f"{titles} readable titles. Write the title in double quotes so that it "
            "reaches _REGISTRY."
        )


def test_every_disabled_browser_test_is_registered_with_its_reason() -> None:
    declared = _declared_fixmes()

    unregistered = sorted(set(declared) - set(_REGISTRY))
    assert unregistered == [], (
        "a browser test was disabled without recording why. Add it to _REGISTRY with the "
        f"capability that blocks it, or make it run: {unregistered}"
    )

    stale = sorted(set(_REGISTRY) - set(declared))
    assert stale == [], (
        f"a registered gap is no longer disabled in the suite. Remove it from _REGISTRY: {stale}"
    )


def test_each_registered_gap_names_the_spec_that_holds_it() -> None:
    declared = _declared_fixmes()

    for title, gap in sorted(_REGISTRY.items()):
        assert declared[title] == gap.spec, title


def test_no_browser_test_is_disabled_by_another_means() -> None:
    """The registry is only a closed set while `fixme` is the only way out of the suite."""
    for path in _spec_files():
        text = path.read_text(encoding="utf-8")
        for disabler in _OTHER_DISABLERS:
            assert disabler not in text, (
                f"{path.relative_to(CONSOLE)} uses {disabler}, which takes a test out of the "
                "suite without passing through _REGISTRY"
            )


def test_every_gap_explains_itself_in_the_spec_as_well() -> None:
    """The registry is read by whoever runs CI; the comment is read by whoever opens the file."""
    for path in sorted({CONSOLE / gap.spec for gap in _REGISTRY.values()}):
        text = path.read_text(encoding="utf-8")
        assert text.count("GAP") >= sum(
            1 for gap in _REGISTRY.values() if CONSOLE / gap.spec == path
        ), f"{path.relative_to(CONSOLE)} has a disabled test with no GAP comment"


def test_delivering_the_capability_these_gaps_rest_on_fails_this_file() -> None:
    """The tripwire: these six are only acceptable while the capability is undelivered.

    `docs/status.md` calls the demonstration console a demonstration and not a
    deployment, with no authentication and no managed warehouse binding. That is why
    four of these screens have no reachable browser state and two have no second
    identity. When that stops being true the sentence changes, this assertion fails,
    and the gaps above have to be reconsidered rather than quietly outliving their
    reason.

    It has fired once, and worked: the demonstration gained a database of its own and
    the sentence stopped saying "no warehouse", which forced every entry to be
    re-read rather than left to drift behind a claim that no longer held.
    """
    assert _CAPABILITY_CLAIM in STATUS.read_text(encoding="utf-8"), (
        f"`docs/status.md` no longer says {_CAPABILITY_CLAIM!r}. If the demonstration console "
        "gained an identity or a managed warehouse binding, revisit every entry in _REGISTRY "
        "before changing this constant to match."
    )


@pytest.mark.parametrize(("title", "gap"), sorted(_REGISTRY.items()))
def test_a_registered_gap_cites_an_undelivered_capability(title: str, gap: _Gap) -> None:
    """A reason that does not name what is missing is not a reason.

    The named capability is what is checked, not the length of the prose. A minimum
    length was tried and removed: the shortest honest entry cleared it by six
    characters, so rewording a sentence would have failed this for something that
    has nothing to do with whether the reason is true.
    """
    assert gap.blocked_by.startswith(("No authentication", "No managed warehouse binding")), title
