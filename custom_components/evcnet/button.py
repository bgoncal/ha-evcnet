"""Buttons for the commands of the portal's real-time dashboard."""

from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import ChargePointAction, EvcNetAuthError, EvcNetError
from .const import LOGGER
from .coordinator import EvcNetConfigEntry, EvcNetCoordinator
from .entity import EvcNetChannelEntity, EvcNetChargePointEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class EvcNetButtonDescription(ButtonEntityDescription):
    """Describes a button that sends a portal command."""

    action: ChargePointAction


CHARGE_POINT_BUTTONS: tuple[EvcNetButtonDescription, ...] = (
    EvcNetButtonDescription(
        key="hard_reset",
        translation_key="hard_reset",
        action=ChargePointAction.HARD_RESET,
        device_class=ButtonDeviceClass.RESTART,
        entity_category=EntityCategory.CONFIG,
    ),
    EvcNetButtonDescription(
        key="soft_reset",
        translation_key="soft_reset",
        action=ChargePointAction.SOFT_RESET,
        device_class=ButtonDeviceClass.RESTART,
        entity_category=EntityCategory.CONFIG,
    ),
    EvcNetButtonDescription(
        key="retrieve_status",
        translation_key="retrieve_status",
        action=ChargePointAction.GET_STATUS,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    EvcNetButtonDescription(
        key="block",
        translation_key="block",
        action=ChargePointAction.BLOCK,
    ),
    EvcNetButtonDescription(
        key="unblock",
        translation_key="unblock",
        action=ChargePointAction.UNBLOCK,
    ),
)

CHANNEL_BUTTONS: tuple[EvcNetButtonDescription, ...] = (
    EvcNetButtonDescription(
        key="start_charging",
        translation_key="start_charging",
        action=ChargePointAction.START_TRANSACTION,
    ),
    EvcNetButtonDescription(
        key="stop_charging",
        translation_key="stop_charging",
        action=ChargePointAction.STOP_TRANSACTION,
    ),
    EvcNetButtonDescription(
        key="unlock_connector",
        translation_key="unlock_connector",
        action=ChargePointAction.UNLOCK_CONNECTOR,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EvcNetConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up EVC-net buttons."""
    coordinator = entry.runtime_data
    entities: list[ButtonEntity] = []
    for charge_point in coordinator.charge_points.values():
        entities.extend(
            EvcNetChargePointButton(coordinator, description, charge_point.id)
            for description in CHARGE_POINT_BUTTONS
        )
        entities.extend(
            EvcNetChannelButton(coordinator, description, charge_point.id, channel)
            for channel in charge_point.channels
            for description in CHANNEL_BUTTONS
        )
    async_add_entities(entities)


async def _send(
    coordinator: EvcNetCoordinator,
    charge_point_id: str,
    action: ChargePointAction,
    **kwargs: Any,
) -> None:
    status = coordinator.data.get(charge_point_id) if coordinator.data else None
    if status is not None and status.online is False:
        # The portal cannot pass commands on, and might deliver them later.
        raise HomeAssistantError(
            f"The charger has no connection to the portal, so {action} was not sent"
        )
    try:
        result = await coordinator.client.action(charge_point_id, action, **kwargs)
    except EvcNetAuthError as err:
        coordinator.config_entry.async_start_reauth(coordinator.hass)
        raise HomeAssistantError(
            f"Log in to the portal again before sending {action}: {err}"
        ) from err
    except EvcNetError as err:
        raise HomeAssistantError(f"The portal did not take {action}: {err}") from err
    LOGGER.debug("%s on %s answered %r", action, charge_point_id, result)
    await coordinator.async_request_refresh()


class EvcNetChargePointButton(EvcNetChargePointEntity, ButtonEntity):
    """A command for the whole charge point."""

    entity_description: EvcNetButtonDescription

    async def async_press(self) -> None:
        """Send the command; block and unblock apply to all channels."""
        await _send(
            self.coordinator, self._charge_point_id, self.entity_description.action
        )


class EvcNetChannelButton(EvcNetChannelEntity, ButtonEntity):
    """A command for one channel (connector)."""

    entity_description: EvcNetButtonDescription

    async def async_press(self) -> None:
        """Send the command for this channel."""
        action = self.entity_description.action
        kwargs: dict[str, Any] = {"channel": self._channel}
        if action is ChargePointAction.START_TRANSACTION:
            if (card := self.coordinator.selected_card) is None:
                raise HomeAssistantError(
                    "No charging card found on the portal account to start with"
                )
            kwargs["card"] = card.id
        await _send(self.coordinator, self._charge_point_id, action, **kwargs)
