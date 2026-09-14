"""BedJet sensor entities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from habluetooth import get_manager

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BedJetConfigEntry
from .entity import BedJetEntity
from .pybedjet import BedJet, BedJetNotification


@dataclass(frozen=True, kw_only=True)
class BedJetSensorEntityDescription(SensorEntityDescription):
    """BedJet sensor entity description."""

    value_fn: Callable[[BedJet], Any]


SENSORS = (
    BedJetSensorEntityDescription(
        key="ambient_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        translation_key="ambient_temperature",
        value_fn=lambda device: device.state.ambient_temp_c,
    ),
    BedJetSensorEntityDescription(
        key="outlet_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        translation_key="outlet_temperature",
        value_fn=lambda device: device.state.actual_temp_c,
    ),
    BedJetSensorEntityDescription(
        key="notification",
        device_class=SensorDeviceClass.ENUM,
        options=[member.name.lower() for member in BedJetNotification],
        translation_key="notification",
        value_fn=(
            lambda device: (
                notification.name.lower()
                if (notification := device.state.notification) is not None
                else None
            )
        ),
    ),
    BedJetSensorEntityDescription(
        key="bio_sequence_step",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        translation_key="bio_sequence_step",
        value_fn=lambda device: device.state.bio_sequence_step,
    ),
    BedJetSensorEntityDescription(
        key="shutdown_reason",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        translation_key="shutdown_reason",
        value_fn=lambda device: device.state.shutdown_reason,
    ),
    BedJetSensorEntityDescription(
        key="turbo_time",
        device_class=SensorDeviceClass.DURATION,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        translation_key="turbo_time",
        value_fn=lambda device: device.state.turbo_time,
    ),
    BedJetSensorEntityDescription(
        key="update_phase",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        translation_key="update_phase",
        value_fn=lambda device: device.state.update_phase,
    ),
)

# The one sensor an automation is expected to read while the link is *down*,
# so it is diagnostic but enabled by default (and always available - see
# `BedJetConnectionSensorEntity.available`). It carries no `value_fn`: its
# state comes from habluetooth's live slot allocations, not from a frame.
CONNECTION_SENSOR = SensorEntityDescription(
    key="connection",
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="connection",
)

#: State reported whenever no scanner is holding a GATT link to this device.
STATE_DISCONNECTED = "disconnected"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BedJetConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensor platform for BedJet."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            *(
                BedJetSensorEntity(coordinator, entry.title, descriptor)
                for descriptor in SENSORS
            ),
            BedJetConnectionSensorEntity(coordinator, entry.title),
        ]
    )


class BedJetSensorEntity(BedJetEntity, SensorEntity):
    """Representation of a BedJet sensor."""

    entity_description: BedJetSensorEntityDescription

    def __init__(self, coordinator, name: str, entity_description) -> None:
        """Initialize a BedJet sensor entity."""
        self.entity_description = entity_description
        self._attr_unique_id = f"{coordinator.device.address}_{entity_description.key}"
        super().__init__(coordinator, name)

    @callback
    def _async_update_attrs(self) -> None:
        """Handle updating _attr values."""
        if self.coordinator.data is None:
            return
        self._attr_native_value = self.entity_description.value_fn(self._device)


class BedJetConnectionSensorEntity(BedJetEntity, SensorEntity):
    """Which Bluetooth proxy currently carries this BedJet's held GATT link.

    Exists so a heal automation can tell *which* proxy to restart - and, just
    as importantly, can leave alone a proxy that other devices are holding.
    The state is the scanner's display name while a link is held, or the
    literal "disconnected".
    """

    entity_description = CONNECTION_SENSOR

    def __init__(self, coordinator, name: str) -> None:
        """Initialize the connection sensor."""
        self._attr_unique_id = f"{coordinator.device.address}_connection"
        super().__init__(coordinator, name)

    @property
    def available(self) -> bool:
        """Always True: reporting "disconnected" is this sensor's whole job."""
        return True

    async def async_added_to_hass(self) -> None:
        """Subscribe to habluetooth's connection-slot allocation changes.

        Allocations change on connect and disconnect without any status frame
        being involved, so the coordinator's frame pushes alone would leave
        this sensor stale (and a dropped link produces no frames at all).
        """
        await super().async_added_to_hass()
        self.async_on_remove(
            get_manager().async_register_allocation_callback(
                self._async_allocations_changed, None
            )
        )

    @callback
    def _async_allocations_changed(self, _allocations) -> None:
        """Re-read the authoritative allocation set and republish.

        The callback payload is only the one scanner that changed; the
        coordinator re-reads every scanner's allocations, which is both
        cheaper to reason about and correct when a link moves between
        proxies (one scanner frees a slot as another claims it).
        """
        self._async_update_attrs()
        self.async_write_ha_state()

    @callback
    def _async_update_attrs(self) -> None:
        """Handle updating _attr values."""
        self._attr_native_value = (
            self.coordinator.connection_scanner_name or STATE_DISCONNECTED
        )
