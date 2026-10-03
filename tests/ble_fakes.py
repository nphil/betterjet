"""Fakes for bleak / bleak_retry_connector used by pybedjet connection tests.

WHY: pybedjet talks to a real BLE peripheral through
``bleak_retry_connector.establish_connection()``, which in production hands
back a connected ``BleakClient``. Tests replace that call with
``FakeBleakClient``, a duck-typed stand-in exposing the same public surface
(``connect``/``disconnect``/``write_gatt_char``/``start_notify``/
``stop_notify``/``read_gatt_char``) that records every write, can push
notification frames on demand, and can simulate a mid-session disconnect -
without BLE hardware or a running Home Assistant.

Real ``bleak`` and ``bleak-retry-connector`` are installed (see
requirements_test.txt) so ``BLEDevice``/``AdvertisementData`` construction
exercises the real, tiny, backend-independent dataclasses instead of a
hand-rolled copy that could drift from their field names.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError

BEDJET_SERVICE_UUID = "00001000-bed0-0080-aa55-4265644a6574"
BEDJET_STATUS_UUID = "00002000-bed0-0080-aa55-4265644a6574"
BEDJET_COMMAND_UUID = "00002004-bed0-0080-aa55-4265644a6574"

DEFAULT_ADDRESS = "FC:F5:C4:20:1A:92"
DEFAULT_LOCAL_NAME = "BEDJET_V3"
DEFAULT_SOURCE = "D4:D4:DA:9D:40:8A"  # the ESPHome proxy's own MAC


def make_ble_device(
    address: str = DEFAULT_ADDRESS, name: str = DEFAULT_LOCAL_NAME
) -> BLEDevice:
    """Build a real bleak BLEDevice, matching the ground-truth BedJet identity."""
    return BLEDevice(address=address, name=name, details={"path": "/fake/dev"})


def make_advertisement_data(
    *,
    rssi: int = -70,
    local_name: str = DEFAULT_LOCAL_NAME,
    service_uuids: list[str] | None = None,
) -> AdvertisementData:
    """Build a real bleak AdvertisementData for the BedJet's V3 identity."""
    return AdvertisementData(
        local_name=local_name,
        manufacturer_data={},
        service_data={},
        service_uuids=service_uuids or [BEDJET_SERVICE_UUID],
        tx_power=None,
        rssi=rssi,
        platform_data=(),
    )


class FakeBleakGATTCharacteristic:
    """Minimal stand-in so start_notify/write callbacks receive *something* char-like."""

    def __init__(self, uuid: str) -> None:
        self.uuid = uuid


class FakeBleakClient:
    """Duck-typed bleak.BleakClient replacement.

    Construction mirrors ``bleak_retry_connector.establish_connection``'s
    call shape: ``FakeBleakClient(ble_device, disconnected_callback=...)``.
    Tests reach into ``.writes`` to assert on exact bytes sent to the command
    characteristic, call ``.notify(char_uuid, payload)`` to simulate an
    incoming status frame, and call ``.simulate_disconnect()`` to simulate
    the BedJet dropping the link (e.g. because the phone app grabbed it).
    """

    #: class-level toggle so a test can make the *next* connect attempt fail,
    #: simulating "slot taken by the phone app" without touching call sites.
    fail_next_connects: int = 0

    def __init__(
        self,
        address_or_ble_device: BLEDevice | str,
        disconnected_callback: Callable[[Any], None] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.address = (
            address_or_ble_device
            if isinstance(address_or_ble_device, str)
            else address_or_ble_device.address
        )
        self._disconnected_callback = disconnected_callback
        self._connected = False
        self.writes: list[bytes] = []
        self.reads: list[str] = []
        self.read_kwargs: list[dict[str, Any]] = []
        self.notify_kwargs: list[dict[str, Any]] = []
        self._notify_callbacks: dict[str, Callable[[BleakGATTCharacteristic, bytearray], None]] = {}
        self._char_values: dict[str, bytes] = {}
        self.connect_calls = 0
        self.disconnect_calls = 0

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self, **kwargs: Any) -> bool:
        self.connect_calls += 1
        if FakeBleakClient.fail_next_connects > 0:
            FakeBleakClient.fail_next_connects -= 1
            raise BleakError("simulated connect failure (slot taken)")
        self._connected = True
        return True

    async def disconnect(self) -> bool:
        self.disconnect_calls += 1
        was_connected = self._connected
        self._connected = False
        if was_connected and self._disconnected_callback is not None:
            self._disconnected_callback(self)
        return True

    async def write_gatt_char(
        self, char_specifier: str, data: bytes | bytearray, response: bool = False
    ) -> None:
        if not self._connected:
            raise BleakError("Not connected")
        self.writes.append(bytes(data))

    async def read_gatt_char(self, char_specifier: str, **kwargs: Any) -> bytearray:
        if not self._connected:
            raise BleakError("Not connected")
        self.read_kwargs.append(kwargs)
        self.reads.append(char_specifier)
        return bytearray(self._char_values.get(char_specifier, b""))

    async def start_notify(
        self,
        char_specifier: str,
        callback: Callable[[BleakGATTCharacteristic, bytearray], None],
        **kwargs: Any,
    ) -> None:
        self.notify_kwargs.append(kwargs)
        self._notify_callbacks[char_specifier] = callback

    async def stop_notify(self, char_specifier: str) -> None:
        self._notify_callbacks.pop(char_specifier, None)

    def set_read_value(self, char_specifier: str, value: bytes) -> None:
        """Prime what a subsequent read_gatt_char(char_specifier) returns."""
        self._char_values[char_specifier] = value

    def notify(self, char_specifier: str, payload: bytes) -> None:
        """Simulate the peripheral pushing a notification frame."""
        callback = self._notify_callbacks.get(char_specifier)
        if callback is None:
            raise AssertionError(
                f"no start_notify registered for {char_specifier!r}; "
                "the library must subscribe before frames can arrive"
            )
        callback(FakeBleakGATTCharacteristic(char_specifier), bytearray(payload))

    def simulate_disconnect(self) -> None:
        """Simulate the peripheral tearing down the link without our request."""
        if not self._connected:
            return
        self._connected = False
        if self._disconnected_callback is not None:
            self._disconnected_callback(self)


class FakeBleakClientFactory:
    """Records every FakeBleakClient created, for connect-attempt-count assertions."""

    def __init__(self) -> None:
        self.clients: list[FakeBleakClient] = []

    def __call__(self, address_or_ble_device, disconnected_callback=None, *args, **kwargs):
        client = FakeBleakClient(address_or_ble_device, disconnected_callback, *args, **kwargs)
        self.clients.append(client)
        return client

    @property
    def last(self) -> FakeBleakClient:
        if not self.clients:
            raise AssertionError("no BleakClient was constructed yet")
        return self.clients[-1]


def make_fake_establish_connection(factory: FakeBleakClientFactory):
    """Build a drop-in replacement for bleak_retry_connector.establish_connection.

    Mirrors the real signature closely enough for pybedjet's call site:
    connects the fake client (raising on a primed failure) and returns it,
    exactly like the real helper does after its retry loop succeeds.
    """

    async def _fake_establish_connection(
        client_class,
        device,
        name,
        disconnected_callback=None,
        max_attempts: int = 4,
        **kwargs: Any,
    ):
        last_error: Exception | None = None
        for _ in range(max_attempts):
            client = factory(device, disconnected_callback)
            try:
                await client.connect()
            except BleakError as err:  # pragma: no branch - simple retry loop
                last_error = err
                await asyncio.sleep(0)
                continue
            return client
        raise last_error or BleakError("establish_connection failed")

    return _fake_establish_connection
