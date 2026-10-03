"""Tests for pybedjet.BedJet's connection lifecycle: hold_connection
semantics, reconnect-on-advertisement, command confirmation/timeout, the
watchdog's three tiers, tail-read staleness gating, and listener fan-out
rate limiting.

Real time is never slept: `establish_connection`/`BleakClient` are replaced
with `tests.ble_fakes`, `_monotonic` is monkeypatched to a controllable fake
clock, and short real-world constants (`COMMAND_TIMEOUT_S`,
`reconnect_backoff_seconds`) are patched to tiny values so a test only waits
on genuinely-pending asyncio tasks, never a real multi-second/minute delay.
"""

from __future__ import annotations

import asyncio
import datetime

import pytest

import custom_components.bedjet.pybedjet as pb
from custom_components.bedjet.pybedjet import (
    BedJet,
    BedJetCommandError,
    BedJetConnectionError,
)
from custom_components.bedjet.pybedjet.const import BedJetMode
from tests.ble_fakes import (
    BEDJET_COMMAND_UUID,
    BEDJET_STATUS_UUID,
    FakeBleakClient,
    FakeBleakClientFactory,
    make_advertisement_data,
    make_ble_device,
    make_fake_establish_connection,
)
from tests.conftest import build_notify_frame, build_tail


class FakeClock:
    """Controllable stand-in for time.monotonic, advanced explicitly by tests."""

    def __init__(self, t: float = 1_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


async def _pump(times: int = 5) -> None:
    """Let pending asyncio tasks (create_task callbacks) make progress."""
    for _ in range(times):
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _reset_fake_bleak_client():
    FakeBleakClient.fail_next_connects = 0
    yield
    FakeBleakClient.fail_next_connects = 0


@pytest.fixture
def factory(monkeypatch) -> FakeBleakClientFactory:
    factory = FakeBleakClientFactory()
    monkeypatch.setattr(pb, "establish_connection", make_fake_establish_connection(factory))
    return factory


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(pb, "_monotonic", clock)
    return clock


@pytest.fixture
def fast_backoff(monkeypatch):
    """Backoff delay small enough that a real await resolves near-instantly."""
    monkeypatch.setattr(pb, "reconnect_backoff_seconds", lambda attempt, rng=None: 0.01)


@pytest.fixture
def fast_command_timeout(monkeypatch):
    monkeypatch.setattr(pb, "COMMAND_TIMEOUT_S", 0.05)


def make_bedjet(*, hold_connection: bool = True) -> BedJet:
    return BedJet(
        make_ble_device(),
        make_advertisement_data(),
        hold_connection=hold_connection,
    )


class TestHoldConnection:
    def test_false_at_start_never_connects(self, factory, fast_backoff) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=False)
            await bedjet.start()
            await _pump()
            assert factory.clients == []
            assert bedjet.connected is False
            await bedjet.stop()

        asyncio.run(scenario())

    def test_setting_true_connects(self, factory, fast_backoff) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=False)
            await bedjet.start()
            await _pump()
            assert factory.clients == []

            bedjet.hold_connection = True
            await _pump()

            assert bedjet.connected is True
            await bedjet.stop()

        asyncio.run(scenario())

    def test_setting_false_while_connected_disconnects(self, factory, fast_backoff) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            await bedjet.start()
            await _pump()
            assert bedjet.connected is True
            client = factory.last

            bedjet.hold_connection = False
            await _pump()

            assert client.disconnect_calls == 1
            assert bedjet.connected is False
            await bedjet.stop()

        asyncio.run(scenario())

    def test_false_suppresses_reconnect_even_on_advertisement(self, factory, fast_backoff) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            await bedjet.start()
            await _pump()
            assert len(factory.clients) == 1

            bedjet.hold_connection = False
            await _pump()

            bedjet.set_ble_device_and_advertisement_data(
                make_ble_device(), make_advertisement_data()
            )
            await _pump()

            # No new client: the slot stays released until hold_connection
            # is turned back on.
            assert len(factory.clients) == 1
            assert bedjet.connected is False
            await bedjet.stop()

        asyncio.run(scenario())


class TestReconnectOnAdvertisement:
    def test_advertisement_wakes_a_pending_backoff_immediately(self, factory, monkeypatch) -> None:
        async def scenario() -> None:
            # A long backoff so a "did it wake early" assertion is meaningful.
            monkeypatch_backoff_calls: list[int] = []

            def slow_backoff(attempt: int, rng=None) -> float:
                monkeypatch_backoff_calls.append(attempt)
                return 10.0  # would never resolve within this test's timeout

            monkeypatch.setattr(pb, "reconnect_backoff_seconds", slow_backoff)

            FakeBleakClient.fail_next_connects = 4  # exhaust establish_connection's retries
            bedjet = make_bedjet(hold_connection=True)
            await bedjet.start()
            await _pump()
            assert bedjet.connected is False
            assert monkeypatch_backoff_calls == [0]  # first failure recorded

            # Supervisor is now asleep waiting up to 10s for a wakeup. An
            # advertisement must interrupt that wait immediately.
            bedjet.set_ble_device_and_advertisement_data(
                make_ble_device(), make_advertisement_data()
            )
            await asyncio.wait_for(_pump(20), timeout=1.0)

            assert bedjet.connected is True
            assert len(factory.clients) > 1  # a genuine second attempt happened
            await bedjet.stop()

        asyncio.run(scenario())

    def test_disconnect_triggers_exactly_one_reconnect(self, factory, fast_backoff) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            await bedjet.start()
            await _pump()
            assert len(factory.clients) == 1

            factory.last.simulate_disconnect()
            await asyncio.wait_for(_pump(20), timeout=1.0)

            assert len(factory.clients) == 2
            assert bedjet.connected is True
            await bedjet.stop()

        asyncio.run(scenario())


class TestReconnectBackoffIntegration:
    def test_attempt_count_increments_then_resets_on_success(self, factory, monkeypatch) -> None:
        async def scenario() -> None:
            seen_attempts: list[int] = []

            def recording_backoff(attempt: int, rng=None) -> float:
                seen_attempts.append(attempt)
                return 0.0  # expires immediately; no real wall-clock wait needed

            monkeypatch.setattr(pb, "reconnect_backoff_seconds", recording_backoff)

            FakeBleakClient.fail_next_connects = 12  # fail several full establish_connection calls
            bedjet = make_bedjet(hold_connection=True)
            await bedjet.start()
            for _ in range(50):
                await asyncio.sleep(0.005)
                if len(seen_attempts) >= 3:
                    break

            assert seen_attempts == sorted(seen_attempts)  # non-decreasing
            assert seen_attempts[0] == 0
            assert len(seen_attempts) >= 2  # more than one failure was recorded
            await bedjet.stop()

        asyncio.run(scenario())


class TestCommandConfirmation:
    def test_not_connected_raises_connection_error(self, factory) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=False)
            with pytest.raises(BedJetConnectionError):
                await bedjet.set_mode(BedJetMode.HEAT)

        asyncio.run(scenario())

    def test_matching_frame_resolves_the_command(self, factory) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            await bedjet.start()
            await _pump()
            client = factory.last

            task = asyncio.create_task(bedjet.set_mode(BedJetMode.HEAT))
            await _pump()
            assert client.writes[-1] == bytes([0x01, 0x03])  # BUTTON, HEAT

            client.notify(BEDJET_STATUS_UUID, build_notify_frame(mode=int(BedJetMode.HEAT)))
            await asyncio.wait_for(task, timeout=1.0)  # must not raise

            assert bedjet.state.mode is BedJetMode.HEAT
            await bedjet.stop()

        asyncio.run(scenario())

    def test_non_matching_frames_do_not_resolve_and_it_times_out(
        self, factory, fast_command_timeout
    ) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            await bedjet.start()
            await _pump()
            client = factory.last

            task = asyncio.create_task(bedjet.set_mode(BedJetMode.HEAT))
            await _pump()
            # Frames confirming a different mode must not resolve this command.
            client.notify(BEDJET_STATUS_UUID, build_notify_frame(mode=int(BedJetMode.COOL)))
            await _pump()

            with pytest.raises(BedJetCommandError):
                await asyncio.wait_for(task, timeout=1.0)
            await bedjet.stop()

        asyncio.run(scenario())


class TestTailReadGating(object):
    def test_partial_frame_triggers_one_tail_read_not_per_frame(self, factory, clock) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            await bedjet.start()
            await _pump()
            client = factory.last
            client.set_read_value(BEDJET_STATUS_UUID, build_tail())

            client.notify(BEDJET_STATUS_UUID, build_notify_frame())
            await _pump()
            assert client.reads.count(BEDJET_STATUS_UUID) == 1

            # A second is_partial frame shortly after (well within
            # TAIL_MAX_AGE_S) with nothing stale/pending must not re-read.
            clock.advance(1.0)
            client.notify(BEDJET_STATUS_UUID, build_notify_frame(fan_step=0))
            await _pump()
            assert client.reads.count(BEDJET_STATUS_UUID) == 1

            await bedjet.stop()

        asyncio.run(scenario())

    def test_stale_tail_is_re_read_after_max_age(self, factory, clock) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            await bedjet.start()
            await _pump()
            client = factory.last
            client.set_read_value(BEDJET_STATUS_UUID, build_tail())

            client.notify(BEDJET_STATUS_UUID, build_notify_frame())
            await _pump()
            assert client.reads.count(BEDJET_STATUS_UUID) == 1

            clock.advance(pb.TAIL_MAX_AGE_S + 1.0)
            client.notify(BEDJET_STATUS_UUID, build_notify_frame())
            await _pump()

            assert client.reads.count(BEDJET_STATUS_UUID) == 2
            await bedjet.stop()

        asyncio.run(scenario())

    def test_command_completion_forces_a_tail_re_read(self, factory, clock) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            await bedjet.start()
            await _pump()
            client = factory.last
            client.set_read_value(BEDJET_STATUS_UUID, build_tail())

            client.notify(BEDJET_STATUS_UUID, build_notify_frame())
            await _pump()
            assert client.reads.count(BEDJET_STATUS_UUID) == 1

            # Barely any time passes (not stale), but a command just wrote -
            # the next partial frame must still trigger a fresh tail read.
            clock.advance(0.5)
            task = asyncio.create_task(bedjet.set_mode(BedJetMode.HEAT))
            await _pump()
            client.notify(BEDJET_STATUS_UUID, build_notify_frame(mode=int(BedJetMode.HEAT)))
            await asyncio.wait_for(task, timeout=1.0)
            await _pump()

            assert client.reads.count(BEDJET_STATUS_UUID) == 2
            await bedjet.stop()

        asyncio.run(scenario())


class TestListenerFanOutRateLimit:
    def test_continuous_only_change_is_rate_limited(self, factory, clock) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            events: list[int] = []
            bedjet.register_callback(lambda _d: events.append(1))

            await bedjet.start()
            await _pump()
            client = factory.last
            # Establish a baseline decoded state first: the very first frame
            # is always "meaningful" (previous is None), which would
            # otherwise make a bare actual-temp-only test frame publish
            # immediately for the wrong reason.
            client.notify(BEDJET_STATUS_UUID, build_notify_frame(actual_temp_step=50))
            await _pump()
            baseline = len(events)

            clock.advance(0.5)  # well within PUBLISH_MIN_INTERVAL_S
            client.notify(
                BEDJET_STATUS_UUID, build_notify_frame(actual_temp_step=60)
            )  # ambient/actual only
            await _pump()
            assert len(events) == baseline  # rate-limited, not fired

            clock.advance(pb.PUBLISH_MIN_INTERVAL_S + 0.1)
            client.notify(BEDJET_STATUS_UUID, build_notify_frame(actual_temp_step=62))
            await _pump()
            assert len(events) == baseline + 1  # now stale enough to publish

            await bedjet.stop()

        asyncio.run(scenario())

    def test_mode_change_publishes_immediately_despite_rate_limit(self, factory, clock) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            events: list[int] = []
            bedjet.register_callback(lambda _d: events.append(1))

            await bedjet.start()
            await _pump()
            client = factory.last
            client.notify(BEDJET_STATUS_UUID, build_notify_frame(mode=int(BedJetMode.STANDBY)))
            await _pump()
            baseline = len(events)

            clock.advance(0.1)  # nowhere near PUBLISH_MIN_INTERVAL_S; must publish anyway
            client.notify(BEDJET_STATUS_UUID, build_notify_frame(mode=int(BedJetMode.HEAT)))
            await _pump()

            assert len(events) == baseline + 1


            await bedjet.stop()

        asyncio.run(scenario())


class TestWatchdog:
    async def _connected_bedjet(self, factory):
        bedjet = make_bedjet(hold_connection=True)
        await bedjet.start()
        await _pump(20)  # let the background bio-name read task finish too
        return bedjet, factory.last

    async def _run_one_tick(self, monkeypatch, bedjet) -> None:
        """Run exactly one watchdog tick body by making the sleep a no-op
        that stops the loop right after, instead of sleeping WATCHDOG_TICK_S.
        """

        async def fake_sleep(_seconds: float) -> None:
            bedjet._stopped = True

        monkeypatch.setattr(pb.asyncio, "sleep", fake_sleep)
        await bedjet._watchdog_loop()

    def test_below_probe_threshold_does_nothing(self, factory, clock, monkeypatch) -> None:
        async def scenario() -> None:
            bedjet, client = await self._connected_bedjet(factory)
            writes_before = len(client.writes)
            clock.advance(30.0)
            await self._run_one_tick(monkeypatch, bedjet)
            assert client.writes[writes_before:] == []

        asyncio.run(scenario())

    def test_probe_tier_sends_status_request(
        self, factory, clock, monkeypatch, fast_command_timeout
    ) -> None:
        async def scenario() -> None:
            bedjet, client = await self._connected_bedjet(factory)
            clock.advance(pb.WATCHDOG_PROBE_AFTER_S + 1.0)
            await self._run_one_tick(monkeypatch, bedjet)
            assert client.writes[-1] == bytes([0x06])  # CMD_STATUS, no data

        asyncio.run(scenario())

    def test_unavailable_tier_fires_callback_once_per_stale_last_frame(
        self, factory, clock, monkeypatch
    ) -> None:
        async def scenario() -> None:
            bedjet, client = await self._connected_bedjet(factory)
            events: list[int] = []
            bedjet.register_callback(lambda _d: events.append(1))
            before = len(events)

            clock.advance(pb.STATUS_TIMEOUT_S + 1.0)
            assert bedjet.available is False
            await self._run_one_tick(monkeypatch, bedjet)
            assert len(events) == before + 1

            # Same stale last_frame_at again: must not re-notify.
            bedjet._stopped = False
            await self._run_one_tick(monkeypatch, bedjet)
            assert len(events) == before + 1

        asyncio.run(scenario())

    def test_reconnect_tier_forces_disconnect(self, factory, clock, monkeypatch) -> None:
        async def scenario() -> None:
            bedjet, client = await self._connected_bedjet(factory)
            clock.advance(pb.WATCHDOG_RECONNECT_AFTER_S + 1.0)
            await self._run_one_tick(monkeypatch, bedjet)

            assert client.disconnect_calls == 1
            assert bedjet.connected is False
            assert bedjet._connect_wakeup.is_set()

        asyncio.run(scenario())


class TestDropAccounting:
    """`drops_1h`/`last_drop`/`reconnect_attempt` are what the HA Connection
    sensor publishes, and habluetooth tracks none of them (it scores connect
    *failures*, never post-connect drops), so this is the only source.
    Deliberate releases must not be counted: a user handing the slot to the
    phone app is not a fault.
    """

    def test_unexpected_disconnect_is_counted(self, factory, clock, fast_backoff) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            await bedjet.start()
            await _pump()
            assert bedjet.drops_1h == 0
            assert bedjet.last_drop is None

            factory.last.simulate_disconnect()
            await _pump()

            assert bedjet.drops_1h == 1
            assert bedjet.last_drop is not None
            await bedjet.stop()

        asyncio.run(scenario())

    def test_drops_age_out_of_the_trailing_hour(self, factory, clock, fast_backoff) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            await bedjet.start()
            await _pump()
            factory.last.simulate_disconnect()
            await _pump()
            assert bedjet.drops_1h == 1

            clock.advance(pb.DROP_WINDOW_S + 1.0)

            assert bedjet.drops_1h == 0
            # But the timestamp of the last one is still reportable.
            assert bedjet.last_drop is not None
            await bedjet.stop()

        asyncio.run(scenario())

    def test_releasing_the_slot_on_purpose_is_not_a_drop(self, factory, fast_backoff) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            await bedjet.start()
            await _pump()

            bedjet.hold_connection = False
            await _pump()
            assert bedjet.connected is False

            await bedjet.stop()
            assert bedjet.drops_1h == 0
            assert bedjet.last_drop is None

        asyncio.run(scenario())

    def test_watchdog_forced_reconnect_is_counted(self, factory, clock, monkeypatch) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            await bedjet.start()
            await _pump(20)
            clock.advance(pb.WATCHDOG_RECONNECT_AFTER_S + 1.0)

            async def fake_sleep(_seconds: float) -> None:
                bedjet._stopped = True

            monkeypatch.setattr(pb.asyncio, "sleep", fake_sleep)
            await bedjet._watchdog_loop()

            # A wedged link that stopped streaming is a lost hold, and it
            # must be counted exactly once despite the bleak disconnect
            # callback that follows our own teardown.
            assert bedjet.drops_1h == 1

        asyncio.run(scenario())

    def test_reconnect_attempt_is_zero_while_connected(self, factory, fast_backoff) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            await bedjet.start()
            await _pump()
            assert bedjet.connected is True
            assert bedjet.reconnect_attempt == 0
            await bedjet.stop()

        asyncio.run(scenario())

    def test_failed_attempts_publish_so_the_sensor_can_follow(
        self, factory, monkeypatch
    ) -> None:
        async def scenario() -> None:
            monkeypatch.setattr(pb, "reconnect_backoff_seconds", lambda attempt, rng=None: 0.0)
            FakeBleakClient.fail_next_connects = 12
            bedjet = make_bedjet(hold_connection=True)
            events: list[int] = []
            bedjet.register_callback(lambda device: events.append(device.reconnect_attempt))

            await bedjet.start()
            for _ in range(50):
                await asyncio.sleep(0.005)
                if len(events) >= 2:
                    break

            assert events[:2] == [1, 2]
            await bedjet.stop()

        asyncio.run(scenario())


class TestReleaseForShutdown:
    """Home Assistant's shutdown job: release the link, then never connect again."""

    def test_disconnects_and_refuses_every_path_back_to_a_connection(
        self, factory, fast_backoff
    ) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            await bedjet.start()
            await _pump()
            assert bedjet.connected is True
            client = factory.last
            fired: list[BedJet] = []
            bedjet.register_callback(fired.append)

            await bedjet.release_for_shutdown()

            assert client.disconnect_calls == 1
            assert bedjet.connected is False
            # A deliberate release is not published (no wave of state writes).
            assert fired == []
            # The user's hold setting is untouched.
            assert bedjet.hold_connection is True

            # An advertisement (the slot looks free), a re-hold request and a
            # fresh start() must all be refused.
            bedjet.set_ble_device_and_advertisement_data(
                make_ble_device(), make_advertisement_data()
            )
            bedjet.hold_connection = False
            bedjet.hold_connection = True
            await bedjet.start()
            await _pump(20)
            assert len(factory.clients) == 1
            assert bedjet.connected is False
            with pytest.raises(BedJetConnectionError):
                await bedjet.set_mode(BedJetMode.HEAT)

        asyncio.run(scenario())

    def test_refused_even_while_a_reconnect_backoff_is_pending(
        self, factory, monkeypatch
    ) -> None:
        monkeypatch.setattr(pb, "reconnect_backoff_seconds", lambda attempt, rng=None: 0.05)

        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            await bedjet.start()
            await _pump()
            factory.last.simulate_disconnect()  # link lost; supervisor now backing off
            FakeBleakClient.fail_next_connects = 1
            await _pump()
            before = len(factory.clients)

            await bedjet.release_for_shutdown()
            await asyncio.sleep(0.2)  # several backoff periods

            assert len(factory.clients) == before
            assert bedjet.connected is False

        asyncio.run(scenario())

    def test_hanging_disconnect_is_bounded_by_the_callers_timeout(
        self, factory, fast_backoff
    ) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            await bedjet.start()
            await _pump()
            hang = asyncio.Event()

            async def never_returns() -> bool:
                await hang.wait()
                return True

            factory.last.disconnect = never_returns  # type: ignore[method-assign]

            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.05):
                    await bedjet.release_for_shutdown()

            # Latched even though the disconnect never completed.
            bedjet.set_ble_device_and_advertisement_data(
                make_ble_device(), make_advertisement_data()
            )
            await _pump(20)
            assert len(factory.clients) == 1

        asyncio.run(scenario())


class TestStuckStepsFailFast:
    """Startup contract S4: no single connect/subscribe/write/disconnect step
    may park the library forever, even though bleak has no timeout of its own.
    The real limit is 10 s; tests shrink it so only pending tasks are awaited.
    """

    @pytest.fixture(autouse=True)
    def _short_step_timeout(self, monkeypatch, fast_backoff):
        monkeypatch.setattr(pb, "GATT_STEP_TIMEOUT_S", 0.05)

    def test_start_returns_at_once_and_hung_connect_recovers_with_data(
        self, factory, monkeypatch
    ) -> None:
        """Setup never waits (a); a connect that hangs is abandoned and the
        retry connects, and data arriving long after start() populates state (b)."""
        real_establish = make_fake_establish_connection(factory)
        calls = 0

        async def hang_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.Event().wait()  # never answers
            return await real_establish(*args, **kwargs)

        monkeypatch.setattr(pb, "establish_connection", hang_once)

        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            events: list[int] = []
            bedjet.register_callback(lambda _d: events.append(1))
            async with asyncio.timeout(0.02):  # far below the step timeout
                await bedjet.start()
            assert bedjet.connected is False
            assert bedjet.state is None

            await asyncio.wait_for(_until(lambda: bedjet.connected), timeout=1.0)
            assert calls == 2
            factory.last.notify(
                BEDJET_STATUS_UUID, build_notify_frame(mode=int(BedJetMode.HEAT))
            )
            assert bedjet.state.mode is BedJetMode.HEAT
            assert events
            await bedjet.stop()

        asyncio.run(scenario())

    def test_hung_subscribe_is_abandoned_and_link_released(self, factory, monkeypatch) -> None:
        subscribe_calls = 0
        original = FakeBleakClient.start_notify

        async def hang_first(self, *args, **kwargs):
            nonlocal subscribe_calls
            subscribe_calls += 1
            if subscribe_calls == 1:
                await asyncio.Event().wait()
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(FakeBleakClient, "start_notify", hang_first)

        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            await bedjet.start()
            await asyncio.wait_for(_until(lambda: bedjet.connected), timeout=1.0)
            assert subscribe_calls == 2
            assert factory.clients[0].disconnect_calls == 1  # stuck link not leaked
            await bedjet.stop()

        asyncio.run(scenario())

    def test_hung_clock_write_does_not_park_the_supervisor(self, factory, monkeypatch) -> None:
        original = FakeBleakClient.write_gatt_char

        async def hang_write(self, *args, **kwargs):
            await asyncio.Event().wait()

        monkeypatch.setattr(FakeBleakClient, "write_gatt_char", hang_write)

        async def scenario() -> None:
            bedjet = BedJet(
                make_ble_device(),
                make_advertisement_data(),
                hold_connection=True,
                clock=lambda: datetime.datetime(2026, 1, 1, 12, 0),
            )
            await bedjet.start()
            # The post-connect clock write hangs; the connect attempt must
            # still finish (and go on to the memory-name read) after the
            # step timeout instead of parking the supervisor forever.
            await asyncio.wait_for(
                _until(lambda: bedjet._bio_read_task is not None), timeout=1.0
            )
            monkeypatch.setattr(FakeBleakClient, "write_gatt_char", original)
            await bedjet.stop()

        asyncio.run(scenario())

    def test_hung_disconnect_is_bounded_without_a_caller_timeout(self, factory) -> None:
        async def scenario() -> None:
            bedjet = make_bedjet(hold_connection=True)
            await bedjet.start()
            await _pump()
            hang = asyncio.Event()

            async def never_returns() -> bool:
                await hang.wait()
                return True

            factory.last.disconnect = never_returns  # type: ignore[method-assign]
            await asyncio.wait_for(bedjet.stop(), timeout=1.0)  # must not hang

        asyncio.run(scenario())


async def _until(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0.005)
