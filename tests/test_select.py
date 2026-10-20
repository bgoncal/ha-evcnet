"""Tests for the EVC-net charging card select."""

from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.evcnet.api import EvcNetConnectionError
from homeassistant.components.select import ATTR_OPTIONS
from homeassistant.core import HomeAssistant, State

from .conftest import CHARGE_POINT_ID

ENTITY_ID = f"select.charge_point_{CHARGE_POINT_ID}_charging_card"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_cards_listed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """Test the select lists the account's cards and defaults to the first."""
    await _setup(hass, mock_config_entry)

    state = hass.states.get(ENTITY_ID)
    assert state.state == "NL-ABC-C12345678-X"
    assert state.attributes[ATTR_OPTIONS] == [
        "NL-ABC-C12345678-X",
        "NL-XYZ-C87654321-Y",
    ]


async def test_selection_survives_restart(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """Test the previously picked card is restored."""
    mock_restore_cache(hass, [State(ENTITY_ID, "NL-XYZ-C87654321-Y")])

    await _setup(hass, mock_config_entry)

    assert hass.states.get(ENTITY_ID).state == "NL-XYZ-C87654321-Y"


async def test_cards_unreadable(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """Test setup still works when the Cards page cannot be read."""
    mock_client.cards.side_effect = EvcNetConnectionError("HTTP 500")

    await _setup(hass, mock_config_entry)

    assert mock_config_entry.runtime_data.cards == []
    assert hass.states.get(ENTITY_ID) is None
