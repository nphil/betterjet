"""Tests for the `device_unreachable` repair and its recovery wizard.

Two contracts live here, and they are the reason this feature exists:

1. The issue tracks *reality*, never a remembered flag. A sibling BLE
   integration gated its `async_delete_issue` on an in-memory "previously
   synced" boolean that a config-entry reload reset, so an issue raised at
   12:22 was still open 13 hours after the condition cleared at 13:00. The
   setup-time reconciliation test below fails if that gate ever comes back.
2. The wizard only offers a proxy restart when there is a proxy *and* an
   ESPHome action to restart it with - otherwise the Fix button would show
   the operator a rung that cannot be climbed.

Everything asserted here is something an operator or the issue registry can
observe: which issues are open, which flow step comes back, what ends up in
the config entry's options. Never the translated wording, and never the order
in which the integration made its (idempotent) registry calls.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import custom_components.bedjet as bedjet_init
from custom_components.bedjet.const import (
    DOMAIN,
    OPTION_LAST_HOLDING_PROXY,
    OPTION_RECOVERY_OUTLET,
    UNREACHABLE_GRACE_S,
)
import custom_components.bedjet.coordinator as coordinator_module
from custom_components.bedjet.coordinator import BedJetCoordinator
import custom_components.bedjet.repairs as repairs_module
from custom_components.bedjet.repairs import BleRecoveryFixFlow, async_create_fix_flow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ADDRESS, CONF_ENTITY_ID
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir

# Live values, read off the running instance on 2026-09-09: the BedJet at this
# address was held by the proxy whose ESPHome node is `downstairs-bluetooth-
# proxy` through scanner source D4:D4:DA:9D:40:8A, and that node registers
# `esphome.downstairs_bluetooth_proxy_restart_proxy`. Using the real strings
# here is what makes the gating test meaningful - the display name habluetooth
# builds from them slugifies to something else entirely.
ADDRESS = "FC:F5:C4:20:1A:92"
ISSUE_ID = f"{ADDRESS}_unreachable"
SCANNER_SOURCE = "D4:D4:DA:9D:40:8A"
PROXY_NODE = "downstairs-bluetooth-proxy"
RESTART_ACTION = "downstairs_bluetooth_proxy_restart_proxy"
OUTLET = "switch.bedjet_outlet"

SERVICE_INFO = SimpleNamespace(
    device=object(), advertisement=object(), source=SCANNER_SOURCE
)
ALLOCATION = SimpleNamespace(
    source=SCANNER_SOURCE, slots=3, free=2, allocated=[ADDRESS.lower()]
)


# --- fakes ------------------------------------------------------------------


class FakeIssueRegistry:
    """Records what the integration asked the repair registry to hold.

    Only the *contents* are asserted on: both registry calls are idempotent in
    real Home Assistant, so how many times each was made says nothing about
    what an operator would see.
    """

    IssueSeverity = ir.IssueSeverity

    def __init__(self, *open_issue_ids: str) -> None:
        self.issues: dict[str, dict[str, Any]] = {
            issue_id: {} for issue_id in open_issue_ids
        }

    def async_create_issue(self, hass, domain, issue_id, **kwargs) -> None:
        assert domain == DOMAIN
        self.issues[issue_id] = kwargs

    def async_delete_issue(self, hass, domain, issue_id) -> None:
        assert domain == DOMAIN
        self.issues.pop(issue_id, None)


class FakeClock:
    """Monotonic clock the test winds forward by hand."""

    def __init__(self) -> None:
        self.seconds = 0.0

    def __call__(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> None:
        self.seconds += seconds


class FakeTimers:
    """Stand-in for `async_call_later`, so a test decides when time passes.

    Each timer is stored by its absolute deadline on the fake clock, not by
    the delay it was armed with: the failure this file exists to catch is a
    countdown re-armed for a fresh full window on every reload, and a fake
    that fired whatever was scheduled, whenever asked, could not tell that
    apart from the right behaviour.
    """

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.scheduled: list[tuple[float, Any]] = []

    @property
    def due(self) -> list[float]:
        """Absolute deadlines of every pending timer."""
        return [due for due, _action in self.scheduled]

    def call_later(self, hass, delay, action):
        item = (self._clock() + delay, action)
        self.scheduled.append(item)

        def _cancel() -> None:
            if item in self.scheduled:
                self.scheduled.remove(item)

        return _cancel

    def fire(self) -> None:
        """Fire the last-armed timer regardless of its deadline."""
        _due, action = self.scheduled.pop()
        action(None)

    def fire_due(self) -> None:
        """Fire every timer whose deadline the clock has reached."""
        now = self._clock()
        ready = [item for item in self.scheduled if item[0] <= now]
        for item in ready:
            self.scheduled.remove(item)
            item[1](None)


class FakeDevice:
    """The narrow slice of pybedjet.BedJet the integration touches."""

    def __init__(self, *, available: bool = False, connected: bool = False) -> None:
        self.address = ADDRESS
        self.available = available
        self.connected = connected
        self.scanner_source = SCANNER_SOURCE
        self.state = None
        self.started = False
        self.stopped = False
        self._callbacks: list = []

    def register_callback(self, callback):
        self._callbacks.append(callback)
        return lambda: self._callbacks.remove(callback)

    def push(self, state=None) -> None:
        self.state = state
        for callback in list(self._callbacks):
            callback(self)

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def set_ble_device_and_advertisement_data(
        self, device, adv, *, source=None
    ) -> None:
        pass


class FakeServices:
    def __init__(self, esphome_actions: tuple[str, ...] = ()) -> None:
        self._registered = {f"esphome.{action}" for action in esphome_actions}
        self.calls: list[tuple[str, str, dict | None]] = []

    def has_service(self, domain, service) -> bool:
        return f"{domain}.{service}" in self._registered

    async def async_call(self, domain, service, data=None, blocking=False) -> None:
        self.calls.append((str(domain), str(service), data))


class FakeConfigEntries:
    def __init__(self) -> None:
        self.entries: list[FakeEntry] = []
        self.forwarded: list[tuple] = []
        self.reloaded: list[str] = []
        self.option_writes: list[dict[str, Any]] = []

    def async_entries(self, domain=None) -> list[FakeEntry]:
        return list(self.entries)

    async def async_forward_entry_setups(self, entry, platforms) -> None:
        self.forwarded.append((entry, tuple(platforms)))

    async def async_unload_platforms(self, entry, platforms) -> bool:
        return True

    async def async_reload(self, entry_id) -> None:
        self.reloaded.append(entry_id)

    def async_update_entry(self, entry, *, options=None, **kwargs) -> None:
        if options is not None:
            self.option_writes.append(options)
            entry.options = options


class FakeBus:
    def async_listen_once(self, event_type, callback):
        return lambda: None


class FakeHass:
    def __init__(self, esphome_actions: tuple[str, ...] = ()) -> None:
        self.bus = FakeBus()
        self.config_entries = FakeConfigEntries()
        self.services = FakeServices(esphome_actions)
        self.data: dict[str, Any] = {}


class FakeEntry:
    def __init__(self, *, options: dict[str, Any] | None = None) -> None:
        self.entry_id = "01JBEDJET"
        self.data = {CONF_ADDRESS: ADDRESS}
        self.options = options or {}
        self.title = "Bedjetty"
        self.state = ConfigEntryState.LOADED
        self.runtime_data = None

    def async_on_unload(self, callback) -> None:
        pass


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def registry(monkeypatch) -> FakeIssueRegistry:
    fake = FakeIssueRegistry()
    monkeypatch.setattr(coordinator_module, "ir", fake)
    return fake


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(coordinator_module, "monotonic", fake)
    return fake


@pytest.fixture
def timers(monkeypatch, clock) -> FakeTimers:
    fake = FakeTimers(clock)
    monkeypatch.setattr(coordinator_module, "async_call_later", fake.call_later)
    return fake


@pytest.fixture
def holding_proxy(monkeypatch) -> list[Any]:
    """Report the link as held by the downstairs proxy, as habluetooth would.

    The scanner's shape is the live one, not a convenience: habluetooth sets
    `name` to "<adapter> (<source>)" for any remote scanner, which is what
    `sensor.master_bedroom_bedjet_connection` was observed reporting on
    2026-09-09, while the registered ESPHome action was derived from the bare
    node name in `adapter`.

    Returns the live allocation list so a test can empty it - `resolve_
    connection_source` answers from the allocations before it ever looks at
    `connected`, so a test that only flips the device's flags still reports a
    live hold, and would never reach the remembered-proxy branch.
    """
    allocations: list[Any] = [ALLOCATION]
    monkeypatch.setattr(
        coordinator_module,
        "get_manager",
        lambda: SimpleNamespace(async_current_allocations=lambda: list(allocations)),
    )
    monkeypatch.setattr(
        coordinator_module,
        "async_scanner_by_source",
        lambda hass, source: SimpleNamespace(
            name=f"{PROXY_NODE} ({SCANNER_SOURCE})", adapter=PROXY_NODE
        ),
    )
    return allocations


@pytest.fixture
def no_holding_proxy(monkeypatch) -> None:
    """Nothing holds the link - the normal state while the repair is open."""
    monkeypatch.setattr(
        coordinator_module,
        "get_manager",
        lambda: SimpleNamespace(async_current_allocations=lambda: ()),
    )


@pytest.fixture(autouse=True)
def _collapse_wizard_waiting(monkeypatch) -> None:
    """Wind the wizard's poll windows down to nothing.

    What is under test is which branch each rung takes, not how patiently it
    waits; left alone, one test run would sit through minutes of real sleeps.
    """
    monkeypatch.setattr(repairs_module, "RECOVERY_TIMEOUT_S", 0.0)
    monkeypatch.setattr(repairs_module, "POWER_CYCLE_TIMEOUT_S", 0.0)
    monkeypatch.setattr(repairs_module, "POWER_CYCLE_OFF_S", 0.0)
    monkeypatch.setattr(repairs_module, "POLL_INTERVAL_S", 0.0)


# --- helpers ----------------------------------------------------------------


def run_setup(
    monkeypatch,
    hass: FakeHass,
    entry: FakeEntry,
    device: FakeDevice,
    *,
    seen: bool = True,
) -> bool:
    """Run async_setup_entry against a prepared device.

    `seen=False` is the one not-ready case: Bluetooth has never heard this
    address, so there is no service info to build a device from.
    """
    monkeypatch.setattr(bedjet_init, "BedJet", lambda *args, **kwargs: device)
    monkeypatch.setattr(
        bedjet_init.bluetooth,
        "async_last_service_info",
        lambda hass, address, connectable=True: SERVICE_INFO if seen else None,
    )
    monkeypatch.setattr(
        bedjet_init.bluetooth, "async_register_callback", lambda *a, **k: lambda: None
    )
    return asyncio.run(bedjet_init.async_setup_entry(hass, entry))


def reload(monkeypatch, hass: FakeHass, entry: FakeEntry) -> FakeDevice:
    """Reload the entry the way Home Assistant does: unload, then set up afresh.

    Returns the new `BedJet` the fresh setup built. It is not streaming yet,
    exactly as in production: `start()` only spawns the connect loop.
    """
    asyncio.run(bedjet_init.async_unload_entry(hass, entry))
    asyncio.run(entry.runtime_data.async_shutdown())
    entry.state = ConfigEntryState.NOT_LOADED
    device = FakeDevice(available=False)
    assert run_setup(monkeypatch, hass, entry, device) is True
    entry.state = ConfigEntryState.LOADED
    return device


def make_coordinator(
    hass: FakeHass, entry: FakeEntry, device: FakeDevice
) -> BedJetCoordinator:
    coordinator = BedJetCoordinator(hass, entry, device)
    entry.runtime_data = coordinator
    hass.config_entries.entries.append(entry)
    return coordinator


def make_flow(hass: FakeHass) -> BleRecoveryFixFlow:
    flow = asyncio.run(async_create_fix_flow(hass, ISSUE_ID, None))
    flow.hass = hass
    return flow


# --- the issue itself -------------------------------------------------------


class TestUnreachableIssue:
    """The issue must follow the link, not the integration's memory of it."""

    def test_a_reload_mid_outage_does_not_strand_the_issue(
        self, monkeypatch, registry, timers, clock, holding_proxy
    ) -> None:
        """The orphan bug, in the sequence production actually produces.

        `async_setup_entry` always builds a fresh `BedJet` and `start()` only
        spawns the connect loop, so the device is never streaming yet at the
        setup-time reconcile - the reload leaves the issue standing (correct,
        the outage is still on) and the *first frame* is what has to clear it.
        A fresh coordinator remembers nothing about having raised it, so a
        delete gated on any remembered state leaves this open forever, which
        is exactly how a sibling integration kept an issue alive for 13 hours.
        """
        registry.issues[ISSUE_ID] = {}
        hass = FakeHass()
        entry = FakeEntry()
        device = FakeDevice(available=False)

        assert run_setup(monkeypatch, hass, entry, device) is True
        assert ISSUE_ID in registry.issues, "setup must not pretend an outage ended"

        device.connected = True
        device.available = True
        device.push()

        assert ISSUE_ID not in registry.issues

    def test_reconcile_deletes_an_issue_it_never_saw_created(
        self, registry, timers, clock, holding_proxy
    ) -> None:
        # The same rule at the unit level, in the state a post-reload
        # coordinator is in: healthy link, no outage on record, no memory of
        # an issue. Reality is the only input the delete is allowed to have.
        hass = FakeHass()
        make_coordinator(hass, FakeEntry(), FakeDevice(available=True, connected=True))
        registry.issues[ISSUE_ID] = {}

        hass.config_entries.entries[0].runtime_data.async_reconcile_unreachable_issue()

        assert ISSUE_ID not in registry.issues

    def test_setup_keeps_an_issue_the_link_still_justifies(
        self, monkeypatch, registry, timers, clock, no_holding_proxy
    ) -> None:
        # Same reload, but the outage is ongoing: the issue must survive, and
        # the countdown must be re-armed rather than the issue re-raised
        # immediately (setup cannot know how long it has really been down).
        registry.issues[ISSUE_ID] = {}
        hass = FakeHass()

        run_setup(monkeypatch, hass, FakeEntry(), FakeDevice(available=False))

        assert ISSUE_ID in registry.issues
        assert timers.due == [UNREACHABLE_GRACE_S]

    def test_issue_is_raised_only_once_the_grace_period_has_fully_elapsed(
        self, registry, timers, clock, no_holding_proxy
    ) -> None:
        hass = FakeHass()
        coordinator = make_coordinator(hass, FakeEntry(), FakeDevice(available=False))

        coordinator.async_reconcile_unreachable_issue()
        assert registry.issues == {}, "a link that just dropped is not a repair yet"

        clock.advance(UNREACHABLE_GRACE_S - 1)
        timers.fire()
        assert registry.issues == {}, "one second short is still not 15 minutes"
        clock.advance(1)
        timers.fire()

        assert ISSUE_ID in registry.issues

    def test_reconnecting_deletes_the_issue(
        self, registry, timers, clock, holding_proxy
    ) -> None:
        hass = FakeHass()
        device = FakeDevice(available=False)
        coordinator = make_coordinator(hass, FakeEntry(), device)
        coordinator.async_reconcile_unreachable_issue()
        clock.advance(UNREACHABLE_GRACE_S)
        timers.fire()
        assert ISSUE_ID in registry.issues

        device.available = True
        device.connected = True
        device.push()

        assert registry.issues == {}
        assert timers.scheduled == [], "the countdown must not outlive the outage"

    def test_a_silent_connection_is_not_treated_as_a_working_link(
        self, registry, timers, clock, holding_proxy
    ) -> None:
        # The BedJet streams status ~4 Hz whenever a real link is held, so a
        # connection that produces no frames is a wedged link holding the
        # device's only slot - exactly what this repair exists to surface.
        hass = FakeHass()
        device = FakeDevice(available=False, connected=True)
        coordinator = make_coordinator(hass, FakeEntry(), device)

        coordinator.async_reconcile_unreachable_issue()
        clock.advance(UNREACHABLE_GRACE_S)
        timers.fire()

        assert ISSUE_ID in registry.issues

    def test_reloads_inside_the_window_do_not_restart_the_countdown(
        self, monkeypatch, registry, timers, clock, no_holding_proxy
    ) -> None:
        """The measured failure, replayed: the repair never came.

        Live instance, 2026-09-09, a deliberate 21-minute power cut of a
        sibling BLE device that the household autoheal sweep also dispatches
        to - it reloads any entry whose link is down every 5 minutes:

            22:24:37  link drop       -> countdown armed
            22:35:00  autoheal reload -> fresh coordinator, countdown at zero
            22:40:00  autoheal reload -> fresh coordinator, countdown at zero

        Anything measured by an object the entry owns restarts on every one
        of those reloads, so 15 continuous minutes can never be observed for
        as long as the device is down. The deadline must stay where the
        first drop put it, and the issue must be open 15 minutes after that
        drop - not 15 minutes after the last reload.
        """
        hass = FakeHass()
        entry = FakeEntry()
        hass.config_entries.entries.append(entry)
        device = FakeDevice(available=True, connected=True)
        assert run_setup(monkeypatch, hass, entry, device) is True

        device.available = False
        device.connected = False
        device.push()
        dropped_at = clock()
        assert registry.issues == {}

        for _sweep in range(2):
            clock.advance(5 * 60)
            reload(monkeypatch, hass, entry)
            assert registry.issues == {}, "10 minutes is not yet 15"
            assert timers.due == [dropped_at + UNREACHABLE_GRACE_S], (
                "a reload must neither restart the countdown nor add a second one"
            )

        clock.advance(5 * 60)
        timers.fire_due()

        assert ISSUE_ID in registry.issues

    def test_a_device_bluetooth_never_hears_gets_the_repair(
        self, monkeypatch, registry, timers, clock, no_holding_proxy
    ) -> None:
        """The not-ready path: the most total outage, and the one never covered.

        A device that stopped advertising has no service info, so setup
        raises `ConfigEntryNotReady` before a coordinator exists; anything a
        coordinator would have armed never starts. Home Assistant retries
        setup on a backoff for as long as that lasts (a sibling integration's
        entry sat in `setup_retry` overnight this way on 2026-09-09), so the
        countdown started on the first attempt must survive every retry and
        the retries must not move its deadline.
        """
        hass = FakeHass()
        entry = FakeEntry()
        hass.config_entries.entries.append(entry)
        first_attempt = clock()
        for backoff in (0, 10, 20, 40, 80):
            clock.advance(backoff)
            with pytest.raises(ConfigEntryNotReady):
                run_setup(monkeypatch, hass, entry, FakeDevice(), seen=False)
            entry.state = ConfigEntryState.SETUP_RETRY
            assert timers.due == [first_attempt + UNREACHABLE_GRACE_S]
        assert registry.issues == {}

        clock.seconds = first_attempt + UNREACHABLE_GRACE_S
        timers.fire_due()

        assert ISSUE_ID in registry.issues

        # The device reappears: the retry that finally succeeds builds a
        # coordinator, and its first frame is what clears the repair.
        device = FakeDevice(available=False)
        assert run_setup(monkeypatch, hass, entry, device) is True
        entry.state = ConfigEntryState.LOADED
        assert ISSUE_ID in registry.issues, "setup must not pretend an outage ended"
        device.available = True
        device.connected = True
        device.push()

        assert registry.issues == {}
        assert timers.scheduled == []

    def test_a_deadline_firing_after_unload_raises_nothing(
        self, registry, timers, clock, no_holding_proxy
    ) -> None:
        # The countdown outlives the entry on purpose (that is what makes it
        # reload-proof), so what must hold instead is that a deadline reached
        # while the entry is not loaded raises no issue whose Fix button has
        # nothing left to act on.
        hass = FakeHass()
        entry = FakeEntry()
        coordinator = make_coordinator(hass, entry, FakeDevice(available=False))
        coordinator.async_reconcile_unreachable_issue()

        asyncio.run(coordinator.async_shutdown())
        entry.state = ConfigEntryState.NOT_LOADED
        clock.advance(UNREACHABLE_GRACE_S)
        timers.fire_due()

        assert registry.issues == {}

    def test_removing_the_entry_forgets_the_outage(
        self, monkeypatch, registry, timers, clock, no_holding_proxy
    ) -> None:
        # Removal is the one lifecycle event that must forget the clock: the
        # same address set up again later is a new device, not a 20-minute
        # outage, and no deadline may fire for an entry that is gone.
        monkeypatch.setattr(bedjet_init, "ir", registry)
        hass = FakeHass()
        entry = FakeEntry()
        coordinator = make_coordinator(hass, entry, FakeDevice(available=False))
        coordinator.async_reconcile_unreachable_issue()
        clock.advance(UNREACHABLE_GRACE_S + 5 * 60)

        asyncio.run(bedjet_init.async_remove_entry(hass, entry))
        hass.config_entries.entries.clear()
        timers.fire_due()
        assert registry.issues == {}

        make_coordinator(
            hass, FakeEntry(), FakeDevice(available=False)
        ).async_reconcile_unreachable_issue()

        assert registry.issues == {}
        assert timers.due == [clock() + UNREACHABLE_GRACE_S]


class TestHoldingProxyMemory:
    """While the link is up, remember the proxy carrying it - and only then."""

    def test_connecting_records_the_holding_proxy_without_rewriting_it(
        self, registry, timers, clock, holding_proxy
    ) -> None:
        hass = FakeHass()
        entry = FakeEntry()
        device = FakeDevice()
        make_coordinator(hass, entry, device)

        device.connected = True
        device.available = True
        device.push()
        assert entry.options[OPTION_LAST_HOLDING_PROXY] == PROXY_NODE

        # A flapping link reconnecting to the same proxy must not turn into a
        # stream of config-entry writes.
        device.connected = False
        device.available = False
        device.push()
        device.connected = True
        device.available = True
        device.push()

        assert hass.config_entries.option_writes == [
            {OPTION_LAST_HOLDING_PROXY: PROXY_NODE}
        ]


# --- the wizard -------------------------------------------------------------


class TestRestartProxyGating:
    """Offer the proxy restart only when it can actually be performed."""

    def test_offered_when_the_remembered_proxy_has_a_restart_action(
        self, registry, timers, clock, no_holding_proxy
    ) -> None:
        hass = FakeHass(esphome_actions=(RESTART_ACTION,))
        entry = FakeEntry(options={OPTION_LAST_HOLDING_PROXY: PROXY_NODE})
        make_coordinator(hass, entry, FakeDevice(available=False))

        result = asyncio.run(make_flow(hass).async_step_init())

        assert result["type"] == "menu"
        assert result["menu_options"] == [
            "recheck",
            "reload",
            "restart_proxy",
            "power_cycle",
        ]

    def test_a_remembered_proxy_resolves_the_action_its_node_registers(
        self, registry, timers, clock, holding_proxy
    ) -> None:
        # End to end over the live shapes: a connect edge remembers the proxy,
        # the proxy then releases the slot, and the menu must still offer the
        # restart of `esphome.downstairs_bluetooth_proxy_restart_proxy` from
        # what was remembered. Slugifying habluetooth's display name would look
        # up `downstairs_bluetooth_proxy_d4_d4_da_9d_40_8a_restart_proxy` and
        # silently drop the rung on real hardware.
        hass = FakeHass(esphome_actions=(RESTART_ACTION,))
        entry = FakeEntry()
        device = FakeDevice()
        coordinator = make_coordinator(hass, entry, device)
        device.connected = True
        device.available = True
        device.push()
        assert entry.options[OPTION_LAST_HOLDING_PROXY] == PROXY_NODE

        device.connected = False
        device.available = False
        device.push()
        holding_proxy.clear()  # the proxy gave the slot back
        assert coordinator.holding_proxy_name is None, "nothing may still be live"

        result = asyncio.run(make_flow(hass).async_step_init())

        assert "restart_proxy" in result["menu_options"]

    def test_hidden_when_no_proxy_is_known(
        self, registry, timers, clock, no_holding_proxy
    ) -> None:
        hass = FakeHass(esphome_actions=(RESTART_ACTION,))
        make_coordinator(hass, FakeEntry(), FakeDevice(available=False))

        result = asyncio.run(make_flow(hass).async_step_init())

        assert result["menu_options"] == ["recheck", "reload", "power_cycle"]

    def test_hidden_when_the_proxy_exposes_no_restart_action(
        self, registry, timers, clock, no_holding_proxy
    ) -> None:
        # Stock ESPHome firmware without the user-defined action, or a proxy
        # renamed since the link was last held.
        hass = FakeHass(esphome_actions=("some_other_proxy_restart_proxy",))
        entry = FakeEntry(options={OPTION_LAST_HOLDING_PROXY: PROXY_NODE})
        make_coordinator(hass, entry, FakeDevice(available=False))

        result = asyncio.run(make_flow(hass).async_step_init())

        assert result["menu_options"] == ["recheck", "reload", "power_cycle"]


class TestWizardSteps:
    def test_the_menu_reports_nothing_tried_before_anything_is_tried(
        self, registry, timers, clock, no_holding_proxy
    ) -> None:
        hass = FakeHass()
        make_coordinator(hass, FakeEntry(), FakeDevice(available=False))

        result = asyncio.run(make_flow(hass).async_step_init())

        assert result["description_placeholders"]["last_result"] == ""

    def test_a_rung_that_did_not_help_returns_to_the_menu_saying_so(
        self, registry, timers, clock, no_holding_proxy
    ) -> None:
        hass = FakeHass(esphome_actions=(RESTART_ACTION,))
        entry = FakeEntry(options={OPTION_LAST_HOLDING_PROXY: PROXY_NODE})
        make_coordinator(hass, entry, FakeDevice(available=False))
        flow = make_flow(hass)

        result = asyncio.run(flow.async_step_restart_proxy())

        assert hass.services.calls == [("esphome", RESTART_ACTION, None)]
        assert result["type"] == "menu"
        assert result["description_placeholders"]["last_result"] != ""

    def test_a_recovered_link_completes_the_flow_and_clears_the_issue(
        self, registry, timers, clock, holding_proxy
    ) -> None:
        hass = FakeHass()
        entry = FakeEntry()
        make_coordinator(hass, entry, FakeDevice(available=True, connected=True))
        registry.issues[ISSUE_ID] = {}
        flow = make_flow(hass)

        result = asyncio.run(flow.async_step_recheck())

        assert result["type"] == "create_entry"
        assert registry.issues == {}

    def test_reload_reloads_the_entry(
        self, registry, timers, clock, no_holding_proxy
    ) -> None:
        hass = FakeHass()
        entry = FakeEntry()
        make_coordinator(hass, entry, FakeDevice(available=False))

        result = asyncio.run(make_flow(hass).async_step_reload())

        assert hass.config_entries.reloaded == [entry.entry_id]
        assert result["type"] == "menu"

    def test_power_cycle_remembers_the_chosen_outlet_and_cuts_power(
        self, registry, timers, clock, no_holding_proxy
    ) -> None:
        hass = FakeHass()
        entry = FakeEntry()
        make_coordinator(hass, entry, FakeDevice(available=False))
        flow = make_flow(hass)

        asyncio.run(flow.async_step_power_cycle({CONF_ENTITY_ID: OUTLET}))

        assert entry.options[OPTION_RECOVERY_OUTLET] == OUTLET
        assert hass.services.calls == [
            ("switch", "turn_off", {"entity_id": OUTLET}),
            ("switch", "turn_on", {"entity_id": OUTLET}),
        ]
        # Remembered means offered back: the next visit prefills the field.
        form = asyncio.run(make_flow(hass).async_step_power_cycle())
        [field] = list(form["data_schema"].schema)
        assert field.default() == OUTLET

    def test_flow_aborts_when_the_bedjet_is_no_longer_configured(
        self, registry, timers, clock
    ) -> None:
        hass = FakeHass()

        result = asyncio.run(make_flow(hass).async_step_init())

        assert result == {"type": "abort", "reason": "entry_not_found"}

    def test_flow_aborts_when_the_integration_is_not_loaded(
        self, registry, timers, clock
    ) -> None:
        hass = FakeHass()
        entry = FakeEntry()
        entry.state = ConfigEntryState.SETUP_RETRY
        hass.config_entries.entries.append(entry)

        result = asyncio.run(make_flow(hass).async_step_init())

        assert result == {"type": "abort", "reason": "not_loaded"}
