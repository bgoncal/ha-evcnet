"""Tests for setting up EVC-net and its entities."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.evcnet.api import (
    EvcNetAuthError,
    EvcNetConnectionError,
    EvcNetSessionExpired,
)
from custom_components.evcnet.const import CONF_COOKIES
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .conftest import BASE_URL, CHARGE_POINT_ID, COOKIES, PASSWORD, USERNAME

PREFIX = f"charge_point_{CHARGE_POINT_ID}"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("mock_client")
async def test_offline_charge_point(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test entities for the real "No communication" payload."""
    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(f"binary_sensor.{PREFIX}_connectivity").state == STATE_OFF
    status = hass.states.get(f"sensor.{PREFIX}_status")
    assert status.state == "offline"
    assert status.attributes["flags"] == ("no_communication",)
    assert status.attributes["status_code"] == "1000000000000000"
    assert hass.states.get(f"sensor.{PREFIX}_notification").state == "No communication"
    assert hass.states.get(f"sensor.{PREFIX}_power").state == STATE_UNKNOWN
    assert hass.states.get(f"binary_sensor.{PREFIX}_charging").state == STATE_UNKNOWN
    assert hass.states.get(f"sensor.{PREFIX}_total_energy_delivered").state == "532.0"


@pytest.mark.parametrize("overview_fixture", ["overview_charging.json"])
@pytest.mark.usefixtures("mock_client")
async def test_charging_charge_point(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test entities while charging."""
    await _setup(hass, mock_config_entry)

    assert hass.states.get(f"binary_sensor.{PREFIX}_connectivity").state == STATE_ON
    assert hass.states.get(f"sensor.{PREFIX}_status").state == "charging"
    assert hass.states.get(f"binary_sensor.{PREFIX}_charging").state == STATE_ON
    assert hass.states.get(f"binary_sensor.{PREFIX}_problem").state == STATE_OFF
    assert hass.states.get(f"sensor.{PREFIX}_power").state == "7.36"
    assert hass.states.get(f"sensor.{PREFIX}_session_energy").state == "3.21"
    assert hass.states.get(f"sensor.{PREFIX}_session_duration").state == "65"
    card = hass.states.get(f"sensor.{PREFIX}_card")
    assert card.state == "NL-ABC-C12345678-X"
    assert card.attributes["customer"] == "Jane Doe"


@pytest.mark.usefixtures("mock_client")
async def test_device(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Test the charge point device."""
    await _setup(hass, mock_config_entry)

    device = device_registry.async_get_device_by_identifier(
        ("evcnet", CHARGE_POINT_ID), mock_config_entry.entry_id
    )
    assert device is not None
    assert device.name == f"Charge point {CHARGE_POINT_ID}"
    assert device.sw_version == "1.13.0.1@2.4.6"
    assert device.configuration_url == (
        f"https://50five-snl.evc-net.com/RechargeSpots/Detail/{CHARGE_POINT_ID}"
    )


@pytest.mark.parametrize(
    ("method", "error"),
    [
        pytest.param("charge_points", EvcNetAuthError, id="password_rejected"),
        pytest.param("status", EvcNetSessionExpired, id="session_ended"),
    ],
)
async def test_auth_failure_starts_reauth(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    method: str,
    error: type[Exception],
) -> None:
    """Test a rejected login or ended session asks the user to log in again."""
    getattr(mock_client, method).side_effect = error

    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    (flow,) = hass.config_entries.flow.async_progress()
    assert flow["context"]["source"] == SOURCE_REAUTH


async def test_client_uses_saved_session(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client_cls: MagicMock
) -> None:
    """Test the saved cookies are reused and no code email is triggered."""
    await _setup(hass, mock_config_entry)

    assert mock_client_cls.call_args.args[1:] == (USERNAME, PASSWORD, BASE_URL)
    assert mock_client_cls.call_args.kwargs == {"cookies": COOKIES, "auto_login": False}


async def test_new_cookies_are_saved(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """Test a renewed session survives a restart."""
    mock_client.cookies = {"PHPSESSID": "renewed", "SERVERID": "www_2"}

    await _setup(hass, mock_config_entry)

    assert mock_config_entry.data[CONF_COOKIES] == {
        "PHPSESSID": "renewed",
        "SERVERID": "www_2",
    }


async def test_connection_failure_retries(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """Test an unreachable portal retries setup later."""
    mock_client.status.side_effect = EvcNetConnectionError

    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.usefixtures("mock_client")
async def test_unload(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """Test unloading the entry."""
    await _setup(hass, mock_config_entry)

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
