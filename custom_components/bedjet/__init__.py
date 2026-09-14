"""The BedJet integration."""

from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.match import ADDRESS, BluetoothCallbackMatcher
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import BedJetCoordinator
from .pybedjet import BedJet
import contextlib
import voluptuous as vol
from homeassistant.helpers.event import async_call_later
from typing import Any

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
    stack at all, so there is no BLEDevice to connect to yet. Home Assistant
    retries setup on its own backoff for as long as the device stays silent;
    entities simply report unavailable until an advertisement arrives.
    """
    _async_register_services(hass)
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


# ---------------------------------------------------------------------------
# release_link - clean teardown before Home Assistant goes away
# ---------------------------------------------------------------------------
#
# Home Assistant does NOT unload config entries on shutdown: it fires
# EVENT_HOMEASSISTANT_STOP and the `bluetooth` integration tears its stack down
# concurrently, so held GATT links die without a completed disconnect. The
# peripheral keeps believing it is connected, stops advertising, and answers
# nobody - a ghost link Home Assistant cannot see because the proxy reports its
# slots free (measured 2026-09-09/10: an HA restart wedged three devices; only
# rebooting the proxy holding each stale link freed them).
#
# The proxies now release their links themselves 25 s after losing their API
# client, which covers every way HA can vanish including a crash or a power
# cut. This action is the cooperative path for the case HA *is* still running:
# it releases the link before the restart rather than during it. Implemented by
# unloading the entry, because async_unload_entry already runs the coordinator's async_shutdown, which
# unregisters the device callback and drops the single connection this device
# allows. Anything gentler leaves that one slot occupied.
#
# `resume_after` sets the entry up again if no restart follows, so an operator
# who calls this and changes their mind is not left with a dead device that
# would raise its own unreachable repair a quarter of an hour later.
SERVICE_RELEASE_LINK = "release_link"
ATTR_RESUME_AFTER = "resume_after"
DEFAULT_RESUME_AFTER = 180
RELEASE_LINK_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_RESUME_AFTER, default=DEFAULT_RESUME_AFTER): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=900)
        )
    }
)


async def _async_release_links(hass: HomeAssistant, resume_after: int) -> None:
    """Unload every loaded entry, then set them up again if nothing restarts."""
    released = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED and True
    ]
    for entry in released:
        with contextlib.suppress(Exception):
            await hass.config_entries.async_unload(entry.entry_id)
        _LOGGER.info("Released the Bluetooth link held for %s", entry.title)

    if not released or resume_after <= 0:
        return

    async def _resume(_now: Any) -> None:
        for entry in released:
            if entry.state is ConfigEntryState.LOADED:
                continue  # something set it up already; it owns itself now
            with contextlib.suppress(Exception):
                await hass.config_entries.async_setup(entry.entry_id)
        _LOGGER.info(
            "No restart followed release_link within %s s; links re-established",
            resume_after,
        )

    async_call_later(hass, resume_after, _resume)


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register the domain action once, however many devices are configured."""
    if hass.services.has_service(DOMAIN, SERVICE_RELEASE_LINK):
        return

    async def _handle(call: ServiceCall) -> None:
        await _async_release_links(hass, call.data[ATTR_RESUME_AFTER])

    hass.services.async_register(
        DOMAIN, SERVICE_RELEASE_LINK, _handle, schema=RELEASE_LINK_SCHEMA
    )
