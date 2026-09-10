"""The BedJet integration."""

from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.match import ADDRESS, BluetoothCallbackMatcher
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.util import dt as dt_util

from .coordinator import BedJetCoordinator
from .pybedjet import BedJet

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.FAN,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]

_LOGGER = logging.getLogger(__name__)

type BedJetConfigEntry = ConfigEntry[BedJetCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: BedJetConfigEntry) -> bool:
    """Set up BedJet from a config entry.

    Setup never blocks on the device actually answering: the BedJet only
    accepts one BLE connection at a time, so it may be legitimately busy
    (held by the phone app) for as long as a user wants. If setup instead
    waited for a first status frame, the config entry - and with it the
    always-available Bluetooth Connection switch a user needs to reclaim the
    slot - would never even be created while the app has it. Every entity
    besides that switch simply reports unavailable until the coordinator
    receives its first pushed frame.

    ConfigEntryNotReady is only raised for the one case more waiting cannot
    fix: this address has never been seen by Home Assistant's Bluetooth
    stack at all, so there is no BLEDevice to connect to yet.
    """
    address: str = entry.data[CONF_ADDRESS]
    service_info = bluetooth.async_last_service_info(hass, address, connectable=True)
    if service_info is None:
        raise ConfigEntryNotReady(
            f"BedJet {address} has not been seen by Bluetooth yet"
        )

    device = BedJet(
        service_info.device,
        service_info.advertisement,
        source=service_info.source,
        clock=dt_util.now,
    )

    @callback
    def _async_update_ble(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Feed every advertisement seen for this address to the library.

        The BedJet only advertises while nothing is connected to it, so an
        advertisement is how the library learns the phone app (or a previous
        Home Assistant connection) released the single connection slot.
        """
        device.set_ble_device_and_advertisement_data(
            service_info.device, service_info.advertisement, source=service_info.source
        )

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_update_ble,
            BluetoothCallbackMatcher({ADDRESS: address}),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )

    coordinator = BedJetCoordinator(hass, entry, device)
    entry.runtime_data = coordinator

    # Kicks off the maintain-and-reconnect loop; does not
    # wait for a connection to actually succeed.
    await device.start()

    # The one reconciliation that runs after a config entry reload, and so the
    # one that stops a `device_unreachable` repair from outliving the outage it
    # describes: reloading builds a brand-new coordinator, whose in-memory view
    # of "was an issue open?" is empty by construction. It also starts the
    # 15-minute countdown when the link is already down at setup.
    coordinator.async_reconcile_unreachable_issue()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _async_stop(event: Event) -> None:
        """Release the BLE connection on Home Assistant stop."""
        await device.stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BedJetConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.device.stop()
    return unload_ok
