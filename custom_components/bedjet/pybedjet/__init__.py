"""BedJet V3 async device client: connection lifecycle, watchdog, and command confirmation.

See ``pybedjet/codec.py`` for the wire-protocol byte map (frame/tail decoding,
command encoding) - that module is pure and has no I/O. This module owns only
*when* to connect, read, write, and reconnect over a real BLE link shared
through Home Assistant's ``bluetooth`` component (which may route through a
remote ESPHome Bluetooth proxy) via ``bleak``/``bleak_retry_connector``.

Single-slot connection model
================================================================
A BedJet V3 accepts exactly one BLE connection and stops advertising while
connected (ground truth, verified live against a real unit). An advertisement
is therefore a positive signal that the slot is currently free, and is what
drives reconnection - see `_connect_supervisor`. `hold_connection` is the
switch a user flips to voluntarily give the slot back to the BedJet mobile
app without unloading the whole integration.

16-bit CCCD note [answering the Contract's explicit question]: ESPHome's own
`BedJetHub::write_notify_config_descriptor_` (bedjet_hub.cpp:420-434) exists
only because ESP-IDF's raw `esp_ble_gattc` API and ESPHome's `ble_client`
component write an 8-bit value to the notify Client Characteristic
Configuration Descriptor where the BLE spec requires 16 bits, forcing ESPHome
to redo that write itself. That is an ESP-IDF/ESPHome implementation detail,
not a BedJet protocol requirement. `bleak`'s `BleakClient.start_notify()`
already performs a spec-correct 16-bit CCCD write on every backend; this
module does nothing special here and needs nothing special.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
import contextlib
from datetime import UTC, datetime
from enum import Enum, auto
import logging
import random
import time

from bleak import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
import bleak_retry_connector
from bleak_retry_connector import BleakClientWithServiceCache, BleakError, establish_connection

from .codec import (
    BedJetFrameError,
    BedJetState,
    build_command,
    decode_frame,
    is_meaningful_change,
    merge_tail,
)
from .const import BedJetButton, BedJetCommand, BedJetMode, BedJetNotification, BioDataRequest

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "BedJet",
    "BedJetState",
    "BedJetMode",
    "BedJetButton",
    "BedJetNotification",
    "BedJetCommand",
    "BedJetError",
    "BedJetConnectionError",
    "BedJetCommandError",
    "BedJetFrameError",
    "WatchdogAction",
    "watchdog_action",
    "reconnect_backoff_seconds",
    "DropTracker",
    "is_meaningful_change",
    "decode_frame",
    "merge_tail",
    "build_command",
]

# BedJet V3 GATT layout (see codec.py for the command/frame byte-level map).
SERVICE_UUID = "00001000-bed0-0080-aa55-4265644a6574"
STATUS_UUID = "00002000-bed0-0080-aa55-4265644a6574"  # notify (20B) + plain read (tail, 11B)
COMMAND_UUID = "00002004-bed0-0080-aa55-4265644a6574"  # write-without-response
BIODATA_FULL_UUID = "00002006-bed0-0080-aa55-4265644a6574"  # GET_BIO response read
# NOTE: the prior fork also declared characteristic ...2001 (device name),
# ...2002/...2003 (WiFi SSID/password - BedJet 3's separate cloud/app Wi-Fi
# setup, irrelevant to BLE climate control) and ...2005 ("BIODATA", short).
# None of them were ever read from or written to anywhere in this fork's
# history (verified by grep across every revision of __init__.py) - removed
# as dead surface. Only ...2006 (BIODATA_FULL) is actually used, for GET_BIO
# responses.

# Clock hooks so tests can control elapsed-time math deterministically
# without real sleeps; production code always uses the real defaults.
# `_monotonic` drives every elapsed-seconds decision; `_utcnow` only stamps
# the wall-clock time of a drop for the HA layer to display.
_monotonic: Callable[[], float] = time.monotonic
_utcnow: Callable[[], datetime] = lambda: datetime.now(UTC)

# Command confirmation timeout (Contract: "each awaits confirming frame with
# timeout 5s").
COMMAND_TIMEOUT_S = 5.0

# Watchdog tiers. 300s/900s mirror ESPHome's own NOTIFY_WARN_THRESHOLD and
# DEFAULT_STATUS_TIMEOUT (bedjet_hub.h:148-149); 60s has no ESPHome citation
# and is this library's own addition - see codec.py docstring "[INFERENCE]
# tags summary".
WATCHDOG_PROBE_AFTER_S = 60.0
STATUS_TIMEOUT_S = 300.0
WATCHDOG_RECONNECT_AFTER_S = 900.0
WATCHDOG_TICK_S = 10.0

# Reconnect backoff: the fixed 1/2/5/10/30/60s schedule, capped at 60s and
# spread by +-20% jitter so several devices reconnecting after the same proxy
# reboot do not retry in lockstep. A held GATT link is the point of this
# integration, so the schedule never gives up - it just stops growing.
RECONNECT_BACKOFF_SCHEDULE_S = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
RECONNECT_BACKOFF_JITTER = 0.2

# Log one WARNING per this many consecutive connect failures: a proxy that
# genuinely cannot reach the unit would otherwise fill the log at the tail of
# the backoff schedule (once a minute, forever).
RECONNECT_WARN_EVERY = 10

# Trailing window for the drop counter the HA layer publishes as `drops_1h`.
DROP_WINDOW_S = 3600.0

# Tail re-read staleness threshold - ESPHome's own MIN_NOTIFY_THROTTLE
# (bedjet_hub.h:147).
TAIL_MAX_AGE_S = 15.0

# Listener fan-out rate limit for continuous-only changes (actual/ambient
# temperature). A mode/fan/target/notification/tail-flag change always
# publishes immediately regardless of this limit - see `is_meaningful_change`.
PUBLISH_MIN_INTERVAL_S = 2.0

# Upper bound on one connect+discover+subscribe attempt, so a hung transport
# (e.g. a misbehaving proxy hop) cannot park the reconnect supervisor forever.
CONNECT_ATTEMPT_TIMEOUT_S = 60.0

# Best-effort memory-name read retry budget (matches the prior fork's `tag <
# 2` loop bound).
BIO_READ_ATTEMPTS = 2

_MODE_TO_BUTTON: dict[BedJetMode, BedJetButton] = {
    BedJetMode.STANDBY: BedJetButton.OFF,
    BedJetMode.HEAT: BedJetButton.HEAT,
    BedJetMode.TURBO: BedJetButton.TURBO,
    BedJetMode.EXTENDED_HEAT: BedJetButton.EXTENDED_HEAT,
    BedJetMode.COOL: BedJetButton.COOL,
    BedJetMode.DRY: BedJetButton.DRY,
}


class BedJetError(Exception):
    """Base exception for every pybedjet error."""


class BedJetConnectionError(BedJetError):
    """Raised when a command is attempted while not connected."""


class BedJetCommandError(BedJetError):
    """Raised when a command's confirming frame did not arrive in time."""


class WatchdogAction(Enum):
    """Pure decision output of `watchdog_action`."""

    NONE = auto()
    PROBE = auto()
    UNAVAILABLE = auto()
    RECONNECT = auto()


def watchdog_action(elapsed_s: float) -> WatchdogAction:
    """Pure tier decision for the notify-stream watchdog.

    `elapsed_s` is seconds since the last valid decoded frame. See the
    Contract and codec.py's "[INFERENCE] tags summary" for provenance of the
    three thresholds.
    """
    if elapsed_s >= WATCHDOG_RECONNECT_AFTER_S:
        return WatchdogAction.RECONNECT
    if elapsed_s >= STATUS_TIMEOUT_S:
        return WatchdogAction.UNAVAILABLE
    if elapsed_s >= WATCHDOG_PROBE_AFTER_S:
        return WatchdogAction.PROBE
    return WatchdogAction.NONE


def reconnect_backoff_seconds(attempt: int, rng: random.Random | None = None) -> float:
    """Pure jittered backoff over the fixed `RECONNECT_BACKOFF_SCHEDULE_S`.

    `attempt` is a 0-indexed consecutive-failure count; it indexes the
    schedule and saturates at its last entry (60s), so the supervisor keeps
    retrying at ~1/minute forever rather than backing off into uselessness.
    The returned delay is the scheduled value scaled by
    `1 +- RECONNECT_BACKOFF_JITTER`. Pass a seeded `Random` for deterministic
    tests.
    """
    rng = rng or random.Random()
    index = min(max(attempt, 0), len(RECONNECT_BACKOFF_SCHEDULE_S) - 1)
    base = RECONNECT_BACKOFF_SCHEDULE_S[index]
    return base * rng.uniform(1.0 - RECONNECT_BACKOFF_JITTER, 1.0 + RECONNECT_BACKOFF_JITTER)


class DropTracker:
    """Trailing-window counter of *unexpected* link drops.

    Pure state plus arithmetic - no I/O, no clock of its own: callers pass
    the monotonic timestamp used for windowing and the wall-clock instant
    used for display, so a test can drive it with a fake clock. Only drops
    the integration did not ask for are recorded (see `_handle_disconnect`
    and the watchdog's RECONNECT tier); an intentional release
    (`stop()`/`hold_connection = False`) is not a drop.
    """

    def __init__(self, window_s: float = DROP_WINDOW_S) -> None:
        """Init an empty tracker over a trailing `window_s` window."""
        self._window_s = window_s
        self._times: deque[float] = deque()
        self._last_drop: datetime | None = None

    def record(self, at: float, when: datetime) -> None:
        """Record one drop stamped `at` (monotonic) / `when` (wall clock)."""
        self._times.append(at)
        self._last_drop = when

    def count(self, now: float) -> int:
        """Return drops inside the trailing window, pruning older entries."""
        cutoff = now - self._window_s
        while self._times and self._times[0] < cutoff:
            self._times.popleft()
        return len(self._times)

    @property
    def last_drop(self) -> datetime | None:
        """Wall-clock instant of the most recent drop, or None if never dropped.

        Deliberately not pruned by the window: "when did this link last
        break" stays useful long after the drop leaves the 1h count.
        """
        return self._last_drop


def _any_frame(_state: BedJetState) -> bool:
    """Confirmation predicate for commands with no specific field to check."""
    return True


def _parse_memory_name(chunk: bytes) -> str | None:
    """Best-effort decode of one 16-byte memory-name slot.

    [INFERENCE] Not documented by ESPHome; reverse-engineered by the prior
    fork's `_parse_bio_data_response`. `chunk[0] in (0, 1)` are both observed
    "no custom name" sentinels (historically rendered as the literal string
    "Default" / `None`); this library reports both as `None` so the HA layer
    can fall back to the preset's own "M1"/"M2"/"M3" label - a small,
    intentional behavior improvement over showing the word "Default" for an
    un-named preset.
    """
    if not chunk or chunk[0] in (0, 1):
        return None
    name = chunk.split(b"\x00", 1)[0].decode(errors="replace").strip()
    return name or None


def _parse_memory_names(data: bytes) -> tuple[str | None, str | None, str | None] | None:
    """Decode a GET_BIO(MEMORY_NAMES) response: [bio_type, tag, 3x16-byte name]."""
    payload = data[2:]
    if len(payload) < 48:
        return None
    m1, m2, m3 = (_parse_memory_name(payload[i * 16 : (i + 1) * 16]) for i in range(3))
    return (m1, m2, m3)


class BedJet:
    """Async BedJet V3 client over a shared Bluetooth stack.

    Owns exactly one BLE connection's lifecycle: connecting (only ever
    triggered by `hold_connection` plus an advertisement sighting, never a
    blind poll loop), subscribing to status notifications, reading the tail
    when it is stale or a command just completed, confirming every command
    against a real decoded frame, and a watchdog that notices a wedged
    connection. See the module and `codec.py` docstrings for the full design
    rationale.
    """

    def __init__(
        self,
        ble_device: BLEDevice,
        advertisement_data: AdvertisementData | None = None,
        *,
        hold_connection: bool = True,
        clock: Callable[[], datetime] | None = None,
        source: str | None = None,
        client_class: type[BleakClientWithServiceCache] | None = None,
    ) -> None:
        """Init the BedJet client. Does not connect - call `start()`."""
        self._ble_device = ble_device
        self._advertisement_data = advertisement_data
        self._scanner_source = source
        self._clock = clock
        self._hold_connection = hold_connection
        # Resolved from the live module attribute rather than this file's own
        # frozen top-of-file import: Home Assistant's `bluetooth` component
        # monkeypatches `bleak_retry_connector.BleakClientWithServiceCache`
        # into its own connection-tracking wrapper, and a caller building
        # this before that patch lands still needs the current class, not
        # whichever one existed when this module was first imported.
        self._client_class = client_class or bleak_retry_connector.BleakClientWithServiceCache
        # True if the most recent connect attempt used a caller-preferred
        # scanner. This class has no notion of "preferred" - whatever built
        # `client_class` (see ble_affinity.py) sets this via its on_choice
        # hook; Home Assistant's Connection sensor surfaces it.
        self.via_preferred_proxy = False

        self._client: BleakClientWithServiceCache | None = None
        self._state: BedJetState | None = None
        self._last_frame_at: float | None = None
        self._last_tail_read_at: float | None = None
        self._tail_refresh_needed = False
        self._last_published_at: float | None = None
        self._last_unavailable_notice_at: float | None = None

        self._callbacks: list[Callable[[BedJet], None]] = []
        self._pending: list[tuple[Callable[[BedJetState], bool], asyncio.Future[None]]] = []

        self._started = False
        self._stopped = True
        # Process-lifetime latch set by release_for_shutdown(); unlike
        # _stopped it is never cleared, so nothing can reconnect afterwards.
        self._closing = False
        self._reconnect_attempt = 0
        self._connect_wakeup = asyncio.Event()
        self._drops = DropTracker()

        self._connect_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._tail_read_task: asyncio.Task[None] | None = None
        self._bio_read_task: asyncio.Task[None] | None = None
        self._release_task: asyncio.Task[None] | None = None

        self._m1_name: str | None = None
        self._m2_name: str | None = None
        self._m3_name: str | None = None

    # -- identity / static properties ---------------------------------

    @property
    def address(self) -> str:
        """The device's BLE MAC address."""
        return self._ble_device.address

    @property
    def scanner_source(self) -> str | None:
        """The scanner (adapter or ESPHome proxy) that produced the last advertisement, if known."""
        return self._scanner_source

    # -- connection / freshness properties ------------------------------

    @property
    def connected(self) -> bool:
        """True if a BLE connection is currently established and subscribed."""
        return self._client is not None and self._client.is_connected

    @property
    def available(self) -> bool:
        """True if connected AND a valid frame arrived within `STATUS_TIMEOUT_S`."""
        return (
            self.connected
            and self._last_frame_at is not None
            and (_monotonic() - self._last_frame_at) < STATUS_TIMEOUT_S
        )

    @property
    def reconnect_attempt(self) -> int:
        """Consecutive failed connect attempts, or 0 whenever connected.

        This is the index the supervisor is currently backing off at, so a
        value of 0 while disconnected means "about to try" and a rising
        value means "still failing".
        """
        return 0 if self.connected else self._reconnect_attempt

    @property
    def drops_1h(self) -> int:
        """Unexpected disconnects within the trailing `DROP_WINDOW_S`."""
        return self._drops.count(_monotonic())

    @property
    def last_drop(self) -> datetime | None:
        """UTC instant of the most recent unexpected disconnect, or None."""
        return self._drops.last_drop

    @property
    def last_frame_at(self) -> float | None:
        """`time.monotonic()` timestamp of the last valid decoded frame, or None."""
        return self._last_frame_at

    @property
    def state(self) -> BedJetState | None:
        """The most recently decoded state, or None before the first valid frame."""
        return self._state

    # -- best-effort bio-data (memory preset names) --------------------

    @property
    def m1_name(self) -> str | None:
        """The user-configured name of the M1 memory preset, if read successfully."""
        return self._m1_name

    @property
    def m2_name(self) -> str | None:
        """The user-configured name of the M2 memory preset, if read successfully."""
        return self._m2_name

    @property
    def m3_name(self) -> str | None:
        """The user-configured name of the M3 memory preset, if read successfully."""
        return self._m3_name

    # -- hold_connection --------------------------------------------------

    @property
    def hold_connection(self) -> bool:
        """True = connect and maintain; False = disconnected and will not reconnect."""
        return self._hold_connection

    @hold_connection.setter
    def hold_connection(self, value: bool) -> None:
        if self._closing and value:
            return  # the shutdown latch outranks any request to hold the link
        if value == self._hold_connection:
            return
        self._hold_connection = value
        if value:
            self._reconnect_attempt = 0
        self._connect_wakeup.set()
        if not value and self._client is not None:
            self._release_task = asyncio.create_task(self._disconnect())

    # -- advertisement feed / callbacks --------------------------------

    def set_ble_device_and_advertisement_data(
        self,
        ble_device: BLEDevice,
        advertisement_data: AdvertisementData,
        *,
        source: str | None = None,
    ) -> None:
        """Feed a fresh advertisement sighting.

        An advertisement means the device's single BLE slot is currently
        free (a BedJet does not advertise while connected to anyone) - this
        is what wakes the reconnect supervisor, bypassing backoff early.
        """
        self._ble_device = ble_device
        self._advertisement_data = advertisement_data
        if source is not None:
            self._scanner_source = source
        self._connect_wakeup.set()

    def register_callback(self, callback: Callable[[BedJet], None]) -> Callable[[], None]:
        """Register a callback fired on every observable change.

        Fires on: every decoded frame (rate-limited per
        `is_meaningful_change`/`PUBLISH_MIN_INTERVAL_S`), every successful
        connect, every disconnect, and the watchdog's UNAVAILABLE tier.
        Returns a function that unregisters it.
        """
        self._callbacks.append(callback)

        def unregister() -> None:
            with contextlib.suppress(ValueError):
                self._callbacks.remove(callback)

        return unregister

    def _fire_callbacks(self) -> None:
        if self._closing:
            return
        for callback in list(self._callbacks):
            try:
                callback(self)
            except Exception:
                _LOGGER.exception("%s: registered callback raised", self.address)

    # -- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        """Begin the connection lifecycle. Idempotent; returns promptly.

        Refused for good once `release_for_shutdown()` has latched.

        Never blocks on, or raises for, the device not being reachable right
        now - it only arranges to keep trying (advertisement-triggered, with
        backoff) until `stop()` or `hold_connection = False`.
        """
        if self._started or self._closing:
            return
        self._started = True
        self._stopped = False
        self._reconnect_attempt = 0
        self._connect_wakeup.set()
        self._connect_task = asyncio.create_task(self._connect_supervisor())
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def stop(self) -> None:
        """Stop the connection lifecycle and disconnect. Idempotent."""
        self._stopped = True
        self._connect_wakeup.set()
        await self._cancel_background_tasks()
        await self._disconnect()
        self._started = False

    async def release_for_shutdown(self) -> None:
        """Drop the held link for good because Home Assistant is going away.

        Sets the closing latch FIRST (synchronously), so every path that could
        open a connection - supervisor, reconnect backoff, advertisement
        wakeups, watchdog, commands, `hold_connection = True`, `start()` -
        refuses from then on; then cancels the background tasks and completes
        a GATT disconnect. Observers are not notified: this is a deliberate
        release, not a state change worth publishing, and `hold_connection`
        is left as the user set it. Caller bounds the time it may take.
        """
        self._closing = True
        self._stopped = True
        self._connect_wakeup.set()
        await self._cancel_background_tasks()
        await self._disconnect()
        self._started = False

    async def _cancel_background_tasks(self) -> None:
        tasks = [t for t in (self._connect_task, self._watchdog_task, self._release_task) if t is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            # asyncio.wait, not awaiting each task: it never re-raises a
            # task's CancelledError, so a caller's own cancellation (the
            # shutdown timeout) cannot be mistaken for one and swallowed.
            await asyncio.wait(tasks)
        self._connect_task = None
        self._watchdog_task = None
        self._release_task = None

    # -- commands ---------------------------------------------------------
    # Every command awaits a confirming frame (never optimistic local state)
    # and raises BedJetConnectionError if not connected, or BedJetCommandError
    # if no confirming frame arrives within COMMAND_TIMEOUT_S.

    async def set_mode(self, mode: BedJetMode) -> None:
        """Press the button that switches to `mode`; confirmed by `state.mode`."""
        button = _MODE_TO_BUTTON.get(mode)
        if button is None:
            raise ValueError(f"{mode!r} cannot be set directly (no button exists for it)")
        await self._run_command(
            build_command(BedJetCommand.BUTTON, button),
            lambda state: state.mode == mode,
        )

    async def set_temperature_c(self, temperature_c: float) -> None:
        """Set the target temperature in Celsius; confirmed by `state.target_temp_c`."""
        target_raw = round(temperature_c * 2)
        await self._run_command(
            build_command(BedJetCommand.SET_TEMP, temperature_c),
            lambda state: round(state.target_temp_c * 2) == target_raw,
        )

    async def set_fan_percent(self, fan_percent: int) -> None:
        """Set the fan speed as a percent (5-100, multiple of 5); confirmed by `state.fan_percent`."""
        await self._run_command(
            build_command(BedJetCommand.SET_FAN, fan_percent),
            lambda state: state.fan_percent == fan_percent,
        )

    async def set_runtime(self, hours: int, minutes: int) -> None:
        """Set the absolute time remaining; confirmed by `state.time_remaining` (to the minute).

        Seconds are intentionally ignored for confirmation: `time_remaining`
        is a live countdown, so an exact-seconds comparison would be flaky
        against network/decode latency.
        """
        target_minutes = hours * 60 + minutes
        await self._run_command(
            build_command(BedJetCommand.SET_RUNTIME, hours, minutes),
            lambda state: int(state.time_remaining.total_seconds()) // 60 == target_minutes,
        )

    async def set_led(self, led: bool) -> None:
        """Enable/disable the LED ring; confirmed by `state.leds_enabled` (tail-derived)."""
        button = BedJetButton.LED_ON if led else BedJetButton.LED_OFF
        await self._run_command(
            build_command(BedJetCommand.BUTTON, button),
            lambda state: state.leds_enabled == led,
        )

    async def set_mute(self, muted: bool) -> None:
        """Mute/unmute beeps; confirmed by `state.beeps_muted` (tail-derived)."""
        button = BedJetButton.MUTE if muted else BedJetButton.UNMUTE
        await self._run_command(
            build_command(BedJetCommand.BUTTON, button),
            lambda state: state.beeps_muted == muted,
        )

    async def sync_clock(self) -> None:
        """Send the current time from the configured `clock`; resolves on the next valid frame."""
        if self._clock is None:
            raise BedJetError(f"{self.address}: no clock source configured")
        now = self._clock()
        await self._run_command(build_command(BedJetCommand.SET_CLOCK, now.hour, now.minute), _any_frame)

    async def acknowledge_notification(self) -> None:
        """Send NOTIFY_ACK; resolves on the next valid frame."""
        await self._run_command(build_command(BedJetCommand.BUTTON, BedJetButton.NOTIFY_ACK), _any_frame)

    async def press_button(self, button: BedJetButton) -> None:
        """Press an arbitrary button (e.g. an M1/M2/M3 preset); resolves on the next valid frame."""
        await self._run_command(build_command(BedJetCommand.BUTTON, button), _any_frame)

    async def request_status(self) -> None:
        """Send an explicit CMD_STATUS probe; resolves on the next valid frame.

        [INFERENCE] see codec.py docstring - ESPHome never sends this opcode
        itself; kept as a watchdog nudge per the ground truth.
        """
        await self._run_command(build_command(BedJetCommand.STATUS), _any_frame)

    async def request_firmware_update(self) -> None:
        """Press MAGIC_UPDATE, which reboots the unit; resolves on the next valid frame.

        A timeout here is an expected, honest outcome if the unit reboots
        before sending another frame - this call never assumes success.
        """
        await self._run_command(build_command(BedJetCommand.BUTTON, BedJetButton.UPDATE_FIRMWARE), _any_frame)

    async def _run_command(self, command_bytes: bytes, predicate: Callable[[BedJetState], bool]) -> None:
        if self._closing:
            raise BedJetConnectionError(f"{self.address}: Home Assistant is shutting down")
        if self._client is None or not self._client.is_connected:
            raise BedJetConnectionError(f"{self.address}: not connected")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        entry = (predicate, future)
        self._pending.append(entry)
        try:
            await self._client.write_gatt_char(COMMAND_UUID, command_bytes, response=False)
            self._tail_refresh_needed = True
            async with asyncio.timeout(COMMAND_TIMEOUT_S):
                await future
        except BleakError as err:
            raise BedJetConnectionError(f"{self.address}: write failed: {err}") from err
        except TimeoutError as err:
            raise BedJetCommandError(
                f"{self.address}: command {command_bytes.hex()} not confirmed within {COMMAND_TIMEOUT_S}s"
            ) from err
        finally:
            if entry in self._pending:
                self._pending.remove(entry)

    def _resolve_pending(self, state: BedJetState) -> None:
        if not self._pending:
            return
        remaining = []
        for predicate, future in self._pending:
            if future.done():
                continue
            if predicate(state):
                future.set_result(None)
            else:
                remaining.append((predicate, future))
        self._pending = remaining

    def _fail_pending(self, error: BedJetError) -> None:
        for _predicate, future in self._pending:
            if not future.done():
                future.set_exception(error)
        self._pending = []

    # -- connection supervisor -------------------------------------------

    async def _connect_supervisor(self) -> None:
        """The single task that owns connecting and reconnecting.

        Waits for a wakeup (advertisement, hold_connection toggle, disconnect,
        or stop()) whenever there is nothing to do; otherwise attempts one
        connection and backs off (with jitter) on failure. Never raises -
        must run for the lifetime of the client. The outer try/except is a
        last-resort guard: even a failure *inside* the inner handler (e.g. a
        logging call) must never terminate the loop that is this client's
        only path back to a working connection.
        """
        while not self._stopped:
            try:
                if not self._hold_connection or self.connected:
                    await self._wait_for_wakeup()
                    continue
                try:
                    await self._connect_once()
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001 - the supervisor must never die
                    self._reconnect_attempt += 1
                    delay = reconnect_backoff_seconds(self._reconnect_attempt - 1)
                    # One WARNING per RECONNECT_WARN_EVERY consecutive
                    # failures: enough to notice a link that never comes
                    # back, quiet enough to live with forever.
                    log = (
                        _LOGGER.warning
                        if self._reconnect_attempt % RECONNECT_WARN_EVERY == 0
                        else _LOGGER.debug
                    )
                    log(
                        "%s: connect attempt %d failed (%s); retrying in %.1fs",
                        self.address,
                        self._reconnect_attempt,
                        err,
                        delay,
                    )
                    # Publish so the Connection sensor's reconnect_attempt
                    # attribute tracks the live backoff instead of freezing
                    # at the value it had when the link dropped.
                    self._fire_callbacks()
                    await self._wait_for_wakeup(timeout=delay)
                else:
                    self._reconnect_attempt = 0
                    if not self._hold_connection:
                        # hold_connection was released while the connect was
                        # in flight; honor that now instead of staying connected.
                        await self._disconnect()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("unexpected error in BedJet connect supervisor")
                with contextlib.suppress(Exception):
                    await asyncio.sleep(RECONNECT_BACKOFF_SCHEDULE_S[0])

    async def _wait_for_wakeup(self, timeout: float | None = None) -> None:
        self._connect_wakeup.clear()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                await self._connect_wakeup.wait()

    async def _connect_once(self) -> None:
        if self._closing:
            raise BedJetConnectionError(f"{self.address}: Home Assistant is shutting down")
        async with asyncio.timeout(CONNECT_ATTEMPT_TIMEOUT_S):
            client = await establish_connection(
                self._client_class,
                self._ble_device,
                self.address,
                self._handle_disconnect,
                use_services_cache=True,
                ble_device_callback=lambda: self._ble_device,
            )
            try:
                await client.start_notify(STATUS_UUID, self._handle_notify)
            except BaseException:
                with contextlib.suppress(BleakError, OSError, EOFError):
                    await client.disconnect()
                raise

        self._client = client
        self._last_frame_at = _monotonic()
        self._last_tail_read_at = None
        _LOGGER.debug("%s: connected", self.address)
        self._fire_callbacks()

        if self._clock is not None:
            try:
                await self.sync_clock()
            except BedJetError as err:
                _LOGGER.debug("%s: clock sync after connect failed: %s", self.address, err)

        self._start_bio_read()

    def _record_drop(self) -> None:
        """Count one unexpected loss of the held link and log it at INFO."""
        self._drops.record(_monotonic(), _utcnow())
        _LOGGER.info(
            "%s: link dropped (%d in the last hour); reconnecting",
            self.address,
            self._drops.count(_monotonic()),
        )

    def _handle_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        """bleak's disconnected_callback - always sync, may fire for any disconnect reason.

        Reaching here always means the link went away without this library
        asking for it (an intentional release goes through `_disconnect`,
        which clears `_client` first and so returns early below), which is
        exactly the definition of a drop for `drops_1h`/`last_drop`.
        habluetooth itself does not count post-connect drops anywhere - it
        only scores *connect* failures - so this is the only place the
        information exists.
        """
        if self._client is None:
            return  # already torn down via our own _disconnect()
        self._record_drop()
        self._client = None
        self._last_tail_read_at = None
        if self._tail_read_task is not None:
            self._tail_read_task.cancel()
        if self._bio_read_task is not None:
            self._bio_read_task.cancel()
        self._connect_wakeup.set()
        self._fail_pending(BedJetConnectionError(f"{self.address}: disconnected"))
        self._fire_callbacks()

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        _LOGGER.debug("%s: disconnecting", self.address)
        self._last_tail_read_at = None
        if self._tail_read_task is not None:
            self._tail_read_task.cancel()
        if self._bio_read_task is not None:
            self._bio_read_task.cancel()
        if client.is_connected:
            with contextlib.suppress(BleakError, OSError, EOFError):
                await client.stop_notify(STATUS_UUID)
            with contextlib.suppress(BleakError, OSError, EOFError):
                await client.disconnect()
        self._fail_pending(BedJetConnectionError(f"{self.address}: disconnected"))
        self._fire_callbacks()

    # -- notifications / tail --------------------------------------------

    def _handle_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        """bleak's notify callback - always sync; schedules the tail read as a tracked task."""
        try:
            new_state = decode_frame(bytes(data), self._state)
        except BedJetFrameError as err:
            _LOGGER.debug("%s: rejected frame %s: %s", self.address, bytes(data).hex(), err)
            return

        now = _monotonic()
        previous = self._state
        self._state = new_state
        self._last_frame_at = now
        self._resolve_pending(new_state)

        if new_state.is_partial and (
            self._tail_refresh_needed
            or self._last_tail_read_at is None
            or now - self._last_tail_read_at >= TAIL_MAX_AGE_S
        ):
            self._schedule_tail_read()

        self._maybe_publish(previous, new_state, now)

    def _maybe_publish(self, previous: BedJetState | None, current: BedJetState, now: float) -> None:
        meaningful = is_meaningful_change(previous, current)
        stale = self._last_published_at is None or now - self._last_published_at >= PUBLISH_MIN_INTERVAL_S
        if meaningful or stale:
            self._last_published_at = now
            self._fire_callbacks()

    def _schedule_tail_read(self) -> None:
        if self._tail_read_task is not None and not self._tail_read_task.done():
            return  # already in flight; it will pick up any newer command's effect too
        self._tail_refresh_needed = False
        self._tail_read_task = asyncio.create_task(self._read_tail())

    async def _read_tail(self) -> None:
        client = self._client
        if client is None or not client.is_connected:
            return
        try:
            data = await client.read_gatt_char(STATUS_UUID)
        except (BleakError, OSError, EOFError) as err:
            _LOGGER.debug("%s: tail read failed: %s", self.address, err)
            return
        self._last_tail_read_at = _monotonic()
        if self._state is None:
            return
        try:
            new_state = merge_tail(self._state, bytes(data))
        except BedJetFrameError as err:
            _LOGGER.debug("%s: rejected tail %s: %s", self.address, bytes(data).hex(), err)
            return
        previous = self._state
        self._state = new_state
        self._resolve_pending(new_state)
        self._maybe_publish(previous, new_state, _monotonic())

    # -- watchdog ---------------------------------------------------------

    async def _watchdog_loop(self) -> None:
        """Periodic tick; never raises. See `watchdog_action` for the pure tier decision."""
        while not self._stopped:
            try:
                await asyncio.sleep(WATCHDOG_TICK_S)
                if not self.connected or self._last_frame_at is None:
                    continue
                elapsed = _monotonic() - self._last_frame_at
                action = watchdog_action(elapsed)
                if action is WatchdogAction.NONE:
                    continue
                if action is WatchdogAction.PROBE:
                    # Fire-and-forget: a confirmed request_status() would block
                    # this loop for up to COMMAND_TIMEOUT_S waiting for a frame
                    # that, by definition, is not arriving - the escalation to
                    # UNAVAILABLE/RECONNECT must not be delayed by that wait.
                    # If the probe *does* wake the device, the ordinary notify
                    # path updates last_frame_at on its own; nothing here needs
                    # to observe that outcome. The public `request_status()`
                    # stays a normal confirmed command for callers that want one.
                    _LOGGER.debug("%s: no frame for %.0fs, probing", self.address, elapsed)
                    await self._write_raw_command(build_command(BedJetCommand.STATUS))
                elif action is WatchdogAction.UNAVAILABLE:
                    if self._last_unavailable_notice_at != self._last_frame_at:
                        self._last_unavailable_notice_at = self._last_frame_at
                        _LOGGER.warning("%s: no frame for %.0fs, marking unavailable", self.address, elapsed)
                        self._fire_callbacks()
                elif action is WatchdogAction.RECONNECT:
                    # A wedged link that stopped streaming is a lost hold as
                    # far as anyone downstream is concerned, so it counts as
                    # a drop even though bleak never reported a disconnect
                    # (this `_disconnect` clears `_client` first, so the
                    # bleak callback that follows will not double-count).
                    _LOGGER.warning("%s: no frame for %.0fs, forcing reconnect", self.address, elapsed)
                    self._record_drop()
                    await self._disconnect()
                    self._connect_wakeup.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("unexpected error in BedJet watchdog loop")

    async def _write_raw_command(self, command_bytes: bytes) -> None:
        """Best-effort write with no confirmation. Used only by the watchdog probe."""
        client = self._client
        if client is None or not client.is_connected:
            return
        with contextlib.suppress(BleakError, OSError, EOFError):
            await client.write_gatt_char(COMMAND_UUID, command_bytes, response=False)

    # -- best-effort bio-data (memory preset names) ----------------------

    def _start_bio_read(self) -> None:
        if self._bio_read_task is not None and not self._bio_read_task.done():
            return
        self._bio_read_task = asyncio.create_task(self._read_memory_names())

    async def _read_memory_names(self) -> None:
        """Best-effort, non-fatal read of the M1/M2/M3 preset names.

        [INFERENCE] Entirely absent from ESPHome; kept per the assignment's
        explicit carve-out (knowledge ESPHome lacks). Any failure here is
        logged at debug and never surfaces to a caller or affects `state`.
        """
        for attempt in range(BIO_READ_ATTEMPTS):
            client = self._client
            if client is None or not client.is_connected:
                return
            try:
                await client.write_gatt_char(
                    COMMAND_UUID,
                    build_command(BedJetCommand.GET_BIO, BioDataRequest.MEMORY_NAMES, attempt),
                    response=False,
                )
                data = await client.read_gatt_char(BIODATA_FULL_UUID)
            except (BleakError, OSError, EOFError) as err:
                _LOGGER.debug("%s: memory-name read attempt %d failed: %s", self.address, attempt, err)
                continue
            names = _parse_memory_names(bytes(data))
            if names is not None:
                self._m1_name, self._m2_name, self._m3_name = names
                return
        _LOGGER.debug("%s: could not read memory preset names", self.address)
