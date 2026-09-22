"""Push-driven data update coordinator for the BedJet integration."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from datetime import UTC, datetime
import logging
import time
from typing import Any, Protocol

from habluetooth import get_manager

from homeassistant.components.bluetooth import async_scanner_by_source
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import CONF_PREFERRED_PROXY
from .pybedjet import BedJet, BedJetState

_LOGGER = logging.getLogger(__name__)

# Trailing window for the drops_1h attribute, matching the other three BLE
# integrations so one dashboard can compare them without unit surprises.
DROP_WINDOW_SECONDS = 3600.0


class SlotAllocations(Protocol):
    """The part of ``habluetooth.HaBluetoothSlotAllocations`` used here."""

    source: str
    allocated: list[str]


def resolve_connection_source(
    allocations: Iterable[SlotAllocations],
    address: str,
    connected: bool,
    fallback_source: str | None,
) -> str | None:
    """Return the scanner source currently carrying the GATT link, or None.

    ``allocations`` is habluetooth's live per-scanner slot accounting - the
    same data Home Assistant's own ``bluetooth/subscribe_connection_alloca
    tions`` websocket serves - so it names the proxy the link actually runs
    through, which is not necessarily the proxy whose advertisement we last
    saw (habluetooth re-scores every proxy by RSSI on each connect).

    ``fallback_source`` covers the case of a connection held through an
    adapter that reports no slot accounting: we are demonstrably connected,
    so report the last known scanner rather than claiming "disconnected".
    Pure: no I/O, so the mapping is testable without a Bluetooth manager.
    """
    wanted = address.upper()
    for allocation in allocations:
        if any(allocated.upper() == wanted for allocated in allocation.allocated):
            return allocation.source
    return fallback_source if connected else None


class BedJetCoordinator(DataUpdateCoordinator[BedJetState | None]):
    """Coordinate a single BedJet device.

    There is no polling interval: the BedJet notify characteristic streams a
    status frame at roughly 4 Hz for as long as a connection is held (even in
    standby), so the library pushes every decoded frame straight into
    ``async_set_updated_data`` and this coordinator never schedules a refresh
    of its own.

    No dedup is done here for consecutive identical frames: pybedjet already
    rate-limits its own callback firing for continuous-value-only changes per
    the Contract (mode/fan/target/notification changes still publish
    immediately), and Home Assistant's state machine itself is a no-op past
    the initial comparison when an entity writes an unchanged state and
    attributes. ``DataUpdateCoordinator.always_update`` would not help here
    either way - it is only consulted by the polling refresh path, not by
    ``async_set_updated_data``.
    """

    def __init__(
        self, hass: HomeAssistant, config_entry: ConfigEntry, device: BedJet
    ) -> None:
        """Initialize the coordinator and start listening to the device."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=config_entry.title,
        )
        self.device = device
        self._unregister_device_callback = device.register_callback(
            self._handle_device_update
        )
        # Tracks the GATT link edge for the operator-facing INFO log, and for
        # the drop bookkeeping below.
        self._was_connected = device.connected
        # Monotonic timestamps of unexpected disconnects, pruned to the window.
        # A deque bounded only by pruning: a link that flaps once a second for
        # an hour is 3600 floats, which is not worth a cleverer structure.
        self._drops: deque[float] = deque()
        self._last_drop: datetime | None = None

    @property
    def available(self) -> bool:
        """Return True while the device is connected and reporting fresh frames."""
        return self.device.available

    @property
    def drops_1h(self) -> int:
        """Unexpected disconnects in the trailing hour."""
        cutoff = time.monotonic() - DROP_WINDOW_SECONDS
        drops = self._drops
        while drops and drops[0] < cutoff:
            drops.popleft()
        return len(drops)

    @property
    def last_drop(self) -> str | None:
        """ISO-8601 UTC timestamp of the most recent drop, or None.

        Never pruned: the window governs only the *count*, and "when did the
        BedJet last lose its link" stays useful long after the hour is up.
        """
        if self._last_drop is None:
            return None
        return self._last_drop.isoformat()

    def connection_attributes(self) -> dict[str, Any]:
        """Attribute payload for the Connection diagnostic sensor.

        Deliberately the same key set the AC Infinity, Fluval and EcoFlow
        integrations publish, so a single template can read any of them.
        ``reconnect_attempt`` comes straight from the library's supervisor,
        which is the only component that knows how many consecutive connects
        have failed.
        """
        return {
            "drops_1h": self.drops_1h,
            "last_drop": self.last_drop,
            "reconnect_attempt": self.device.reconnect_attempt,
            "preferred_proxy": (
                self.config_entry.options.get(CONF_PREFERRED_PROXY) or None
            ),
            "via_preferred_proxy": self.device.via_preferred_proxy,
        }

    @property
    def connection_source(self) -> str | None:
        """Scanner source (MAC) currently carrying the held GATT link, or None."""
        return resolve_connection_source(
            get_manager().async_current_allocations() or (),
            self.device.address,
            self.device.connected,
            self.device.scanner_source,
        )

    @property
    def connection_scanner_name(self) -> str | None:
        """Display name of the scanner carrying the held link, or None.

        Falls back to the bare source MAC when Home Assistant has no scanner
        registered for it, which is what the core Bluetooth panel does too.
        """
        source = self.connection_source
        if source is None:
            return None
        scanner = async_scanner_by_source(self.hass, source)
        return scanner.name if scanner is not None else source

    @callback
    def _handle_device_update(self, device: BedJet) -> None:
        """Push the library's latest decoded state into the coordinator.

        State first, then the logging: the connect-edge log resolves a
        scanner name through habluetooth, and that diagnostic lookup must
        never be able to swallow an entity state update (pybedjet only logs
        a callback that raises, so anything downstream of a raise is simply
        lost).
        """
        self.async_set_updated_data(device.state)
        self._handle_connection_transition(device)

    @callback
    def _handle_connection_transition(self, device: BedJet) -> None:
        """Log each connect/disconnect edge once, naming the scanner in use.

        The library itself only knows scanner *sources* (MACs); the scanner
        name lives in Home Assistant's Bluetooth registry, so the INFO line
        operators actually read is emitted from here. The same edge is the
        only honest place to count drops: it fires once per real transition,
        so a flapping link cannot inflate the count with repeat callbacks.
        """
        connected = device.connected
        if connected == self._was_connected:
            return
        self._was_connected = connected
        if not connected:
            # A shutdown deliberately drops the link; counting that as a fault
            # would make every restart look like a BLE failure.
            if device.hold_connection:
                self._drops.append(time.monotonic())
                self._last_drop = datetime.now(UTC)
            _LOGGER.info("%s: disconnected", device.address)
            return
        _LOGGER.info(
            "%s: connected via %s",
            device.address,
            self.connection_scanner_name or "an unknown scanner",
        )

    async def async_shutdown(self) -> None:
        """Stop listening to the device in addition to the base shutdown."""
        self._unregister_device_callback()
        await super().async_shutdown()
