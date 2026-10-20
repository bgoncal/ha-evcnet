"""Data update coordinator for EVC-net."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ChargePoint,
    ChargePointStatus,
    EvcNetAuthError,
    EvcNetClient,
    EvcNetError,
)
from .const import CONF_COOKIES, DOMAIN, LOGGER, SCAN_INTERVAL

type EvcNetConfigEntry = ConfigEntry[EvcNetCoordinator]


class EvcNetCoordinator(DataUpdateCoordinator[dict[str, ChargePointStatus]]):
    """Polls the real-time dashboard data of every charge point on the account."""

    config_entry: EvcNetConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: EvcNetConfigEntry, client: EvcNetClient
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.client = client
        self.charge_points: dict[str, ChargePoint] = {}

    async def _async_setup(self) -> None:
        try:
            charge_points = await self.client.charge_points()
        except EvcNetAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except EvcNetError as err:
            raise UpdateFailed(str(err)) from err
        self.charge_points = {cp.id: cp for cp in charge_points}
        self._save_cookies()

    async def _async_update_data(self) -> dict[str, ChargePointStatus]:
        try:
            data = await self.client.status(self.charge_points)
        except EvcNetAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except EvcNetError as err:
            raise UpdateFailed(str(err)) from err
        self._save_cookies()
        return data

    def _save_cookies(self) -> None:
        """Keep the portal session across restarts."""
        if (cookies := self.client.cookies) != self.config_entry.data.get(CONF_COOKIES):
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_COOKIES: cookies},
            )
