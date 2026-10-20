"""Diagnostics for EVC-net."""

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_COOKIES
from .coordinator import EvcNetConfigEntry

TO_REDACT = {
    CONF_COOKIES,
    CONF_PASSWORD,
    CONF_USERNAME,
    "address",
    "card_id",
    "customer_name",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EvcNetConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "charge_points": async_redact_data(
            {cp_id: asdict(cp) for cp_id, cp in coordinator.charge_points.items()},
            TO_REDACT,
        ),
        "status": async_redact_data(
            {cp_id: asdict(st) for cp_id, st in (coordinator.data or {}).items()},
            TO_REDACT,
        ),
    }
