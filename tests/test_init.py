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

import bleak_retry_connector
import pytest

import custom_components.bedjet as bedjet_init
from homeassistant.const import CONF_ADDRESS
from homeassistant.exceptions import ConfigEntryNotReady

ADDRESS = "FC:F5:C4:20:1A:92"


class FakeBedJet:
    instances: list["FakeBedJet"] = []

    def __init__(
        self, ble_device, advertisement_data, *, source=None, clock=None, client_class=None
    ) -> None:
        self.ble_device = ble_device
        self.advertisement_data = advertisement_data
        self.source = source
        self.clock = clock
        self.started = False
        self.stopped = False
        self.released_for_shutdown = False
        # Set to an Event to make the shutdown release hang, or an exception
        # to make it fail.
        self.release_blocker = None
        self.callbacks: list = []
        self.set_ble_calls: list[tuple] = []
        self.state = SimpleNamespace(sentinel=True)
        self.address = ADDRESS
        self.client_class = client_class
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

    async def release_for_shutdown(self) -> None:
        self.released_for_shutdown = True
        blocker = self.release_blocker
        if isinstance(blocker, Exception):
            raise blocker
        if blocker is not None:
            await blocker.wait()

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
        self.entries: list = []
        self.setup_calls: list[str] = []
        # Set to an Event to make platform forwarding block.
        self.forward_gate = None
        self.on_forward = None

    def async_entries(self, domain):
        return list(self.entries)

    async def async_unload(self, entry_id) -> bool:
        for entry in self.entries:
            if entry.entry_id == entry_id:
                entry.state = bedjet_init.ConfigEntryState.NOT_LOADED
        return True

    async def async_setup(self, entry_id) -> bool:
        self.setup_calls.append(entry_id)
        return True

    async def async_forward_entry_setups(self, entry, platforms) -> None:
        if self.on_forward is not None:
            self.on_forward()
        if self.forward_gate is not None:
            await self.forward_gate.wait()
        self.forwarded.append((entry, tuple(platforms)))

    async def async_unload_platforms(self, entry, platforms) -> bool:
        self.unloaded.append((entry, tuple(platforms)))
        return True


class _FakeServices:
    """Just enough registry for the release_link registration at setup."""

    def __init__(self) -> None:
        self.registered: set[str] = set()

    def has_service(self, domain, service) -> bool:
        return f"{domain}.{service}" in self.registered

    def async_register(self, domain, service, handler, schema=None) -> None:
        self.registered.add(f"{domain}.{service}")


class FakeHass:
    def __init__(self) -> None:
        # Entry setup registers the domain release_link action.
        self.services = _FakeServices()
        self.bus = FakeBus()
        self.config_entries = FakeConfigEntries()
        # The `device_unreachable` outage clock lives here, keyed by address.
        self.data: dict = {}
        self.shutdown_jobs: list = []

    def async_add_shutdown_job(self, job):
        self.shutdown_jobs.append(job)
        return lambda: self.shutdown_jobs.remove(job)


class FakeEntry:
    def __init__(self, address: str = ADDRESS) -> None:
        self.data = {CONF_ADDRESS: address}
        self.options: dict = {}
        self.title = "Bedjetty"
        self.runtime_data = None
        self.entry_id = "entry-1"
        self.state = bedjet_init.ConfigEntryState.LOADED
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


def test_setup_builds_an_affinity_client_class_when_habluetooth_supports_it(
    monkeypatch, patched_bedjet
) -> None:
    """Regression: setup must build the client class through
    ble_affinity.make_affinity_client_class - handing BedJet the plain
    bleak_retry_connector base straight through would silently disable the
    preferred-proxy option. `bleak_retry_connector.BleakClientWithServiceCache`
    is monkeypatched here to a fake exposing the habluetooth selection hooks
    (affinity_supported() requires them), standing in for what Home
    Assistant's real `bluetooth` component installs at runtime.
    """

    class FakeBaseWithHooks:
        def _async_get_best_available_backend_and_device(self, manager):
            return None

        def _async_get_backend_for_ble_device(self, manager, scanner, ble_device):
            return None

    monkeypatch.setattr(bleak_retry_connector, "BleakClientWithServiceCache", FakeBaseWithHooks)
    service_info = SimpleNamespace(device=object(), advertisement=object(), source="D4:D4:DA:9D:40:8A")
    monkeypatch.setattr(
        bedjet_init.bluetooth,
        "async_last_service_info",
        lambda hass, address, connectable=True: service_info,
    )
    hass = FakeHass()
    entry = FakeEntry()

    asyncio.run(bedjet_init.async_setup_entry(hass, entry))

    client_class = FakeBedJet.instances[-1].client_class
    assert client_class is not None
    assert client_class is not FakeBaseWithHooks
    assert issubclass(client_class, FakeBaseWithHooks)


def _setup(monkeypatch, patched_bedjet):
    monkeypatch.setattr(
        bedjet_init.bluetooth,
        "async_last_service_info",
        lambda hass, address, connectable=True: SimpleNamespace(
            device=object(), advertisement=object(), source="proxy-1"
        ),
    )
    hass = FakeHass()
    entry = FakeEntry()
    assert asyncio.run(bedjet_init.async_setup_entry(hass, entry)) is True
    return hass, entry, FakeBedJet.instances[-1]


def test_each_entry_registers_one_shutdown_job_removed_on_unload(
    monkeypatch, patched_bedjet
) -> None:
    hass, entry, _device = _setup(monkeypatch, patched_bedjet)
    assert len(hass.shutdown_jobs) == 1
    # No STOP-event listener competes with the shutdown job any more.
    assert hass.bus.listeners == []

    for unload in entry._unload_callbacks:
        unload()

    assert hass.shutdown_jobs == []


def test_shutdown_job_releases_the_link_and_refuses_later_setup(
    monkeypatch, patched_bedjet
) -> None:
    hass, entry, device = _setup(monkeypatch, patched_bedjet)

    asyncio.run(hass.shutdown_jobs[0].target())

    assert device.released_for_shutdown is True
    # Entry is not unloaded: only the link is released.
    assert device.stopped is False
    assert hass.config_entries.unloaded == []
    # Nothing in this process may set a BedJet up (and so connect) afterwards.
    with pytest.raises(ConfigEntryNotReady):
        asyncio.run(bedjet_init.async_setup_entry(hass, FakeEntry()))


def test_shutdown_job_is_bounded_when_the_disconnect_hangs(
    monkeypatch, patched_bedjet
) -> None:
    monkeypatch.setattr(bedjet_init, "SHUTDOWN_RELEASE_TIMEOUT_S", 0.05)
    hass, _entry, device = _setup(monkeypatch, patched_bedjet)

    async def scenario() -> None:
        device.release_blocker = asyncio.Event()  # never set
        await asyncio.wait_for(hass.shutdown_jobs[0].target(), timeout=2)

    asyncio.run(scenario())  # returns; does not raise


def test_shutdown_job_swallows_a_failing_release(monkeypatch, patched_bedjet) -> None:
    hass, _entry, device = _setup(monkeypatch, patched_bedjet)
    device.release_blocker = RuntimeError("proxy went away")

    asyncio.run(hass.shutdown_jobs[0].target())  # must not raise
    assert device.released_for_shutdown is True


def test_domain_latch_job_is_registered_once_by_async_setup() -> None:
    hass = FakeHass()

    assert asyncio.run(bedjet_init.async_setup(hass, {})) is True

    assert len(hass.shutdown_jobs) == 1
    asyncio.run(hass.shutdown_jobs[0].target())
    assert hass.data[bedjet_init.KEY_SHUTTING_DOWN] is True


def test_shutdown_job_is_registered_before_platform_forwarding(
    monkeypatch, patched_bedjet
) -> None:
    # HA lists the shutdown jobs once when Stage 1 starts; a job added after
    # the platform-forwarding await would be missed.
    monkeypatch.setattr(
        bedjet_init.bluetooth,
        "async_last_service_info",
        lambda hass, address, connectable=True: SimpleNamespace(
            device=object(), advertisement=object(), source="proxy-1"
        ),
    )
    hass = FakeHass()
    seen: list[int] = []
    hass.config_entries.on_forward = lambda: seen.append(len(hass.shutdown_jobs))

    asyncio.run(bedjet_init.async_setup_entry(hass, FakeEntry()))

    assert seen == [1]


def test_setup_refuses_and_tears_down_when_shutdown_starts_during_start(
    monkeypatch, patched_bedjet
) -> None:
    monkeypatch.setattr(
        bedjet_init.bluetooth,
        "async_last_service_info",
        lambda hass, address, connectable=True: SimpleNamespace(
            device=object(), advertisement=object(), source="proxy-1"
        ),
    )
    hass = FakeHass()

    async def start_during_shutdown(self) -> None:
        self.started = True
        hass.data[bedjet_init.KEY_SHUTTING_DOWN] = True

    monkeypatch.setattr(FakeBedJet, "start", start_during_shutdown)

    with pytest.raises(ConfigEntryNotReady):
        asyncio.run(bedjet_init.async_setup_entry(hass, FakeEntry()))

    assert FakeBedJet.instances[-1].released_for_shutdown is True
    assert hass.config_entries.forwarded == []


def test_domain_latch_cancels_a_pending_release_link_resume(monkeypatch) -> None:
    cancelled: list[str] = []
    timers: list = []

    def fake_call_later(hass, delay, action):
        timers.append(action)
        return lambda: cancelled.append("cancelled")

    monkeypatch.setattr(bedjet_init, "async_call_later", fake_call_later)
    hass = FakeHass()
    entry = FakeEntry()
    hass.config_entries.entries = [entry]
    asyncio.run(bedjet_init.async_setup(hass, {}))

    asyncio.run(bedjet_init._async_release_links(hass, 1))
    assert len(timers) == 1

    # release_link unloaded the entry (its own shutdown job is gone); the
    # domain job alone must still stop the pending resume.
    asyncio.run(hass.shutdown_jobs[0].target())
    assert cancelled == ["cancelled"]

    # Even if the timer fires anyway, it must not set anything up.
    asyncio.run(timers[0](None))
    assert hass.config_entries.setup_calls == []


def test_release_link_during_shutdown_schedules_no_resume(monkeypatch) -> None:
    timers: list = []
    monkeypatch.setattr(
        bedjet_init, "async_call_later", lambda hass, delay, action: timers.append(action)
    )
    hass = FakeHass()
    hass.config_entries.entries = [FakeEntry()]
    hass.data[bedjet_init.KEY_SHUTTING_DOWN] = True

    asyncio.run(bedjet_init._async_release_links(hass, 1))

    assert timers == []
