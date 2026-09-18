from __future__ import annotations

import pytest
from heinzel_context_exposure.app import (
    AgentInterfaceConfigurationError,
    build_production_application,
)
from heinzel_context_exposure.settings import AppSettings
from pydantic import ValidationError


def _settings() -> AppSettings:
    return AppSettings.model_validate(
        {
            "tenant_id": "tenant-a",
            "delegation_id": "delegation-1",
            "principal_ref": "principal:requester-a",
            "agent_client_ref": "agent-client:assistant-a",
            "purpose": "monthly revenue analysis",
            "rate_invocation_ceiling": 20,
            "rate_window_seconds": 60,
            "maximum_rate_principals": 100,
        }
    )


def test_settings_are_strict_and_bounded() -> None:
    with pytest.raises(ValidationError):
        AppSettings.model_validate({**_settings().model_dump(), "unexpected": True})
    with pytest.raises(ValidationError):
        AppSettings.model_validate({**_settings().model_dump(), "rate_invocation_ceiling": 0})


def test_production_startup_names_missing_authority_and_owner_ports() -> None:
    with pytest.raises(AgentInterfaceConfigurationError) as caught:
        build_production_application(_settings())

    assert caught.value.owner == "context-exposure deployment"
    assert caught.value.interface == "configured current-authority and owning-service ports"
