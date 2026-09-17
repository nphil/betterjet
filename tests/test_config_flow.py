"""Tests for the BedJet config flow: discovery matching, connectivity probe
outcomes, and the manual-pick step's discovered-device filtering.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import custom_components.bedjet.config_flow as config_flow
from custom_components.bedjet.config_flow import BedjetDeviceConfigFlow, _is_bedjet
from custom_components.bedjet.const import BEDJET_SERVICE_UUID, CONF_PREFERRED_PROXY
from homeassistant.const import CONF_ADDRESS

ADDRESS = "FC:F5:C4:20:1A:92"


def make_service_info(
    *, address: str = ADDRESS, name: str = "BEDJET_V3", service_uuids=None
) -> SimpleNamespace:
    return SimpleNamespace(
        address=address,
        name=name,
        device=object(),
        advertisement=object(),
        source="D4:D4:DA:9D:40:8A",
        service_uuids=service_uuids if service_uuids is not None else [BEDJET_SERVICE_UUID],
    )


class TestIsBedjet:
    def test_matches_service_uuid(self) -> None:
        info = make_service_info(name="anything", service_uuids=[BEDJET_SERVICE_UUID])
        assert _is_bedjet(info) is True

    def test_matches_name_prefix_case_insensitive(self) -> None:
        info = make_service_info(name="bedjet_v3", service_uuids=[])
        assert _is_bedjet(info) is True

    def test_rejects_unrelated_device(self) -> None:
        info = make_service_info(name="SmartBulb", service_uuids=["0000ffff-0000-1000-8000-00805f9b34fb"])
        assert _is_bedjet(info) is False


class FakeBedJetProbe:
    """Controls _async_probe's outcome without touching the real library."""

    behavior = "ok"  # "ok" | "timeout" | "error"

    def __init__(self, ble_device, advertisement_data, *, source=None, clock=None) -> None:
        self.stopped = False

    def register_callback(self, callback):
        self._callback = callback
        return lambda: None

    async def start(self) -> None:
        if FakeBedJetProbe.behavior == "ok":
            self._callback(self)
        elif FakeBedJetProbe.behavior == "error":
            raise RuntimeError("boom")
        # "timeout": never fires the callback

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture(autouse=True)
def _patch_bedjet_and_timeout(monkeypatch):
    monkeypatch.setattr(config_flow, "BedJet", FakeBedJetProbe)
    monkeypatch.setattr(config_flow, "PROBE_TIMEOUT", 0.01)
    FakeBedJetProbe.behavior = "ok"
    yield
    FakeBedJetProbe.behavior = "ok"


def make_flow() -> BedjetDeviceConfigFlow:
    flow = BedjetDeviceConfigFlow()
    flow.hass = object()
    flow.context = {}
    return flow


class TestBluetoothDiscoveryStep:
    def test_successful_probe_advances_to_confirm(self) -> None:
        flow = make_flow()
        info = make_service_info()

        result = asyncio.run(flow.async_step_bluetooth(info))

        assert result["step_id"] == "bluetooth_confirm"

    def test_probe_timeout_aborts_with_cannot_connect(self) -> None:
        FakeBedJetProbe.behavior = "timeout"
        flow = make_flow()
        info = make_service_info()

        result = asyncio.run(flow.async_step_bluetooth(info))

        assert result == {"type": "abort", "reason": "cannot_connect"}

    def test_probe_exception_aborts_with_unknown(self) -> None:
        FakeBedJetProbe.behavior = "error"
        flow = make_flow()
        info = make_service_info()

        result = asyncio.run(flow.async_step_bluetooth(info))

        assert result == {"type": "abort", "reason": "unknown"}

    def test_confirm_step_creates_entry_with_address(self) -> None:
        flow = make_flow()
        info = make_service_info()
        asyncio.run(flow.async_step_bluetooth(info))

        result = asyncio.run(flow.async_step_bluetooth_confirm({}))

        assert result["type"] == "create_entry"
        assert result["data"] == {CONF_ADDRESS: ADDRESS}


class TestUserStep:
    def test_no_devices_found_aborts(self, monkeypatch) -> None:
        monkeypatch.setattr(config_flow, "async_discovered_service_info", lambda hass: [])
        flow = make_flow()

        result = asyncio.run(flow.async_step_user(None))

        assert result == {"type": "abort", "reason": "no_devices_found"}

    def test_filters_out_non_bedjet_and_already_configured(self, monkeypatch) -> None:
        bedjet_info = make_service_info(address="AA:AA:AA:AA:AA:AA")
        other_info = make_service_info(
            address="BB:BB:BB:BB:BB:BB", name="SmartBulb", service_uuids=[]
        )
        monkeypatch.setattr(
            config_flow,
            "async_discovered_service_info",
            lambda hass: [bedjet_info, other_info],
        )
        flow = make_flow()

        result = asyncio.run(flow.async_step_user(None))

        choices = result["data_schema"].schema[next(iter(result["data_schema"].schema))].container
        assert "AA:AA:AA:AA:AA:AA" in choices
        assert "BB:BB:BB:BB:BB:BB" not in choices

    def test_picking_a_device_probes_and_creates_entry(self, monkeypatch) -> None:
        info = make_service_info(address="AA:AA:AA:AA:AA:AA")
        monkeypatch.setattr(
            config_flow, "async_discovered_service_info", lambda hass: [info]
        )
        flow = make_flow()
        asyncio.run(flow.async_step_user(None))  # populate _discovered_devices

        result = asyncio.run(
            flow.async_step_user({CONF_ADDRESS: "AA:AA:AA:AA:AA:AA"})
        )

        assert result["type"] == "create_entry"
        assert result["data"] == {CONF_ADDRESS: "AA:AA:AA:AA:AA:AA"}

    def test_picking_a_device_that_fails_probe_shows_form_with_error(self, monkeypatch) -> None:
        FakeBedJetProbe.behavior = "timeout"
        info = make_service_info(address="AA:AA:AA:AA:AA:AA")
        monkeypatch.setattr(
            config_flow, "async_discovered_service_info", lambda hass: [info]
        )
        flow = make_flow()
        asyncio.run(flow.async_step_user(None))

        result = asyncio.run(
            flow.async_step_user({CONF_ADDRESS: "AA:AA:AA:AA:AA:AA"})
        )

        assert result["type"] == "form"
        assert result["errors"]["base"] == "cannot_connect"


class TestOptionsFlow:
    """The "preferred proxy" option: its form lists live connectable
    scanners, keeps a configured-but-offline choice visible, and saving it
    creates the entry with exactly what was submitted (bedjet's options
    dict holds nothing else that would need preserving).
    """

    def make_options_flow(self, entry: SimpleNamespace) -> config_flow.OptionsFlowHandler:
        flow = config_flow.OptionsFlowHandler(entry)
        flow.hass = object()
        return flow

    def test_async_get_options_flow_returns_a_handler(self) -> None:
        entry = SimpleNamespace(options={})
        result = BedjetDeviceConfigFlow.async_get_options_flow(entry)
        assert isinstance(result, config_flow.OptionsFlowHandler)

    def test_form_lists_connectable_scanners_sorted_with_automatic_first(
        self, monkeypatch
    ) -> None:
        scanners = [
            SimpleNamespace(adapter="plant-room-bluetooth-proxy", connectable=True),
            SimpleNamespace(adapter="attic-bluetooth-proxy", connectable=True),
            SimpleNamespace(adapter="busy-proxy", connectable=False),
            SimpleNamespace(adapter=None, connectable=True),
        ]
        monkeypatch.setattr(config_flow, "async_current_scanners", lambda hass: scanners)
        flow = self.make_options_flow(SimpleNamespace(options={}))

        result = asyncio.run(flow.async_step_init())

        assert result["type"] == "form"
        assert result["step_id"] == "init"
        selector = result["data_schema"].schema[CONF_PREFERRED_PROXY]
        assert selector.config["options"] == [
            "",
            "attic-bluetooth-proxy",
            "plant-room-bluetooth-proxy",
        ]
        assert selector.config["custom_value"] is True

    def test_form_keeps_a_configured_proxy_that_is_currently_offline(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(config_flow, "async_current_scanners", lambda hass: [])
        entry = SimpleNamespace(options={CONF_PREFERRED_PROXY: "offline-proxy"})
        flow = self.make_options_flow(entry)

        result = asyncio.run(flow.async_step_init())

        selector = result["data_schema"].schema[CONF_PREFERRED_PROXY]
        assert selector.config["options"] == ["", "offline-proxy"]

    def test_submitting_creates_the_entry_with_exactly_the_chosen_value(
        self, monkeypatch
    ) -> None:
        flow = self.make_options_flow(SimpleNamespace(options={}))

        result = asyncio.run(
            flow.async_step_init({CONF_PREFERRED_PROXY: "plant-room-bluetooth-proxy"})
        )

        assert result == {
            "type": "create_entry",
            "title": "",
            "data": {CONF_PREFERRED_PROXY: "plant-room-bluetooth-proxy"},
        }
