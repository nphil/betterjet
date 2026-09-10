"""Tests for custom_components.bedjet.__init__: setup/unload lifecycle.

The real pybedjet.BedJet and homeassistant.components.bluetooth are replaced
with fakes/monkeypatches so this exercises only the integration's own wiring:
ConfigEntryNotReady only when HA's bluetooth stack has never seen the address
at all (never for "device hasn't answered yet" - device.start() only kicks
off the background connect-retry loop and returns immediately, so setup must
forward platforms and let entities report unavailable rather than blocking
or failing config entry setup), plus the advertisement callback that hands
the library every seen advertisement (how it learns the phone released the
slot).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import custom_components.bedjet as bedjet_init
from homeassistant.const import CONF_ADDRESS
from homeassistant.exceptions import ConfigEntryNotReady

ADDRESS = "FC:F5:C4:20:1A:92"


class FakeBedJet:
    instances: list["FakeBedJet"] = []

    def __init__(self, ble_device, advertisement_data, *, source=None, clock=None) -> None:
        self.ble_device = ble_device
        self.advertisement_data = advertisement_data
        self.source = source
        self.clock = clock
        self.started = False
        self.stopped = False
        self.callbacks: list = []
        self.set_ble_calls: list[tuple] = []
        self.state = SimpleNamespace(sentinel=True)
        self.address = ADDRESS
        self.connected = False
        # Setup reconciles the `device_unreachable` repair against the live
        # link, so the fake has to answer the same freshness question the
        # real device does.
        self.available = False
        self.scanner_source = source
        FakeBedJet.instances.append(self)

    def register_callback(self, callback):
        self.callbacks.append(callback)
        return lambda: self.callbacks.remove(callback)

    async def start(self) -> None:
        # Real BedJet.start() only spawns the background connect-retry
        # supervisor and returns immediately - it never blocks on an actual
        # connection succeeding.
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def set_ble_device_and_advertisement_data(self, device, adv, *, source=None) -> None:
        self.set_ble_calls.append((device, adv, source))


class FakeBus:
    def __init__(self) -> None:
        self.listeners: list[tuple[str, object]] = []

    def async_listen_once(self, event_type, callback):
        self.listeners.append((event_type, callback))
        return lambda: self.listeners.remove((event_type, callback))


class FakeConfigEntries:
    def __init__(self) -> None:
        self.forwarded: list[tuple] = []
        self.unloaded: list[tuple] = []

    async def async_forward_entry_setups(self, entry, platforms) -> None:
        self.forwarded.append((entry, tuple(platforms)))

    async def async_unload_platforms(self, entry, platforms) -> bool:
        self.unloaded.append((entry, tuple(platforms)))
        return True


class FakeHass:
    def __init__(self) -> None:
        self.bus = FakeBus()
        self.config_entries = FakeConfigEntries()
        # The `device_unreachable` outage clock lives here, keyed by address.
        self.data: dict = {}


class FakeEntry:
    def __init__(self, address: str = ADDRESS) -> None:
        self.data = {CONF_ADDRESS: address}
        self.title = "Bedjetty"
        self.runtime_data = None
        self._unload_callbacks: list = []

    def async_on_unload(self, callback) -> None:
        self._unload_callbacks.append(callback)


@pytest.fixture(autouse=True)
def _reset_fake_bedjet_instances():
    FakeBedJet.instances.clear()
    yield
    FakeBedJet.instances.clear()


@pytest.fixture
def patched_bedjet(monkeypatch):
    monkeypatch.setattr(bedjet_init, "BedJet", FakeBedJet)
    monkeypatch.setattr(
        bedjet_init.bluetooth, "async_register_callback", lambda *a, **k: (lambda: None)
    )


def test_setup_raises_config_entry_not_ready_when_never_seen(monkeypatch, patched_bedjet) -> None:
    monkeypatch.setattr(
        bedjet_init.bluetooth, "async_last_service_info", lambda hass, address, connectable=True: None
    )
    hass = FakeHass()
    entry = FakeEntry()

    with pytest.raises(ConfigEntryNotReady):
        asyncio.run(bedjet_init.async_setup_entry(hass, entry))


def test_setup_does_not_block_or_fail_when_device_never_answers(
    monkeypatch, patched_bedjet
) -> None:
    """The slot may already be held by the phone app at HA startup; setup
    must still succeed and load entities (which then report unavailable),
    so the user can see the device and the failure instead of a setup error.
    """
    service_info = SimpleNamespace(device=object(), advertisement=object(), source="D4:D4:DA:9D:40:8A")
    monkeypatch.setattr(
        bedjet_init.bluetooth,
        "async_last_service_info",
        lambda hass, address, connectable=True: service_info,
    )
    hass = FakeHass()
    entry = FakeEntry()

    result = asyncio.run(bedjet_init.async_setup_entry(hass, entry))

    assert result is True
    assert FakeBedJet.instances[-1].started is True
    assert hass.config_entries.forwarded  # platforms were forwarded regardless


def test_setup_success_forwards_platforms_and_sets_runtime_data(
    monkeypatch, patched_bedjet
) -> None:
    service_info = SimpleNamespace(device=object(), advertisement=object(), source="D4:D4:DA:9D:40:8A")
    monkeypatch.setattr(
        bedjet_init.bluetooth,
        "async_last_service_info",
        lambda hass, address, connectable=True: service_info,
    )
    hass = FakeHass()
    entry = FakeEntry()

    result = asyncio.run(bedjet_init.async_setup_entry(hass, entry))

    assert result is True
    assert entry.runtime_data is not None
    assert entry.runtime_data.device is FakeBedJet.instances[-1]
    [forwarded_entry, forwarded_platforms] = hass.config_entries.forwarded[-1]
    assert forwarded_entry is entry
    assert set(forwarded_platforms) == set(bedjet_init.PLATFORMS)


def test_advertisement_callback_forwards_device_and_advertisement(
    monkeypatch, patched_bedjet
) -> None:
    service_info = SimpleNamespace(device=object(), advertisement=object(), source="D4:D4:DA:9D:40:8A")
    monkeypatch.setattr(
        bedjet_init.bluetooth,
        "async_last_service_info",
        lambda hass, address, connectable=True: service_info,
    )
    captured_callback = {}

    def fake_register_callback(hass, callback, matcher, mode):
        captured_callback["fn"] = callback
        return lambda: None

    monkeypatch.setattr(bedjet_init.bluetooth, "async_register_callback", fake_register_callback)
    hass = FakeHass()
    entry = FakeEntry()

    asyncio.run(bedjet_init.async_setup_entry(hass, entry))

    device = FakeBedJet.instances[-1]
    new_service_info = SimpleNamespace(
        device=object(), advertisement=object(), source="AA:BB:CC:DD:EE:FF"
    )
    captured_callback["fn"](new_service_info, None)

    assert device.set_ble_calls[-1] == (
        new_service_info.device,
        new_service_info.advertisement,
        new_service_info.source,
    )


def test_unload_entry_stops_device_and_unloads_platforms(monkeypatch, patched_bedjet) -> None:
    hass = FakeHass()
    entry = FakeEntry()
    device = FakeBedJet(object(), object())
    entry.runtime_data = SimpleNamespace(device=device)

    result = asyncio.run(bedjet_init.async_unload_entry(hass, entry))

    assert result is True
    assert device.stopped is True
    assert hass.config_entries.unloaded[-1][0] is entry
