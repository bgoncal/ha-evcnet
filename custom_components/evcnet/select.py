"""Choice of the charging card used to start a session remotely."""

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import EvcNetConfigEntry, EvcNetCoordinator
from .entity import EvcNetChargePointEntity

PARALLEL_UPDATES = 0

CARD_SELECT = SelectEntityDescription(
    key="charging_card",
    translation_key="charging_card",
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EvcNetConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the charging card select when the account has cards."""
    coordinator = entry.runtime_data
    if not coordinator.cards:
        return
    async_add_entities(
        EvcNetCardSelect(coordinator, CARD_SELECT, charge_point_id)
        for charge_point_id in coordinator.charge_points
    )


class EvcNetCardSelect(EvcNetChargePointEntity, SelectEntity, RestoreEntity):
    """The card that the Start charging button uses."""

    def __init__(
        self,
        coordinator: EvcNetCoordinator,
        description: SelectEntityDescription,
        charge_point_id: str,
    ) -> None:
        """Initialize the select."""
        super().__init__(coordinator, description, charge_point_id)
        self._attr_options = [card.number for card in coordinator.cards]

    async def async_added_to_hass(self) -> None:
        """Restore the card picked before a restart."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._select(last.state)

    @property
    def current_option(self) -> str | None:
        """Return the selected card."""
        card = self.coordinator.selected_card
        return card.number if card else None

    async def async_select_option(self, option: str) -> None:
        """Use another card for remote starts."""
        self._select(option)
        self.async_write_ha_state()

    def _select(self, number: str) -> None:
        for card in self.coordinator.cards:
            if card.number == number:
                self.coordinator.selected_card = card
