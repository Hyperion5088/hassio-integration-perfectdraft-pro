"""Tests for the PerfectDraft Taproom config flow."""

import logging
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.perfectdraft.const import (
    CONF_ACCESS_TOKEN,
    CONF_ID_TOKEN,
    CONF_MACHINE_ID,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.perfectdraft.exceptions import AuthenticationError

CONF_RECAPTCHA_TOKEN = "recaptcha_token"
EMAIL = "user@example.com"
PASSWORD = "test-password"
RECAPTCHA_TOKEN = "test-recaptcha-token"
OLD_ENTRY_DATA = {
    CONF_EMAIL: EMAIL,
    CONF_ACCESS_TOKEN: "old-access-token",
    CONF_ID_TOKEN: "old-id-token",
    CONF_REFRESH_TOKEN: "old-refresh-token",
    CONF_MACHINE_ID: "old-machine-id",
    "unrelated": "preserve-me",
}
NEW_TOKENS = {
    CONF_ACCESS_TOKEN: "new-access-token",
    CONF_ID_TOKEN: "new-id-token",
    CONF_REFRESH_TOKEN: "new-refresh-token",
}


@pytest.fixture
def mock_api_client() -> Generator[MagicMock]:
    """Mock successful PerfectDraft authentication and profile retrieval."""
    client = MagicMock()
    client.authenticate = AsyncMock()
    client.get_user_profile = AsyncMock(
        return_value={
            CONF_EMAIL: EMAIL,
            "perfectdraftMachines": [{"id": "new-machine-id"}],
        }
    )
    client.access_token = NEW_TOKENS[CONF_ACCESS_TOKEN]
    client.id_token = NEW_TOKENS[CONF_ID_TOKEN]
    client.refresh_token = NEW_TOKENS[CONF_REFRESH_TOKEN]

    with (
        patch(
            "custom_components.perfectdraft.config_flow.PerfectDraftApiClient",
            return_value=client,
        ),
        patch(
            "custom_components.perfectdraft.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ),
    ):
        yield client


async def _advance_to_token_step(
    hass: HomeAssistant,
    *,
    source: str,
    entry: MockConfigEntry | None = None,
    email: str = EMAIL,
) -> dict:
    """Start a user or reauth flow and advance it to the token form."""
    context = {"source": source}
    data = None
    if entry is not None:
        context["entry_id"] = entry.entry_id
        data = entry.data

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context=context,
        data=data,
    )
    expected_step = "reauth_confirm" if source == SOURCE_REAUTH else "user"
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == expected_step

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: email, CONF_PASSWORD: PASSWORD},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "token"
    return result


async def _submit_token(hass: HomeAssistant, flow_id: str) -> dict:
    """Submit the verification token to an active config flow."""
    return await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_RECAPTCHA_TOKEN: RECAPTCHA_TOKEN},
    )


async def test_successful_initial_setup_creates_one_entry(
    hass: HomeAssistant,
    mock_api_client: MagicMock,
) -> None:
    """Successful initial setup still creates exactly one safe entry."""
    result = await _advance_to_token_step(hass, source=SOURCE_USER)
    result = await _submit_token(hass, result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"PerfectDraft Taproom ({EMAIL})"
    assert result["data"] == {
        CONF_EMAIL: EMAIL,
        **NEW_TOKENS,
        CONF_MACHINE_ID: "new-machine-id",
    }
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert CONF_PASSWORD not in result["data"]
    assert CONF_RECAPTCHA_TOKEN not in result["data"]
    mock_api_client.authenticate.assert_awaited_once_with(
        EMAIL,
        PASSWORD,
        RECAPTCHA_TOKEN,
    )


async def test_successful_reauth_updates_and_reloads_existing_entry(
    hass: HomeAssistant,
    mock_api_client: MagicMock,
) -> None:
    """Reauth replaces credentials without creating a duplicate entry."""
    options = {CONF_SCAN_INTERVAL: 300}
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"PerfectDraft Taproom ({EMAIL})",
        unique_id=EMAIL,
        data=OLD_ENTRY_DATA,
        options=options,
    )
    entry.add_to_hass(hass)

    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        result = await _advance_to_token_step(
            hass,
            source=SOURCE_REAUTH,
            entry=entry,
        )
        result = await _submit_token(hass, result["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert result["reason"] != "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert entry.data == {
        CONF_EMAIL: EMAIL,
        **NEW_TOKENS,
        CONF_MACHINE_ID: "new-machine-id",
        "unrelated": "preserve-me",
    }
    assert entry.options == options
    assert entry.unique_id == EMAIL
    assert CONF_PASSWORD not in entry.data
    assert CONF_RECAPTCHA_TOKEN not in entry.data
    reload_entry.assert_called_once_with(entry.entry_id)


@pytest.mark.parametrize(
    "failure_message",
    [
        pytest.param("Incorrect credentials", id="incorrect-credentials"),
        pytest.param("Expired reCAPTCHA token", id="expired-recaptcha"),
    ],
)
async def test_invalid_auth_stays_on_token_form_without_leaking_secrets(
    hass: HomeAssistant,
    mock_api_client: MagicMock,
    caplog: pytest.LogCaptureFixture,
    failure_message: str,
) -> None:
    """Credential and reCAPTCHA failures remain retryable and secret-safe."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=EMAIL,
        data=OLD_ENTRY_DATA,
    )
    entry.add_to_hass(hass)
    mock_api_client.authenticate.side_effect = AuthenticationError(
        f"{failure_message}: {PASSWORD} {RECAPTCHA_TOKEN}"
    )

    result = await _advance_to_token_step(
        hass,
        source=SOURCE_REAUTH,
        entry=entry,
    )
    with caplog.at_level(logging.ERROR):
        result = await _submit_token(hass, result["flow_id"])

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "token"
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data == OLD_ENTRY_DATA
    assert PASSWORD not in caplog.text
    assert RECAPTCHA_TOKEN not in caplog.text


async def test_reauth_rejects_different_account_without_changes(
    hass: HomeAssistant,
    mock_api_client: MagicMock,
) -> None:
    """Reauth cannot replace an entry with a different account identity."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=EMAIL,
        data=OLD_ENTRY_DATA,
        options={CONF_SCAN_INTERVAL: 300},
    )
    entry.add_to_hass(hass)
    mock_api_client.get_user_profile.return_value = {
        CONF_EMAIL: "different@example.com",
        "perfectdraftMachines": [{"id": "different-machine-id"}],
    }

    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        result = await _advance_to_token_step(
            hass,
            source=SOURCE_REAUTH,
            entry=entry,
        )
        result = await _submit_token(hass, result["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data == OLD_ENTRY_DATA
    assert entry.options == {CONF_SCAN_INTERVAL: 300}
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    reload_entry.assert_not_called()
