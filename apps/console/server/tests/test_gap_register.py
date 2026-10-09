"""The gap register is held to what the console reports about itself.

`docs/demonstration-gaps.md` is maintained by hand against a product that publishes its own
capability states over HTTP, and it drifted twice in one sitting: both times a capability had
been built and the row still said it was missing. One of the two mattered -- the page claimed
dashboard publication was unwired while the recorded journey was publishing a dashboard -- and
it was found by reading the running console rather than the document.

So the document now carries a table of the states it claims, and this holds that table to the
console. It asserts both directions, because each catches a different drift: a capability the
product gained with no row is as wrong as a row naming a capability that no longer exists.

What this does not cover is worth being plain about. It checks the capability states, which are
machine-readable; it cannot check the prose, and the other of the two drifts was a prose row
about a chart. A page of claims nobody can check is still a page of claims nobody can check --
this narrows it to the part that can be.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
from heinzel_console.demo.collaborators import DEMO_ARCHITECT_ID
from heinzel_console.demo.console import DemoConsole
from starlette.testclient import TestClient

REGISTER: Final = Path(__file__).resolve().parents[4] / "docs" / "demonstration-gaps.md"
_CLAIM: Final = re.compile(
    r"^\|\s*`(?P<capability>[a-z-]+)`\s*\|\s*(?P<without>\w+)\s*\|\s*(?P<managed>\w+)\s*\|$"
)


def _claimed() -> dict[str, tuple[str, str]]:
    """The register's own table, as `{capability_id: (without a warehouse, managed)}`."""
    claims = {
        match["capability"]: (match["without"], match["managed"])
        for line in REGISTER.read_text(encoding="utf-8").splitlines()
        if (match := _CLAIM.match(line.strip()))
    }
    assert claims, f"{REGISTER} carries no capability claims to check"
    return claims


@pytest.fixture
def reported(tmp_path: Path) -> dict[str, str]:
    """What the demonstration reports with no warehouse, which is what runs offline here."""
    with DemoConsole(tmp_path / "state") as demo:
        client = TestClient(demo.build_app(origin="http://testserver"))
        response = client.get("/api/v1/workspace", headers={"x-heinzel-actor": DEMO_ARCHITECT_ID})
        assert response.status_code == 200, response.text
        return {
            capability["capability_id"]: capability["state"]
            for capability in response.json()["data"]["capabilities"]
        }


def test_the_register_claims_a_state_for_every_capability_the_console_publishes(
    reported: dict[str, str],
) -> None:
    """A capability gained without a row is a page that has stopped describing the product."""
    assert set(_claimed()) == set(reported), (
        "the gap register and the console disagree about which capabilities exist; "
        f"only the console has {sorted(set(reported) - set(_claimed()))}, "
        f"only the register has {sorted(set(_claimed()) - set(reported))}"
    )


def test_the_register_states_what_the_console_reports_without_a_warehouse(
    reported: dict[str, str],
) -> None:
    """The column this suite can reach. The managed column is asserted by the smoke test."""
    claimed = {capability: states[0] for capability, states in _claimed().items()}

    drifted = {
        capability: (claim, reported[capability])
        for capability, claim in claimed.items()
        if capability in reported and claim != reported[capability]
    }
    assert not drifted, (
        "the gap register claims a capability state the console does not report "
        f"(capability: claimed, reported): {drifted}. Correct the register, or the product -- "
        "never the register alone to make this pass."
    )


def test_the_register_claims_only_states_the_console_can_report() -> None:
    """A typo in the table would otherwise read as a capability that is simply never checked."""
    states = {state for pair in _claimed().values() for state in pair}

    assert states <= {"ready", "degraded", "blocked", "not_delivered"}, (
        f"the gap register claims states the console has no vocabulary for: {sorted(states)}"
    )


def test_the_setup_surface_refuses_entirely_without_a_warehouse(tmp_path: Path) -> None:
    """The register says so in words under the table; this is the words being true.

    There is no binding to report a stage against, so the refusal is the whole read rather than
    seven stages each reporting blocked.
    """
    with DemoConsole(tmp_path / "state") as demo:
        client = TestClient(demo.build_app(origin="http://testserver"))
        response = client.get("/api/v1/setup", headers={"x-heinzel-actor": DEMO_ARCHITECT_ID})

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "capability_not_delivered"
