"""Binary sensors for EVC-net charge points."""

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import Channel, ChannelState, ChargePointStatus
from .coordinator import EvcNetConfigEntry
from .entity import EvcNetChannelEntity, EvcNetChargePointEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EvcNetChannelBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a channel binary sensor."""

    is_on_fn: Callable[[Channel], bool | None]


@dataclass(frozen=True, kw_only=True)
class EvcNetChargePointBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a charge point binary sensor."""

    is_on_fn: Callable[[ChargePointStatus], bool | None]


def _state_is(state: ChannelState) -> Callable[[Channel], bool | None]:
    def is_on(channel: Channel) -> bool | None:
        # Without communication the last known state is meaningless.
        if channel.state in (None, ChannelState.OFFLINE):
            return None
        return channel.state is state

    return is_on


CHANNEL_BINARY_SENSORS: tuple[EvcNetChannelBinarySensorDescription, ...] = (
    EvcNetChannelBinarySensorDescription(
        key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        is_on_fn=_state_is(ChannelState.CHARGING),
    ),
    EvcNetChannelBinarySensorDescription(
        key="problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=_state_is(ChannelState.FAULTY),
    ),
)

CHARGE_POINT_BINARY_SENSORS: tuple[EvcNetChargePointBinarySensorDescription, ...] = (
    EvcNetChargePointBinarySensorDescription(
        key="connectivity",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        is_on_fn=lambda status: status.online,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EvcNetConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up EVC-net binary sensors."""
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for charge_point in coordinator.charge_points.values():
        entities.extend(
            EvcNetChargePointBinarySensor(coordinator, description, charge_point.id)
            for description in CHARGE_POINT_BINARY_SENSORS
        )
        entities.extend(
            EvcNetChannelBinarySensor(
                coordinator, description, charge_point.id, channel
            )
            for channel in charge_point.channels
            for description in CHANNEL_BINARY_SENSORS
        )
    async_add_entities(entities)


class EvcNetChargePointBinarySensor(EvcNetChargePointEntity, BinarySensorEntity):
    """A binary sensor for a whole charge point."""

    entity_description: EvcNetChargePointBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
        """Return the state."""
        if (status := self.charge_point_status) is None:
            return None
        return self.entity_description.is_on_fn(status)


class EvcNetChannelBinarySensor(EvcNetChannelEntity, BinarySensorEntity):
    """A binary sensor for one channel of a charge point."""

    entity_description: EvcNetChannelBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
        """Return the state."""
        if (channel := self.channel_status) is None:
            return None
        return self.entity_description.is_on_fn(channel)
