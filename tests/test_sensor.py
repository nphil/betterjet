"""Tests for the BedJet sensor platform: unique_ids, values, and the
Connection diagnostic sensor - which proxy holds the link, plus the
preferred-proxy affinity attributes ble_affinity.py's client_class reports
back through (see custom_components.bedjet.__init__._on_proxy_choice).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import custom_components.bedjet.sensor as sensor
from custom_components.bedjet.const import CONF_PREFERRED_PROXY
from custom_components.bedjet.pybedjet.const import BedJetNotification
from custom_components.bedjet.sensor import (
    CONNECTION_SENSOR,
    SENSORS,
    STATE_DISCONNECTED,
    BedJetConnectionSensorEntity,
    BedJetSensorEntity,
)
from homeassistant.const import EntityCategory

ADDRESS = "FC:F5:C4:20:1A:92"


class FakeDevice:
    def __init__(self) -> None:
        self.address = ADDRESS
        self.scanner_source = "D4:D4:DA:9D:40:8A"
        self.connected = True
        self.via_preferred_proxy = False
        self.state = SimpleNamespace(
            ambient_temp_c=24.5,
            actual_temp_c=25.0,
            notification=BedJetNotification.CLEAN_FILTER,
            bio_sequence_step=0,
            shutdown_reason=0,
            turbo_time=0,
            update_phase=0x15,
        )


class FakeCoordinator:
    def __init__(self, device: FakeDevice) -> None:
        self.device = device
        self.available = True
        self.data = device.state
        self.connection_scanner_name: str | None = "master-bedroom-bluetooth-proxy"
        self.config_entry = SimpleNamespace(options={})

    def async_add_listener(self, update_callback, context=None):
        return lambda: None


class FakeManager:
    """Stand-in for habluetooth's manager: records allocation subscribers."""

    def __init__(self) -> None:
        self.callbacks: list = []
        self.unsubscribes = 0

    def async_register_allocation_callback(self, callback, source):
        self.callbacks.append((callback, source))

        def _unsubscribe() -> None:
            self.unsubscribes += 1
            self.callbacks.remove((callback, source))

        return _unsubscribe


def descriptor_by_key(key: str):
    return next(d for d in SENSORS if d.key == key)


def make_entity(key: str, device: FakeDevice | None = None):
    device = device or FakeDevice()
    coordinator = FakeCoordinator(device)
    return BedJetSensorEntity(coordinator, "Bedjetty", descriptor_by_key(key)), coordinator, device


def test_all_unique_ids_are_address_prefixed_by_key() -> None:
    device = FakeDevice()
    coordinator = FakeCoordinator(device)
    for descriptor in SENSORS:
        entity = BedJetSensorEntity(coordinator, "Bedjetty", descriptor)
        assert entity.unique_id == f"{ADDRESS}_{descriptor.key}"


def test_ambient_temperature_value() -> None:
    entity, _coordinator, _device = make_entity("ambient_temperature")
    assert entity.native_value == 24.5


def test_outlet_temperature_value() -> None:
    entity, _coordinator, _device = make_entity("outlet_temperature")
    assert entity.native_value == 25.0


def test_notification_value_is_lowercase_enum_name() -> None:
    entity, _coordinator, _device = make_entity("notification")
    assert entity.native_value == "clean_filter"


def test_notification_none_when_no_pending_notification() -> None:
    device = FakeDevice()
    device.state = SimpleNamespace(**{**device.state.__dict__, "notification": None})
    entity, _coordinator, _device = make_entity("notification", device)
    assert entity.native_value is None


def test_notification_none_member_renders_as_string_not_python_none() -> None:
    # Regression: BedJetNotification.NONE (value 0, "no notification
    # pending") must render as the string "none", distinct from the sensor
    # being unavailable because no frame has decoded a notification yet.
    device = FakeDevice()
    device.state = SimpleNamespace(
        **{**device.state.__dict__, "notification": BedJetNotification.NONE}
    )
    entity, _coordinator, _device = make_entity("notification", device)
    assert entity.native_value == "none"


def test_diagnostic_sensors_are_disabled_by_default() -> None:
    for key in ("bio_sequence_step", "shutdown_reason", "turbo_time", "update_phase"):
        descriptor = descriptor_by_key(key)
        assert descriptor.entity_registry_enabled_default is False


def test_ambient_and_outlet_are_enabled_by_default() -> None:
    for key in ("ambient_temperature", "outlet_temperature"):
        descriptor = descriptor_by_key(key)
        assert descriptor.entity_registry_enabled_default is True

def test_notification_is_enabled_default_with_no_category() -> None:
    # Regression: notification was briefly diagnostic+enabled (an
    # inconsistent combination); the assignment lists it only under
    # "enabled by default", so it must carry no entity_category.
    descriptor = descriptor_by_key("notification")
    assert descriptor.entity_category is None
    assert descriptor.entity_registry_enabled_default is True


def test_non_scanner_sensor_skips_update_without_a_decoded_frame() -> None:
    device = FakeDevice()
    coordinator = FakeCoordinator(device)
    coordinator.data = None
    entity = BedJetSensorEntity(coordinator, "Bedjetty", descriptor_by_key("ambient_temperature"))

    # Never assigned because coordinator.data was None at update time.
    assert entity.native_value is None


class TestConnectionSensor:
    """The one sensor a heal automation reads while the link is down: it must
    name the proxy actually carrying the GATT link, stay available when
    disconnected, and update on allocation pushes (a dropped link produces no
    status frames at all, so frame pushes alone would leave it stale).
    """

    def make(self, monkeypatch):
        manager = FakeManager()
        monkeypatch.setattr(sensor, "get_manager", lambda: manager)
        device = FakeDevice()
        coordinator = FakeCoordinator(device)
        entity = BedJetConnectionSensorEntity(coordinator, "Bedjetty")
        return entity, coordinator, device, manager

    def test_unique_id_and_registry_defaults(self, monkeypatch) -> None:
        entity, _coordinator, _device, _manager = self.make(monkeypatch)

        assert entity.unique_id == f"{ADDRESS}_connection"
        assert CONNECTION_SENSOR.entity_category is EntityCategory.DIAGNOSTIC
        # Automations read this one, so it must not be opt-in.
        assert CONNECTION_SENSOR.entity_registry_enabled_default is True

    def test_state_is_the_holding_scanner_name(self, monkeypatch) -> None:
        entity, _coordinator, _device, _manager = self.make(monkeypatch)
        assert entity.native_value == "master-bedroom-bluetooth-proxy"

    def test_state_is_disconnected_when_no_scanner_holds_the_link(
        self, monkeypatch
    ) -> None:
        entity, coordinator, device, _manager = self.make(monkeypatch)
        coordinator.connection_scanner_name = None
        device.connected = False

        entity._async_update_attrs()

        assert entity.native_value == STATE_DISCONNECTED

    def test_stays_available_and_updates_while_disconnected(self, monkeypatch) -> None:
        entity, coordinator, device, _manager = self.make(monkeypatch)
        coordinator.available = False
        coordinator.data = None  # no frame has ever decoded
        coordinator.connection_scanner_name = None

        entity._async_update_attrs()

        assert entity.available is True
        assert entity.native_value == STATE_DISCONNECTED

    def test_extra_state_attributes_report_the_configured_preference_and_last_choice(
        self, monkeypatch
    ) -> None:
        entity, coordinator, device, _manager = self.make(monkeypatch)
        coordinator.config_entry.options = {CONF_PREFERRED_PROXY: "plant-room-bluetooth-proxy"}
        device.via_preferred_proxy = True

        entity._async_update_attrs()

        assert entity.extra_state_attributes == {
            "preferred_proxy": "plant-room-bluetooth-proxy",
            "via_preferred_proxy": True,
        }

    def test_preferred_proxy_attribute_is_none_when_automatic(self, monkeypatch) -> None:
        entity, _coordinator, _device, _manager = self.make(monkeypatch)
        assert entity.extra_state_attributes["preferred_proxy"] is None
        assert entity.extra_state_attributes["via_preferred_proxy"] is False

    def test_allocation_change_republishes_the_new_holder(self, monkeypatch) -> None:
        entity, coordinator, _device, manager = self.make(monkeypatch)
        asyncio.run(entity.async_added_to_hass())
        assert len(manager.callbacks) == 1
        writes_before = entity.write_ha_state_calls

        # The link moved to another proxy; only the allocation feed knows.
        coordinator.connection_scanner_name = "plant-room-bluetooth-proxy"
        allocation_callback, source = manager.callbacks[0]
        assert source is None  # every scanner, not one
        allocation_callback(object())

        assert entity.native_value == "plant-room-bluetooth-proxy"
        assert entity.write_ha_state_calls > writes_before

    def test_removal_unsubscribes_from_allocation_updates(self, monkeypatch) -> None:
        entity, _coordinator, _device, manager = self.make(monkeypatch)
        asyncio.run(entity.async_added_to_hass())

        asyncio.run(entity.async_remove())

        assert manager.unsubscribes == 1
        assert manager.callbacks == []
