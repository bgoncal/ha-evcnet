"""Data update coordinator for EVC-net."""

from dataclasses import replace
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    Card,
    ChargePoint,
    ChargePointStatus,
    EvcNetAuthError,
    EvcNetClient,
    EvcNetError,
)
from .const import (
    CONF_COOKIES,
    DOMAIN,
    LOGGER,
    SCAN_INTERVAL,
    TRANSACTIONS_REFRESH_INTERVAL,
)

type EvcNetConfigEntry = ConfigEntry[EvcNetCoordinator]


class EvcNetCoordinator(DataUpdateCoordinator[dict[str, ChargePointStatus]]):
    """Polls the real-time dashboard data of every charge point on the account.

    The lifetime energy total is the sum of the completed sessions in the
    portal's history plus the energy of any session in progress, so it moves
    in 0.01 kWh steps while charging. The history is only re-read when a
    session starts or ends and every few minutes.
    """

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
        self.cards: list[Card] = []
        self.selected_card: Card | None = None
        self._completed_kwh: dict[str, float] = {}
        self._history_read_at: datetime | None = None
        self._in_session: dict[str, bool] = {}
        self._total_kwh: dict[str, float] = {}

    async def _async_setup(self) -> None:
        try:
            charge_points = await self.client.charge_points()
        except EvcNetAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except EvcNetError as err:
            raise UpdateFailed(str(err)) from err
        self.charge_points = {cp.id: cp for cp in charge_points}
        try:
            self.cards = await self.client.cards()
        except EvcNetError as err:
            # Only remote start needs a card; don't fail the whole setup.
            LOGGER.warning("Could not read the charging cards: %s", err)
        self.selected_card = next(iter(self.cards), None)
        self._save_cookies()

    async def _async_update_data(self) -> dict[str, ChargePointStatus]:
        try:
            statuses = await self.client.status(self.charge_points)
            if self._history_outdated(statuses):
                await self._read_history()
        except EvcNetAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except EvcNetError as err:
            raise UpdateFailed(str(err)) from err
        self._save_cookies()
        return {
            cp_id: replace(status, total_energy_kwh=self._total_energy(cp_id, status))
            for cp_id, status in statuses.items()
        }

    def _history_outdated(self, statuses: dict[str, ChargePointStatus]) -> bool:
        in_session = {
            cp_id: any(ch.in_transaction for ch in status.channels.values())
            for cp_id, status in statuses.items()
        }
        session_changed = in_session != self._in_session
        self._in_session = in_session
        return (
            session_changed
            or self._history_read_at is None
            or dt_util.utcnow() - self._history_read_at >= TRANSACTIONS_REFRESH_INTERVAL
        )

    async def _read_history(self) -> None:
        history = await self.client.transactions(self.charge_points)
        self._completed_kwh = {
            cp_id: sum(
                tx.energy_kwh or 0.0 for tx in transactions if tx.end is not None
            )
            for cp_id, transactions in history.items()
        }
        self._history_read_at = dt_util.utcnow()

    def _total_energy(self, cp_id: str, status: ChargePointStatus) -> float | None:
        if (completed := self._completed_kwh.get(cp_id)) is None:
            return None
        in_progress = sum(
            ch.session_energy_kwh or 0.0
            for ch in status.channels.values()
            if ch.in_transaction
        )
        # A session that just ended may not be in the history yet; dropping
        # back would show up as negative energy in the Energy dashboard.
        total = max(round(completed + in_progress, 3), self._total_kwh.get(cp_id, 0.0))
        self._total_kwh[cp_id] = total
        return total

    def _save_cookies(self) -> None:
        """Keep the portal session across restarts."""
        if (cookies := self.client.cookies) != self.config_entry.data.get(CONF_COOKIES):
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_COOKIES: cookies},
            )
