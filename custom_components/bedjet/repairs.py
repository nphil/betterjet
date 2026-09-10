"""Escalating recovery wizard for a BedJet whose Bluetooth link stayed down.

The household's own healing machinery - a heal script plus an hourly re-home -
reclaims almost every dropped link on its own. This is the fallback for when
it does not: the `device_unreachable` repair gets a Fix button that walks the
same ladder an operator would walk by hand, cheapest rung first, and stops the
moment the link is actually back.

Why a proxy restart belongs on that ladder at all: the BedJet accepts exactly
one BLE connection, and its firmware streams notifications at ~4 Hz to
whoever holds it. An ESPHome proxy that has lost the host side of that link
keeps the device's single slot occupied anyway - nothing else can connect, no
advertisement is emitted for anything to notice, and no amount of reloading
this integration can free it. Only the proxy, or mains power, can. That is
also why every rung below finishes by re-checking `coordinator.available`
(connected *and* streaming) instead of trusting that the entities look alive.
"""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import Any

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components import bluetooth
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ADDRESS,
    CONF_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig
from homeassistant.util import slugify

from . import BedJetConfigEntry
from .const import (
    DOMAIN,
    OPTION_LAST_HOLDING_PROXY,
    OPTION_RECOVERY_OUTLET,
    UNREACHABLE_ISSUE_SUFFIX,
)

#: Domain of the ESPHome integration, whose per-device actions are how a
#: Bluetooth proxy is restarted from Home Assistant.
ESPHOME_DOMAIN = "esphome"
#: Suffix of the ESPHome action this looks for. The proxies expose it as a
#: user-defined action named `restart_proxy`, so the scanner named
#: "master-bedroom-bluetooth-proxy" answers to
#: `esphome.master_bedroom_bluetooth_proxy_restart_proxy`.
RESTART_PROXY_ACTION_SUFFIX = "_restart_proxy"

#: Entry states this ladder can act on. SETUP_RETRY belongs here as much as
#: LOADED: a BedJet that is silent when Home Assistant starts fails setup with
#: ConfigEntryNotReady and keeps retrying, which is exactly when someone
#: reaches for this Fix button - and both `reload` and `power_cycle` work on an
#: entry that never loaded. Measured 2026-09-09 on the live instance: an
#: AC Infinity sibling sat in setup_retry ("Could not find ... device with
#: address") while its radio was silent to all seven proxies, so the mains rung
#: was the only one that could have helped and an abort here withheld it.
#: Anything else - disabled, a setup error, a failed unload - needs a decision
#: from the operator that no amount of restarting can supply.
ACTIONABLE_STATES = (ConfigEntryState.LOADED, ConfigEntryState.SETUP_RETRY)

#: How long each rung waits for the link to come back before admitting it did
#: not work. A reachable BedJet answers within a second or two of a connect
#: attempt, but pybedjet's supervisor backs off between attempts, so allow for
#: a retry or two rather than only the first one.
RECOVERY_TIMEOUT_S = 45.0
#: Longer for the power cycle: a BedJet that lost mains power has to boot and
#: start advertising again before anything can even try to connect.
POWER_CYCLE_TIMEOUT_S = 60.0
#: Mains-off dwell - long enough for the BedJet's radio to actually lose power
#: and for the proxy to give up its half of the dead link.
POWER_CYCLE_OFF_S = 10.0
#: Poll granularity while waiting. The link returns through pybedjet's own
#: supervisor, so there is nothing here to await; sleeping in small slices
#: keeps the event loop free instead of blocking it for the whole timeout.
POLL_INTERVAL_S = 1.0


class BleRecoveryFixFlow(RepairsFlow):
    """Walk the recovery ladder for one BedJet, cheapest rung first."""

    def __init__(self, address: str) -> None:
        """Initialize the flow for the device the issue id names."""
        self.address = address
        #: What the previous rung of *this* flow achieved, rendered into the
        #: menu text. Empty (never the literal "None") on first entry, so the
        #: operator is not shown a stray placeholder before trying anything.
        self._last_result = ""

    # -- live state ----------------------------------------------------------

    def _entry(self) -> BedJetConfigEntry | None:
        """Re-resolve the config entry, freshly, on every use.

        A reload replaces `runtime_data` wholesale - and a reload is the first
        rung of this ladder - so anything captured when the flow started would
        describe a coordinator that no longer exists.
        """
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_ADDRESS) == self.address:
                return entry
        return None

    def _link_is_up(self) -> bool:
        """True when this BedJet's link is established *and* streaming.

        `coordinator.available`, not `device.connected`: a connected-but-silent
        link is precisely the failure being repaired here, so treating it as
        success would have every rung of the ladder report a false victory.

        With no coordinator there is nothing to ask, and reporting False
        forever would make every rung fail for an entry in SETUP_RETRY - the
        state this ladder was extended to serve. The honest substitute is the
        same signal setup itself blocks on: a connectable advertisement. If the
        BedJet is heard again, setup's own retry (or the `reload` rung) will
        load the entry, and the next poll sees the coordinator.
        """
        entry = self._entry()
        if entry is None:
            return False
        if entry.state is ConfigEntryState.LOADED:
            return entry.runtime_data.available
        return self._is_advertising()

    def _is_advertising(self) -> bool:
        """True when Home Assistant has a connectable advertisement for us.

        Deliberately the identical call `async_setup_entry` fails on, so this
        cannot disagree with the condition that raised the repair.
        """
        last_seen = getattr(bluetooth, "async_last_service_info", None)
        if last_seen is None:  # pragma: no cover - very old Home Assistant
            return False
        try:
            return last_seen(self.hass, self.address, connectable=True) is not None
        except Exception:  # noqa: BLE001 - no manager before bluetooth is set up
            return False

    def _link_state(self) -> str:
        """One line describing the link, for the menu text."""
        entry = self._entry()
        if entry is None:
            return "its configuration entry is gone"
        if entry.state is not ConfigEntryState.LOADED:
            if self._is_advertising():
                return "advertising again, but the integration has not loaded yet"
            return "not loaded, and not advertising to any proxy"
        coordinator = entry.runtime_data
        if not coordinator.available:
            return "no Bluetooth link"
        return f"connected via {coordinator.holding_proxy_name or 'an unknown proxy'}"

    def _device_name(self) -> str:
        """The device's name as configured, or its address as a last resort."""
        entry = self._entry()
        return entry.title if entry is not None else self.address

    def _proxy_restart_action(self) -> str | None:
        """The ESPHome action that reboots the proxy this BedJet last used.

        A live hold wins when there is one, but by definition there usually is
        not - which is why the coordinator persists the last proxy it saw into
        the entry options while the link is up. Returns None when no proxy is
        known, or when the one that is known exposes no restart action (stock
        proxy firmware without the `restart_proxy` API action, or a node
        renamed since the link was last held).

        `has_service` rather than `async_services()`: core's own docstring
        says the latter copies the whole service registry and is expensive,
        and this is re-derived every time the menu is drawn.
        """
        entry = self._entry()
        if entry is None:
            return None
        proxy: str | None = None
        if entry.state is ConfigEntryState.LOADED:
            proxy = entry.runtime_data.holding_proxy_name
        if not (proxy := proxy or entry.options.get(OPTION_LAST_HOLDING_PROXY)):
            return None
        action = f"{slugify(proxy)}{RESTART_PROXY_ACTION_SUFFIX}"
        if self.hass.services.has_service(ESPHOME_DOMAIN, action):
            return action
        return None

    # -- menu ----------------------------------------------------------------

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Check there is still something to repair, then show the ladder."""
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        if entry.state not in ACTIONABLE_STATES:
            return self.async_abort(reason="not_loaded")
        return await self.async_step_menu()

    async def async_step_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Offer the recovery rungs, cheapest first, mains-cutting one last."""
        menu_options = ["recheck", "reload"]
        if self._proxy_restart_action() is not None:
            menu_options.append("restart_proxy")
        menu_options.append("power_cycle")
        return self.async_show_menu(
            step_id="menu",
            menu_options=menu_options,
            description_placeholders={
                "name": self._device_name(),
                "link_state": self._link_state(),
                "last_result": self._last_result,
            },
        )

    # -- rungs ---------------------------------------------------------------

    async def async_step_recheck(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Look again and nothing else - free, and links do come back alone."""
        if self._link_is_up():
            return await self._async_finish()
        self._last_result = "Checked again: still no Bluetooth link."
        return await self.async_step_menu()

    async def async_step_reload(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Reload the config entry, which rebuilds the client and reconnects."""
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        await self.hass.config_entries.async_reload(entry.entry_id)
        return await self._async_settle("Reloaded the integration", RECOVERY_TIMEOUT_S)

    async def async_step_restart_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Ask the proxy that last held the link to reboot.

        Reported honestly as a request, never as a reboot: the proxy firmware
        refuses to restart within 20 minutes of booting (it logs "refused: up
        only N s" and carries on), so this rung can legitimately do nothing at
        all.
        """
        action = self._proxy_restart_action()
        if action is None:
            self._last_result = (
                "No Bluetooth proxy could be identified, so nothing was restarted."
            )
            return await self.async_step_menu()
        await self.hass.services.async_call(ESPHOME_DOMAIN, action, blocking=True)
        return await self._async_settle(
            "Asked the proxy to restart (it refuses if it booted less than "
            "20 minutes ago, so it may not have)",
            RECOVERY_TIMEOUT_S,
        )

    async def async_step_power_cycle(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Cut and restore mains power to the BedJet through a chosen switch.

        The switch is remembered in the entry options because there is no
        smart plug wired to this BedJet today: whichever outlet the operator
        ends up using, the wizard should only have to be told once.
        """
        entry = self._entry()
        if entry is None:
            return self.async_abort(reason="entry_not_found")

        if user_input is None:
            remembered = entry.options.get(OPTION_RECOVERY_OUTLET)
            key = (
                vol.Required(CONF_ENTITY_ID, default=remembered)
                if remembered
                else vol.Required(CONF_ENTITY_ID)
            )
            return self.async_show_form(
                step_id="power_cycle",
                data_schema=vol.Schema(
                    {key: EntitySelector(EntitySelectorConfig(domain=Platform.SWITCH))}
                ),
                description_placeholders={"name": self._device_name()},
            )

        outlet: str = user_input[CONF_ENTITY_ID]
        if entry.options.get(OPTION_RECOVERY_OUTLET) != outlet:
            self.hass.config_entries.async_update_entry(
                entry, options={**entry.options, OPTION_RECOVERY_OUTLET: outlet}
            )
        await self.hass.services.async_call(
            Platform.SWITCH, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: outlet}, blocking=True
        )
        await asyncio.sleep(POWER_CYCLE_OFF_S)
        await self.hass.services.async_call(
            Platform.SWITCH, SERVICE_TURN_ON, {ATTR_ENTITY_ID: outlet}, blocking=True
        )
        return await self._async_settle(f"Power-cycled {outlet}", POWER_CYCLE_TIMEOUT_S)

    # -- shared tail ---------------------------------------------------------

    async def _async_settle(
        self, tried: str, timeout: float
    ) -> data_entry_flow.FlowResult:
        """Wait for the link to come back, then finish or fall back to the menu.

        Patience is measured against the clock rather than counted in sleeps,
        so the health check's own cost and any sleep overshoot come out of the
        budget instead of quietly extending it.
        """
        deadline = monotonic() + timeout
        while not self._link_is_up():
            if (remaining := deadline - monotonic()) <= 0:
                self._last_result = (
                    f"{tried}; the link did not come back within {timeout:g} seconds."
                )
                return await self.async_step_menu()
            await asyncio.sleep(min(POLL_INTERVAL_S, remaining))
        return await self._async_finish()

    async def _async_finish(self) -> data_entry_flow.FlowResult:
        """The link is back: drop the issue from both sides.

        Home Assistant deletes the issue behind a completed fix flow, but the
        integration's own reconciliation is what keeps the registry honest
        across reloads, so ask it to agree now rather than letting the two
        views drift until the next health edge.
        """
        entry = self._entry()
        if entry is not None and entry.state is ConfigEntryState.LOADED:
            entry.runtime_data.async_reconcile_unreachable_issue()
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Build the fix flow for a `device_unreachable` issue."""
    return BleRecoveryFixFlow(issue_id.removesuffix(UNREACHABLE_ISSUE_SUFFIX))
