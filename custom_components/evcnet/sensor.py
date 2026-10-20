"""Sensors for EVC-net charge points."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .api import Channel, ChannelState, ChargePointStatus
from .coordinator import EvcNetConfigEntry
from .entity import EvcNetChannelEntity, EvcNetChargePointEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EvcNetChannelSensorDescription(SensorEntityDescription):
    """Describes a channel sensor."""

    value_fn: Callable[[Channel], StateType]
    attributes_fn: Callable[[Channel], dict[str, Any]] | None = None


@dataclass(frozen=True, kw_only=True)
class EvcNetChargePointSensorDescription(SensorEntityDescription):
    """Describes a charge point sensor."""

    value_fn: Callable[[ChargePointStatus], StateType]


def _power(channel: Channel) -> float | None:
    # The portal leaves power empty while idle; report 0 unless unreachable.
    if channel.power_kw is not None:
        return channel.power_kw
    return 0.0 if channel.online else None


CHANNEL_SENSORS: tuple[EvcNetChannelSensorDescription, ...] = (
    EvcNetChannelSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=[state.value for state in ChannelState],
        value_fn=lambda ch: ch.state,
        attributes_fn=lambda ch: {"status_code": ch.status_code, "flags": ch.flags},
    ),
    EvcNetChannelSensorDescription(
        key="notification",
        translation_key="notification",
        value_fn=lambda ch: ch.notification,
    ),
    EvcNetChannelSensorDescription(
        key="power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=_power,
    ),
    EvcNetChannelSensorDescription(
        key="session_energy",
        translation_key="session_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda ch: ch.session_energy_kwh,
    ),
    EvcNetChannelSensorDescription(
        key="session_duration",
        translation_key="session_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda ch: ch.session_minutes,
    ),
    EvcNetChannelSensorDescription(
        key="card",
        translation_key="card",
        value_fn=lambda ch: ch.card_id,
        attributes_fn=lambda ch: {"customer": ch.customer_name},
    ),
    EvcNetChannelSensorDescription(
        key="state_of_charge",
        translation_key="state_of_charge",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda ch: ch.state_of_charge,
    ),
    EvcNetChannelSensorDescription(
        key="connector",
        translation_key="connector",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda ch: ch.connector,
    ),
)

CHARGE_POINT_SENSORS: tuple[EvcNetChargePointSensorDescription, ...] = (
    EvcNetChargePointSensorDescription(
        key="total_energy",
        translation_key="total_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda status: status.total_energy_kwh,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EvcNetConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up EVC-net sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for charge_point in coordinator.charge_points.values():
        entities.extend(
            EvcNetChargePointSensor(coordinator, description, charge_point.id)
            for description in CHARGE_POINT_SENSORS
        )
        entities.extend(
            EvcNetChannelSensor(coordinator, description, charge_point.id, channel)
            for channel in charge_point.channels
            for description in CHANNEL_SENSORS
        )
    async_add_entities(entities)


class EvcNetChargePointSensor(EvcNetChargePointEntity, SensorEntity):
    """A sensor for a whole charge point."""

    entity_description: EvcNetChargePointSensorDescription

    @property
    def native_value(self) -> StateType:
        """Return the sensor value."""
        if (status := self.charge_point_status) is None:
            return None
        return self.entity_description.value_fn(status)


class EvcNetChannelSensor(EvcNetChannelEntity, SensorEntity):
    """A sensor for one channel of a charge point."""

    entity_description: EvcNetChannelSensorDescription

    @property
    def native_value(self) -> StateType:
        """Return the sensor value."""
        if (channel := self.channel_status) is None:
            return None
        return self.entity_description.value_fn(channel)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes."""
        if (attributes_fn := self.entity_description.attributes_fn) is None or (
            channel := self.channel_status
        ) is None:
            return None
        return attributes_fn(channel)
