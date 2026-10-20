"""Base entities for EVC-net."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Channel, ChargePoint, ChargePointStatus
from .const import DOMAIN
from .coordinator import EvcNetCoordinator


def charge_point_device_info(base_url: str, charge_point: ChargePoint) -> DeviceInfo:
    """Return the device info of a charge point."""
    return DeviceInfo(
        identifiers={(DOMAIN, charge_point.id)},
        name=charge_point.name or f"Charge point {charge_point.id}",
        serial_number=charge_point.id,
        sw_version=charge_point.software_version,
        configuration_url=f"{base_url}/RechargeSpots/Detail/{charge_point.id}",
    )


class EvcNetChargePointEntity(CoordinatorEntity[EvcNetCoordinator]):
    """An entity describing a whole charge point."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EvcNetCoordinator,
        description: EntityDescription,
        charge_point_id: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._charge_point_id = charge_point_id
        self._attr_unique_id = f"{charge_point_id}_{description.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, charge_point_id)})

    @property
    def charge_point_status(self) -> ChargePointStatus | None:
        """Return the latest status of the charge point."""
        return self.coordinator.data.get(self._charge_point_id)

    @property
    def available(self) -> bool:
        """Return if the charge point is still reported by the portal."""
        return super().available and self.charge_point_status is not None


class EvcNetChannelEntity(EvcNetChargePointEntity):
    """An entity describing one channel (connector) of a charge point.

    Single-channel charge points keep everything on one device; extra
    channels get their own device so entity names stay unambiguous.
    """

    def __init__(
        self,
        coordinator: EvcNetCoordinator,
        description: EntityDescription,
        charge_point_id: str,
        channel: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, description, charge_point_id)
        self._channel = channel
        self._attr_unique_id = f"{charge_point_id}_{channel}_{description.key}"
        charge_point = coordinator.charge_points[charge_point_id]
        if len(charge_point.channels) > 1:
            parent_name = charge_point.name or f"Charge point {charge_point_id}"
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, f"{charge_point_id}_{channel}")},
                name=f"{parent_name} connector {channel}",
                via_device=(DOMAIN, charge_point_id),
            )

    @property
    def channel_status(self) -> Channel | None:
        """Return the latest status of this channel."""
        if (status := self.charge_point_status) is None:
            return None
        return status.channels.get(self._channel)

    @property
    def available(self) -> bool:
        """Return if the channel is still reported by the portal."""
        return super().available and self.channel_status is not None
