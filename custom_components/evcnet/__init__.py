"""The EVC-net integration (50five and other EVC-net based portals)."""

import aiohttp

from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import EvcNetClient
from .const import CONF_COOKIES, CONF_TWO_FACTOR
from .coordinator import EvcNetConfigEntry, EvcNetCoordinator
from .entity import charge_point_device_info

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: EvcNetConfigEntry) -> bool:
    """Set up EVC-net from a config entry."""
    client = EvcNetClient(
        async_create_clientsession(hass, cookie_jar=aiohttp.DummyCookieJar()),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        entry.data[CONF_URL],
        cookies=entry.data.get(CONF_COOKIES),
        # Logging in again would email a code nobody asked for; reauth instead.
        auto_login=not entry.data.get(CONF_TWO_FACTOR, False),
    )
    coordinator = EvcNetCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    device_registry = dr.async_get(hass)
    for charge_point in coordinator.charge_points.values():
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            **charge_point_device_info(entry.data[CONF_URL], charge_point),
        )

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EvcNetConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
