"""The BedJet integration."""

from __future__ import annotations

import asyncio
import logging
import time

import bleak_retry_connector

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.match import ADDRESS, BluetoothCallbackMatcher
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HassJob, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.util import dt as dt_util

from .ble_affinity import make_affinity_client_class
from .const import CONF_PREFERRED_PROXY, DOMAIN
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

# Upper bound for one entry's shutdown release. Home Assistant runs all
# shutdown jobs concurrently under a single 20 s budget.
SHUTDOWN_RELEASE_TIMEOUT_S = 8
# hass.data flag set by the first shutdown job: once true this process never
# sets an entry up (or resumes one after release_link) again.
KEY_SHUTTING_DOWN = f"{DOMAIN}_shutting_down"
KEY_RESUME_CANCELS = f"{DOMAIN}_resume_cancels"


async def _async_release_device(device: BedJet, title: str) -> None:
    """Release one device's link for good: latched, bounded, never raises."""
    started = time.monotonic()
    try:
        async with asyncio.timeout(SHUTDOWN_RELEASE_TIMEOUT_S):
            await device.release_for_shutdown()
    except asyncio.CancelledError:
        raise  # Home Assistant itself gave up on the job; not ours to eat
    except Exception as err:  # noqa: BLE001 - a shutdown job must never raise
        _LOGGER.warning(
            "Could not release BLE link to %s at shutdown within %.0f s: %r",
            title,
            SHUTDOWN_RELEASE_TIMEOUT_S,
            err,
        )
        return
    _LOGGER.info(
        "Released BLE link to %s at shutdown in %.2f s",
        title,
        time.monotonic() - started,
    )


async def _async_release_at_shutdown(
    hass: HomeAssistant, device: BedJet, title: str
) -> None:
    """Per-entry shutdown job: drop the held BLE link while Bluetooth is alive.

    Runs in Home Assistant's first shutdown stage, before the STOP event that
    tears the Bluetooth stack down. Releases the link only: the entry stays
    loaded (no entity churn).
    """
    _latch_shutdown(hass)
    await _async_release_device(device, title)


@callback
def _latch_shutdown(hass: HomeAssistant) -> None:
    """Mark this process as shutting down and cancel every pending resume."""
    hass.data[KEY_SHUTTING_DOWN] = True
    for cancel in hass.data.pop(KEY_RESUME_CANCELS, []):
        cancel()


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register the domain-lifetime shutdown latch.

    Per-entry shutdown jobs are removed when an entry unloads (release_link
    does that), so on their own they could not stop a resume timer firing
    during shutdown. This job lives for the whole run and is never removed.
    """

    async def _latch_job() -> None:
        _latch_shutdown(hass)

    hass.async_add_shutdown_job(HassJob(_latch_job, "bedjet shutdown latch"))
    return True


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
    if hass.data.get(KEY_SHUTTING_DOWN):
        raise ConfigEntryNotReady("Home Assistant is shutting down")
    _async_register_services(hass)
    address: str = entry.data[CONF_ADDRESS]
    service_info = bluetooth.async_last_service_info(hass, address, connectable=True)
    if service_info is None:
        raise ConfigEntryNotReady(
            f"BedJet {address} has not been seen by Bluetooth yet"
        )

    @callback
    def _on_proxy_choice(_scanner_name: str, preferred_used: bool) -> None:
        """Record whether the last connect attempt used the preferred proxy.

        Only fires once a preferred proxy is configured (see
        ble_affinity.make_affinity_client_class); the Connection sensor
        reads `device.via_preferred_proxy` for its diagnostic attribute.
        """
        device.via_preferred_proxy = preferred_used

    # Resolved at call time, not this module's own frozen import: Home
    # Assistant's `bluetooth` component monkeypatches
    # `bleak_retry_connector.BleakClientWithServiceCache` into its own
    # connection-tracking wrapper, and habluetooth ignores whatever
    # BLEDevice is handed to it on every connect - overriding backend
    # selection is the only way to express "connect through this proxy".
    # See ble_affinity.py.
    client_class = make_affinity_client_class(
        bleak_retry_connector.BleakClientWithServiceCache,
        lambda: entry.options.get(CONF_PREFERRED_PROXY) or None,
        on_choice=_on_proxy_choice,
    )

    device = BedJet(
        service_info.device,
        service_info.advertisement,
        source=service_info.source,
        clock=dt_util.now,
        client_class=client_class,
    )

    # Registered immediately - before any further await - because Home
    # Assistant lists the shutdown jobs once at the start of its first
    # shutdown stage: a job added later (e.g. after platform forwarding) would
    # be missed, leaving this device's link to ghost.
    async def _async_shutdown_job() -> None:
        await _async_release_at_shutdown(hass, device, entry.title)

    entry.async_on_unload(
        hass.async_add_shutdown_job(
            HassJob(_async_shutdown_job, f"bedjet release BLE link {entry.title}")
        )
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
    if hass.data.get(KEY_SHUTTING_DOWN):
        # Shutdown began during the await above: undo what was just started
        # (the entry-unload callbacks, including the shutdown job, run when
        # setup fails) and do not set up platforms.
        await coordinator.async_shutdown()
        await _async_release_device(device, entry.title)
        raise ConfigEntryNotReady("Home Assistant is shutting down")

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
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
# Home Assistant can order this itself: every entry registers a shutdown job
# (see async_setup_entry) that releases the link in HA's first shutdown stage,
# before the Bluetooth stack stops, so a plain restart no longer needs help.
# This action stays as the manual/explicit path (e.g. `script.ble_restart_proxy`
# calls it) and also serves an operator who wants the slot freed right now.
# ESPHome 2026.09.14 removed the proxy's on-API-loss release hook, and
# rebooting a proxy is not a cure either - it re-rolls the dice (2026-09-17: 2
# of 6 proxies re-ghosted on their first post-reboot connection). Implemented
# by unloading the entry, because async_unload_entry already runs the
# coordinator's async_shutdown, which unregisters the device callback and drops
# the single connection this device allows. A ghost that forms anyway is
# caught by `automation.ble_ghost_link_detector` and freed with the holding
# proxy's `force_disconnect_orphan` action (HCI disconnect by handle).
#
# `resume_after` sets the entry up again if no restart follows, so an operator
# who calls this and changes their mind is not left with a dead device. Once
# the shutdown job has run, resuming is refused for the rest of the process.
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
        cancels.remove(cancel)
        for entry in released:
            if hass.data.get(KEY_SHUTTING_DOWN):
                return  # shutdown already released the links for good
            if entry.state is ConfigEntryState.LOADED:
                continue  # something set it up already; it owns itself now
            with contextlib.suppress(Exception):
                await hass.config_entries.async_setup(entry.entry_id)
        _LOGGER.info(
            "No restart followed release_link within %s s; links re-established",
            resume_after,
        )

    if hass.data.get(KEY_SHUTTING_DOWN):
        return  # shutdown began while the entries were unloading
    cancels = hass.data.setdefault(KEY_RESUME_CANCELS, [])
    cancel = async_call_later(hass, resume_after, _resume)
    cancels.append(cancel)


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
