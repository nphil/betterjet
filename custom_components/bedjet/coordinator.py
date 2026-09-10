"""Push-driven data update coordinator for the BedJet integration."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
import logging
from time import monotonic
from typing import Protocol

from habluetooth import get_manager

from homeassistant.components.bluetooth import async_scanner_by_source
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    OPTION_LAST_HOLDING_PROXY,
    UNREACHABLE_GRACE_S,
    UNREACHABLE_ISSUE_SUFFIX,
)
from .pybedjet import BedJet, BedJetState

_LOGGER = logging.getLogger(__name__)


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


#: Key under ``hass.data[DOMAIN]`` for the per-address `LinkOutage` records.
DATA_LINK_OUTAGES = "link_outages"


@dataclass(slots=True)
class LinkOutage:
    """One BLE link's outage in progress, keyed by address in ``hass.data``.

    Process-scoped on purpose - not on the coordinator, not on the entry's
    ``runtime_data``. The first cut of this repair kept `_down_since` on the
    coordinator, and a deliberate 21-minute outage on the live instance
    (2026-09-09) never produced the repair: the household autoheal sweep
    reloads any entry whose link is down every 5 minutes, and a reload
    rebuilds the coordinator, so the countdown restarted from zero at 22:35
    and again at 22:40 after a 22:24 drop. A 15-minute threshold measured by
    anything the entry owns is unreachable by construction for exactly as
    long as the device is down. Only the process outlives the reload.

    `since` is `time.monotonic()`, like pybedjet's own timers: a wall-clock
    jump must not fabricate a 15-minute outage.
    """

    since: float
    #: Cancel for the pending deadline timer, or None while none is armed.
    cancel: Callable[[], None] | None = None


def _link_outages(hass: HomeAssistant) -> dict[str, LinkOutage]:
    return hass.data.setdefault(DOMAIN, {}).setdefault(DATA_LINK_OUTAGES, {})


def unreachable_issue_id(address: str) -> str:
    """Repair-issue id for one device's link; one per config entry."""
    return f"{address}{UNREACHABLE_ISSUE_SUFFIX}"


def _entry_for_address(hass: HomeAssistant, address: str) -> ConfigEntry | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_ADDRESS) == address:
            return entry
    return None


@callback
def async_reconcile_link(
    hass: HomeAssistant, address: str, name: str, healthy: bool
) -> None:
    """Bring the `device_unreachable` repair for `address` in line with `healthy`.

    The one place the outage clock is read or written. A healthy link forgets
    the outage and deletes the issue. An unhealthy one starts the clock only
    if none is running - never overwrites it, which is what a reload must not
    be able to do - and then either raises the issue at once, when the link
    has already been down for the whole grace period, or arms the deadline
    for the time *remaining*, never a fresh full window. Arming is idempotent
    so that setup retries and reloads inside the window cannot push the
    deadline out.

    Both registry calls are idempotent, so calling this from entry setup, from
    the not-ready path, from every health edge and from the deadline is safe.
    """
    outages = _link_outages(hass)
    issue_id = unreachable_issue_id(address)
    if healthy:
        if (outage := outages.pop(address, None)) is not None and outage.cancel:
            outage.cancel()
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return

    now = monotonic()
    if (outage := outages.get(address)) is None:
        outage = outages[address] = LinkOutage(since=now)
    remaining = UNREACHABLE_GRACE_S - (now - outage.since)
    if remaining > 0:
        # Nothing pushes a frame while the link is down, so the issue can
        # only be raised by a timer; the countdown is what turns "down right
        # now" into "down continuously for the whole grace period".
        if outage.cancel is None:
            outage.cancel = async_call_later(
                hass, remaining, partial(_async_link_deadline, hass, address)
            )
        return
    if outage.cancel is not None:
        outage.cancel()
        outage.cancel = None
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="device_unreachable",
        translation_placeholders={
            "name": name,
            "address": address,
            "minutes": str(UNREACHABLE_GRACE_S // 60),
        },
    )


@callback
def async_forget_link(hass: HomeAssistant, address: str) -> None:
    """Drop the outage record and any pending deadline for `address`.

    For the entry being removed: the link is no longer anyone's to watch.
    """
    if (outage := _link_outages(hass).pop(address, None)) and outage.cancel:
        outage.cancel()


@callback
def _async_link_deadline(hass: HomeAssistant, address: str, _now: datetime) -> None:
    """The grace period elapsed: re-read the live link and reconcile from it.

    Scheduled on `hass`, never on an entry unload callback: the timer has to
    outlive the entry that armed it. Two callers need that. A reload tears
    the coordinator down mid-countdown (the autoheal does so every 5 minutes
    for as long as the link is down), and an entry stuck in `setup_retry`
    because Home Assistant cannot find the device never had a coordinator or
    an unload callback at all. So what is live is decided here, at the
    moment of firing, not captured when the timer was armed.
    """
    if (outage := _link_outages(hass).get(address)) is None:
        return
    outage.cancel = None
    entry = _entry_for_address(hass, address)
    if entry is None:
        return
    if entry.state is ConfigEntryState.LOADED:
        entry.runtime_data.async_reconcile_unreachable_issue()
    elif entry.state is ConfigEntryState.SETUP_RETRY:
        # Still failing setup, which for this integration means Home
        # Assistant still cannot find the device - the most total outage
        # there is, and the one a coordinator-owned countdown never covered.
        async_reconcile_link(hass, address, entry.title, healthy=False)
    # Anything else (unloading, mid-reload, disabled): nobody is watching the
    # link right now. The clock survives, and whichever setup comes next
    # reconciles from it - raising immediately if the outage is old enough.


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
        # Two different signals, two different consumers. `_was_connected`
        # tracks the GATT link edge for the operator-facing INFO log;
        # `_link_was_up` tracks *health* (connected and streaming) for the
        # repair issue, which must not clear on a link that reconnected but
        # went silent again. The outage clock itself lives in `hass.data`,
        # not here - see `LinkOutage` for the measurement that forced that.
        self._was_connected = device.connected
        self._link_was_up = device.available

    @property
    def available(self) -> bool:
        """Return True while the device is connected and reporting fresh frames."""
        return self.device.available

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

    @property
    def holding_proxy_name(self) -> str | None:
        """Node name of the proxy carrying the link, as ESPHome names it.

        Not `connection_scanner_name`: habluetooth builds a remote scanner's
        ``name`` as "<adapter> (<source>)" (base_scanner.py, ``self.name =
        adapter_human_name(adapter, source) if adapter != source``), which is
        what the Connection sensor should show but is useless for building the
        ``esphome.<node>_restart_proxy`` action name the repair wizard calls.
        Verified live on 2026-09-09: `sensor.master_bedroom_bedjet_connection`
        read ``downstairs-bluetooth-proxy (D4:D4:DA:9D:40:8A)`` while the
        registered action was ``esphome.downstairs_bluetooth_proxy_restart_proxy``
        - slugifying the display name would have looked up
        ``downstairs_bluetooth_proxy_d4_d4_da_9d_40_8a_restart_proxy`` and
        silently dropped the restart step. ``adapter`` is the bare node name.

        Falls back to the source MAC when Home Assistant has no scanner
        registered for it: it will not match any ESPHome action, which is the
        honest answer when the proxy cannot be identified.
        """
        source = self.connection_source
        if source is None:
            return None
        scanner = async_scanner_by_source(self.hass, source)
        return scanner.adapter if scanner is not None else source

    @callback
    def async_reconcile_unreachable_issue(self) -> None:
        """Bring the `device_unreachable` repair in line with the live link.

        Deliberately unconditional: it compares the issue against reality and
        never against a remembered "did we open one?" flag. A sibling BLE
        integration shipped exactly that bug - the delete was gated on an
        in-memory flag that a config-entry reload reset to None, so an issue
        raised at 12:22 was still open 13 hours after the condition cleared
        at 13:00 (2026-09-09), because the reload landed between the two.
        Both registry calls are idempotent, so reconciling from entry setup
        *and* from every health edge is safe, and unconditional reconciliation
        is the only shape that survives a reload.

        "Healthy" here is `device.available` - connected *and* streaming -
        rather than `device.connected`: the BedJet notifies at ~4 Hz for as
        long as a real link is held, so a connection that stopped producing
        frames is a wedged link that the proxy still counts as connected.
        Trusting `connected` (or a populated entity state, which is just as
        stale) would suppress the repair for the exact failure it exists for.

        Conjoining `connection_source is not None` (habluetooth's slot
        accounting) was considered and rejected: a frame can only arrive over
        a live GATT link, so freshness already implies the link, while
        `resolve_connection_source` falls back to the last scanner source
        whenever we are connected - so the conjunction discriminates in no
        reachable state, and it would put a `get_manager()` call (which raises
        before the Bluetooth manager exists) in the path that *clears* the
        repair. The allocation-without-frames case is the ghost link, which
        this predicate already reports as down.
        """
        async_reconcile_link(
            self.hass,
            self.device.address,
            self.name or self.device.address,
            self.available,
        )

    @callback
    def _handle_device_update(self, device: BedJet) -> None:
        """Push the library's latest decoded state into the coordinator.

        State first, then the repair issue, and only then the logging: the
        connect-edge log resolves a scanner name through habluetooth, and
        that diagnostic lookup must never be able to swallow an entity state
        update or the repair reconciliation (pybedjet only logs a callback
        that raises, so anything downstream of a raise is simply lost).
        """
        self.async_set_updated_data(device.state)
        self._handle_link_health_transition()
        self._handle_connection_transition(device)

    @callback
    def _handle_link_health_transition(self) -> None:
        """Reconcile the repair issue whenever the link's health changes.

        Edge-gated only to keep the issue registry from being rewritten four
        times a second at the notify stream's frame rate: `async_get_or_create`
        fires a registry-updated event and schedules a save even when the
        issue already exists. The reconcile it calls consults no remembered
        state, and entry setup calls it unconditionally, so this gate cannot
        strand an issue the way a "previously synced" flag would.
        """
        healthy = self.available
        if healthy == self._link_was_up:
            return
        self._link_was_up = healthy
        self.async_reconcile_unreachable_issue()

    @callback
    def _handle_connection_transition(self, device: BedJet) -> None:
        """Log each connect/disconnect edge once, naming the proxy in use.

        The library itself only knows scanner *sources* (MACs); the scanner
        name lives in Home Assistant's Bluetooth registry, so the INFO line
        operators actually read is emitted from here - and this is also the
        moment the proxy is remembered for the repair wizard. The log keeps
        habluetooth's display name ("<node> (<source>)", which is what the
        Connection sensor shows too) while the wizard needs the bare node
        name; see `holding_proxy_name`.
        """
        connected = device.connected
        if connected == self._was_connected:
            return
        self._was_connected = connected
        if not connected:
            _LOGGER.info("%s: disconnected", device.address)
            return
        _LOGGER.info(
            "%s: connected via %s",
            device.address,
            self.connection_scanner_name or "an unknown scanner",
        )
        self._async_remember_holding_proxy(self.holding_proxy_name)

    @callback
    def _async_remember_holding_proxy(self, proxy_name: str | None) -> None:
        """Persist the proxy now carrying the link into the entry options.

        The repair wizard needs a proxy to offer a restart of, and by the time
        it runs there is nothing live to ask: the whole premise of the repair
        is that no scanner holds this device. The connect edge is the only
        moment the answer can change - a GATT link belongs to the radio that
        opened it, so a link cannot migrate between proxies without a
        disconnect first.

        Written only on change: every write hits config-entry storage and
        wakes the entry's update listeners, and a flapping link must not turn
        into a stream of entry updates.
        """
        entry = self.config_entry
        if proxy_name is None or entry is None:
            return
        if entry.options.get(OPTION_LAST_HOLDING_PROXY) == proxy_name:
            return
        self.hass.config_entries.async_update_entry(
            entry, options={**entry.options, OPTION_LAST_HOLDING_PROXY: proxy_name}
        )

    async def async_shutdown(self) -> None:
        """Stop listening to the device in addition to the base shutdown.

        The unreachable countdown is deliberately *not* cancelled here: it is
        process-scoped (see `LinkOutage`) precisely so that a reload cannot
        restart it, and its deadline re-reads the entry's live state before
        raising anything, so an unloaded entry gets no issue from it.
        """
        self._unregister_device_callback()
        await super().async_shutdown()
