"""Tests for the vendored ble_affinity module's selection behavior, wired up
exactly the way custom_components.bedjet.__init__.async_setup_entry wires it:
a preferred_getter closure reading a fake config entry's options, keyed by
CONF_PREFERRED_PROXY.

ble_affinity.py itself is vendored, not edited here (see its own docstring
for the mechanism) - these tests pin the observable contract the wiring
depends on: a present, healthy, preferred scanner wins; anything else falls
back to whatever the base class would have picked on its own.
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.bedjet.ble_affinity import make_affinity_client_class
from custom_components.bedjet.const import CONF_PREFERRED_PROXY

ADDRESS = "FC:F5:C4:20:1A:92"
DEFAULT_BACKEND = SimpleNamespace(scanner=SimpleNamespace(name="default-scanner"))


class FakeConnector:
    def __init__(self, can_connect: bool = True) -> None:
        self._can_connect = can_connect

    def can_connect(self) -> bool:
        return self._can_connect


class FakeScanner:
    """Stands in for a habluetooth BaseHaScanner - just the bits ble_affinity reads."""

    def __init__(self, adapter: str, *, connectable: bool = True, failures: int = 0) -> None:
        self.adapter = adapter
        self.source = adapter
        self.name = adapter
        self.connector = FakeConnector(connectable)
        self._failures = failures

    def connection_failures(self, _address: str) -> int:
        return self._failures


class FakeScannerDevice:
    def __init__(self, scanner: FakeScanner) -> None:
        self.scanner = scanner
        self.ble_device = object()
        self.advertisement = SimpleNamespace(rssi=-55)


class FakeManager:
    def __init__(self, scanner_devices: list[FakeScannerDevice]) -> None:
        self._scanner_devices = scanner_devices

    def async_scanner_devices_by_address(self, _address: str, _connectable: bool):
        return list(self._scanner_devices)


class FakeBase:
    """Stands in for whatever bleak_retry_connector.BleakClientWithServiceCache
    resolves to once Home Assistant's `bluetooth` component has monkeypatched
    it: the two selection hooks ble_affinity overrides/calls, plus the
    name-mangled address attribute HaBleakClientWrapper carries pre-connect.
    """

    _HaBleakClientWrapper__address = ADDRESS

    def _async_get_best_available_backend_and_device(self, _manager):
        return DEFAULT_BACKEND

    def _async_get_backend_for_ble_device(self, _manager, scanner, ble_device):
        return SimpleNamespace(scanner=scanner, ble_device=ble_device)


def make_entry(preferred: str = "") -> SimpleNamespace:
    """A fake config entry with just the one option bedjet's options flow writes."""
    return SimpleNamespace(options={CONF_PREFERRED_PROXY: preferred} if preferred else {})


def select(entry: SimpleNamespace, manager: FakeManager, *, on_choice=None):
    """Build the affinity class exactly as __init__.py does, then select once."""
    affinity_class = make_affinity_client_class(
        FakeBase,
        lambda: entry.options.get(CONF_PREFERRED_PROXY) or None,
        on_choice=on_choice,
    )
    client = affinity_class()
    return client._async_get_best_available_backend_and_device(manager)


def test_no_preference_configured_uses_the_default_selection() -> None:
    entry = make_entry()  # automatic - the default for every device today
    manager = FakeManager([FakeScannerDevice(FakeScanner("plant-room-bluetooth-proxy"))])

    backend = select(entry, manager)

    assert backend is DEFAULT_BACKEND


def test_preferred_scanner_present_and_healthy_wins() -> None:
    entry = make_entry("plant-room-bluetooth-proxy")
    preferred_scanner = FakeScanner("plant-room-bluetooth-proxy")
    manager = FakeManager(
        [
            FakeScannerDevice(FakeScanner("master-bedroom-bluetooth-proxy")),
            FakeScannerDevice(preferred_scanner),
        ]
    )
    choices: list[tuple[str, bool]] = []

    backend = select(entry, manager, on_choice=lambda name, used: choices.append((name, used)))

    assert backend.scanner is preferred_scanner
    assert choices == [("plant-room-bluetooth-proxy", True)]


def test_preferred_scanner_not_currently_visible_falls_back_to_default() -> None:
    entry = make_entry("plant-room-bluetooth-proxy")
    manager = FakeManager([FakeScannerDevice(FakeScanner("master-bedroom-bluetooth-proxy"))])
    choices: list[tuple[str, bool]] = []

    backend = select(entry, manager, on_choice=lambda name, used: choices.append((name, used)))

    assert backend is DEFAULT_BACKEND
    assert choices == [("default-scanner", False)]


def test_preferred_scanner_after_three_failures_falls_back_to_default() -> None:
    entry = make_entry("plant-room-bluetooth-proxy")
    failing_scanner = FakeScanner("plant-room-bluetooth-proxy", failures=3)
    manager = FakeManager([FakeScannerDevice(failing_scanner)])
    choices: list[tuple[str, bool]] = []

    backend = select(entry, manager, on_choice=lambda name, used: choices.append((name, used)))

    assert backend is DEFAULT_BACKEND
    assert choices == [("default-scanner", False)]


def test_preferred_scanner_below_the_failure_threshold_still_wins() -> None:
    entry = make_entry("plant-room-bluetooth-proxy")
    flaky_scanner = FakeScanner("plant-room-bluetooth-proxy", failures=2)
    manager = FakeManager([FakeScannerDevice(flaky_scanner)])

    backend = select(entry, manager)

    assert backend.scanner is flaky_scanner


def test_preferred_scanner_without_a_free_slot_falls_back_to_default() -> None:
    entry = make_entry("plant-room-bluetooth-proxy")
    busy_scanner = FakeScanner("plant-room-bluetooth-proxy", connectable=False)
    manager = FakeManager([FakeScannerDevice(busy_scanner)])

    backend = select(entry, manager)

    assert backend is DEFAULT_BACKEND
