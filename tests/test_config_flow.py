"""Tests for the EVC-net config flow."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.evcnet.api import (
    EvcNetAuthError,
    EvcNetConnectionError,
    EvcNetInvalidCode,
    EvcNetTwoFactorRequired,
)
from custom_components.evcnet.const import (
    CONF_CODE,
    CONF_COOKIES,
    CONF_TWO_FACTOR,
    DOMAIN,
)
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import BASE_URL, COOKIES, PASSWORD, USERNAME

USER_INPUT = {
    CONF_URL: "50five-snl.evc-net.com/",
    CONF_USERNAME: f" {USERNAME} ",
    CONF_PASSWORD: PASSWORD,
}


async def _start(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    return result["flow_id"]


async def test_user_flow(hass: HomeAssistant, mock_client_cls: MagicMock) -> None:
    """Test a login without email verification, including URL normalisation."""
    result = await hass.config_entries.flow.async_configure(
        await _start(hass), USER_INPUT
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == USERNAME
    assert result["data"] == {
        CONF_URL: BASE_URL,
        CONF_USERNAME: USERNAME,
        CONF_PASSWORD: PASSWORD,
        CONF_COOKIES: COOKIES,
        CONF_TWO_FACTOR: False,
    }
    assert result["result"].unique_id == f"50five-snl.evc-net.com:{USERNAME}"
    assert mock_client_cls.call_args.args[1:] == (USERNAME, PASSWORD, BASE_URL)


async def test_user_flow_two_factor(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """Test a login with the emailed code, including a typo."""
    mock_client.login.side_effect = EvcNetTwoFactorRequired
    mock_client.verify_code.side_effect = [EvcNetInvalidCode, None]

    result = await hass.config_entries.flow.async_configure(
        await _start(hass), USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "two_factor"
    assert result["description_placeholders"] == {"destination": "u***@e***.com"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: "000000"}
    )
    assert result["errors"] == {"base": "invalid_code"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: "123456"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TWO_FACTOR] is True
    assert result["data"][CONF_COOKIES] == COOKIES
    mock_client.verify_code.assert_called_with("123456")


async def test_two_factor_session_ended(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """Test the flow stops when the portal drops the pending verification."""
    mock_client.login.side_effect = EvcNetTwoFactorRequired
    mock_client.verify_code.side_effect = EvcNetAuthError
    result = await hass.config_entries.flow.async_configure(
        await _start(hass), USER_INPUT
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: "123456"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "two_factor_expired"


@pytest.mark.parametrize(
    ("side_effect", "error"),
    [
        pytest.param(EvcNetAuthError, "invalid_auth", id="invalid_auth"),
        pytest.param(EvcNetConnectionError, "cannot_connect", id="cannot_connect"),
        pytest.param(RuntimeError, "unknown", id="unknown"),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant,
    mock_client: AsyncMock,
    side_effect: type[Exception],
    error: str,
) -> None:
    """Test errors are shown and the flow can recover."""
    mock_client.login.side_effect = side_effect
    flow_id = await _start(hass)
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    mock_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(flow_id, USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.usefixtures("mock_client")
async def test_already_configured(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test the same account cannot be added twice."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_configure(
        await _start(hass), USER_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("user_input", "expected_password"),
    [
        pytest.param({}, PASSWORD, id="keep_saved_password"),
        pytest.param({CONF_PASSWORD: "new-secret"}, "new-secret", id="new_password"),
    ],
)
async def test_reauth_with_code(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client_cls: MagicMock,
    user_input: dict[str, str],
    expected_password: str,
) -> None:
    """Test logging in again after the session ended."""
    mock_client = mock_client_cls.return_value
    mock_config_entry.add_to_hass(hass)
    mock_client.login.side_effect = EvcNetTwoFactorRequired
    mock_client.cookies = {"PHPSESSID": "fresh-session"}
    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["step_id"] == "two_factor"
    assert mock_client_cls.call_args.args[2] == expected_password

    mock_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: "123456"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_PASSWORD] == expected_password
    assert mock_config_entry.data[CONF_COOKIES] == {"PHPSESSID": "fresh-session"}
    assert mock_config_entry.data[CONF_TWO_FACTOR] is True


async def test_reauth_wrong_password(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """Test a rejected password during reauth."""
    mock_config_entry.add_to_hass(hass)
    mock_client.login.side_effect = EvcNetAuthError
    result = await mock_config_entry.start_reauth_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "still-wrong"}
    )

    assert result["errors"] == {"base": "invalid_auth"}
