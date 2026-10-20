"""Tests for the EVC-net command buttons."""

from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.evcnet.api import (
    ChargePointAction,
    EvcNetConnectionError,
    EvcNetSessionExpired,
)
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.select import (
    DOMAIN as SELECT_DOMAIN,
    SERVICE_SELECT_OPTION,
)
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import ATTR_ENTITY_ID, ATTR_OPTION
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .conftest import CHARGE_POINT_ID

PREFIX = f"charge_point_{CHARGE_POINT_ID}"


@pytest.fixture
def overview_fixture() -> str:
    """Commands are only sent to a charger that is online."""
    return "overview_charging.json"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _press(hass: HomeAssistant, key: str) -> None:
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: f"button.{PREFIX}_{key}"},
        blocking=True,
    )


@pytest.mark.parametrize(
    ("key", "action", "kwargs"),
    [
        pytest.param("hard_reset", ChargePointAction.HARD_RESET, {}, id="hard_reset"),
        pytest.param("soft_reset", ChargePointAction.SOFT_RESET, {}, id="soft_reset"),
        pytest.param(
            "retrieve_status", ChargePointAction.GET_STATUS, {}, id="retrieve_status"
        ),
        pytest.param("block", ChargePointAction.BLOCK, {}, id="block"),
        pytest.param("unblock", ChargePointAction.UNBLOCK, {}, id="unblock"),
        pytest.param(
            "unlock_connector",
            ChargePointAction.UNLOCK_CONNECTOR,
            {"channel": "1"},
            id="unlock_connector",
        ),
        pytest.param(
            "stop_charging",
            ChargePointAction.STOP_TRANSACTION,
            {"channel": "1"},
            id="stop_charging",
        ),
        pytest.param(
            "start_charging",
            ChargePointAction.START_TRANSACTION,
            {"channel": "1", "card": "501"},
            id="start_charging_with_first_card",
        ),
    ],
)
@pytest.mark.usefixtures("mock_client")
async def test_press(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    key: str,
    action: ChargePointAction,
    kwargs: dict[str, str],
) -> None:
    """Test each button sends its portal command and refreshes the data."""
    await _setup(hass, mock_config_entry)
    mock_client = mock_config_entry.runtime_data.client
    polls = mock_client.status.call_count

    await _press(hass, key)

    mock_client.action.assert_called_once_with(CHARGE_POINT_ID, action, **kwargs)
    assert mock_client.status.call_count == polls + 1


async def test_start_charging_with_selected_card(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """Test the charging card select decides which card starts a session."""
    await _setup(hass, mock_config_entry)
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {
            ATTR_ENTITY_ID: f"select.{PREFIX}_charging_card",
            ATTR_OPTION: "NL-XYZ-C87654321-Y",
        },
        blocking=True,
    )

    await _press(hass, "start_charging")

    mock_client.action.assert_called_once_with(
        CHARGE_POINT_ID, ChargePointAction.START_TRANSACTION, channel="1", card="502"
    )


async def test_start_charging_without_cards(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """Test a clear error when the account has no usable card."""
    mock_client.cards.return_value = []
    await _setup(hass, mock_config_entry)

    with pytest.raises(HomeAssistantError, match="No charging card"):
        await _press(hass, "start_charging")
    mock_client.action.assert_not_called()


async def test_portal_error(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """Test a failed command is reported to the user."""
    await _setup(hass, mock_config_entry)
    mock_client.action.side_effect = EvcNetConnectionError("HTTP 500")

    with pytest.raises(HomeAssistantError, match="did not take"):
        await _press(hass, "retrieve_status")


async def test_session_ended(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """Test an ended session asks the user to log in again."""
    await _setup(hass, mock_config_entry)
    mock_client.action.side_effect = EvcNetSessionExpired

    with pytest.raises(HomeAssistantError, match="Log in to the portal again"):
        await _press(hass, "block")

    (flow,) = hass.config_entries.flow.async_progress()
    assert flow["context"]["source"] == SOURCE_REAUTH


@pytest.mark.parametrize("overview_fixture", ["overview_offline.json"])
async def test_offline_charger(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """Test no command is sent while the charger has no connection."""
    await _setup(hass, mock_config_entry)

    with pytest.raises(HomeAssistantError, match="no connection"):
        await _press(hass, "start_charging")
    mock_client.action.assert_not_called()
