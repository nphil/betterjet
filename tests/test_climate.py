"""Tests for the BedJet climate entity.

Covers: static 19-43C limits regardless of the per-mode min/max a status
frame reports (upstream issue #61 churn), fan_modes 5%..100%, hvac/preset
mapping, command-error -> HomeAssistantError mapping, and the
deferred-setpoint state machine for a unit in standby.

`FakeDevice` mirrors one non-obvious pybedjet property: a command does not
return until a frame confirms it, and that frame is decoded and fanned out
to entities *synchronously* from inside the notify handler - so an entity's
`_async_update_attrs` has already run against the new state by the time the
awaiting service call resumes. Deferred-setpoint bookkeeping is only correct
if it survives that, so the fake reproduces the ordering rather than
returning silently.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.bedjet.climate import (
    MAX_TEMP_C,
    MIN_TEMP_C,
    PRESET_EXTENDED_HEAT,
    PRESET_NONE,
    PRESET_TURBO,
    BedJetClimateEntity,
)
from custom_components.bedjet.pybedjet import BedJetCommandError
from custom_components.bedjet.pybedjet.const import BedJetButton, BedJetMode
from homeassistant.components.climate import HVACMode
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_WHOLE
from homeassistant.exceptions import HomeAssistantError

ADDRESS = "FC:F5:C4:20:1A:92"


def make_state(**overrides):
    base = dict(
        mode=BedJetMode.STANDBY,
        actual_temp_c=25.0,
        target_temp_c=40.0,
        fan_percent=5,
        min_temp_c=10.0,
        max_temp_c=40.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeDevice:
    def __init__(self) -> None:
        self.address = ADDRESS
        self.mode_calls: list[BedJetMode] = []
        self.fan_calls: list[int] = []
        self.temp_calls: list[float] = []
        self.button_calls: list = []
        self.raise_on_command: Exception | None = None
        self.coordinator: FakeCoordinator | None = None

    def _confirm(self, **changes) -> None:
        """Publish the frame that confirms the command just written."""
        coordinator = self.coordinator
        if coordinator is None or coordinator.data is None:
            return
        coordinator.push(make_state(**{**vars(coordinator.data), **changes}))

    async def set_mode(self, mode) -> None:
        if self.raise_on_command:
            raise self.raise_on_command
        self.mode_calls.append(mode)
        self._confirm(mode=mode)

    async def set_fan_percent(self, percent: int) -> None:
        if self.raise_on_command:
            raise self.raise_on_command
        self.fan_calls.append(percent)
        self._confirm(fan_percent=percent)

    async def set_temperature_c(self, celsius: float) -> None:
        if self.raise_on_command:
            raise self.raise_on_command
        self.temp_calls.append(celsius)
        self._confirm(target_temp_c=celsius)

    async def press_button(self, button) -> None:
        if self.raise_on_command:
            raise self.raise_on_command
        self.button_calls.append(button)
        self._confirm()


class FakeCoordinator:
    def __init__(self, device: FakeDevice) -> None:
        self.device = device
        self.available = True
        self.data = make_state()
        self._listeners: list = []
        device.coordinator = self

    def async_add_listener(self, update_callback, context=None):
        self._listeners.append(update_callback)
        return lambda: self._listeners.remove(update_callback)

    def push(self, state) -> None:
        """Fan a decoded frame out to entities like the real coordinator."""
        self.data = state
        for update_callback in list(self._listeners):
            update_callback()


def make_entity(device: FakeDevice | None = None) -> tuple[BedJetClimateEntity, FakeCoordinator]:
    device = device or FakeDevice()
    coordinator = FakeCoordinator(device)
    return BedJetClimateEntity(coordinator, "Bedjetty"), coordinator


class TestStaticLimits:
    def test_unique_id_is_bare_address(self) -> None:
        entity, _coordinator = make_entity()
        assert entity.unique_id == ADDRESS

    def test_min_max_ignore_per_mode_frame_values(self) -> None:
        entity, coordinator = make_entity()
        coordinator.data = make_state(min_temp_c=5.0, max_temp_c=99.0)
        entity._async_update_attrs()

        assert entity.min_temp == MIN_TEMP_C == 19.0
        assert entity.max_temp == MAX_TEMP_C == 43.0

    def test_hvac_modes_are_off_heat_cool_dry(self) -> None:
        entity, _coordinator = make_entity()
        assert entity.hvac_modes == [
            HVACMode.OFF,
            HVACMode.HEAT,
            HVACMode.COOL,
            HVACMode.DRY,
        ]

    def test_fan_modes_are_5_to_100_percent(self) -> None:
        entity, _coordinator = make_entity()
        assert entity.fan_modes[0] == "5%"
        assert entity.fan_modes[-1] == "100%"
        assert len(entity.fan_modes) == 20

class TestBeforeFirstFrame:
    def test_state_properties_resolve_with_no_coordinator_data(self) -> None:
        """A listener fan-out before the first decoded frame must not raise.

        Live failure: HA computed the climate state while coordinator.data was
        None and ClimateEntity's hvac_mode/fan_mode/preset_mode backing
        attributes had never been assigned (AttributeError in the listener).
        """
        coordinator = FakeCoordinator(FakeDevice())
        coordinator.data = None
        entity = BedJetClimateEntity(coordinator, "Bedjetty")

        assert entity.hvac_mode is None
        assert entity.fan_mode is None
        assert entity.preset_mode is None



class TestModeMapping:
    @pytest.mark.parametrize(
        ("mode", "hvac_mode"),
        [
            (BedJetMode.STANDBY, HVACMode.OFF),
            (BedJetMode.WAIT, HVACMode.OFF),
            (BedJetMode.COOL, HVACMode.COOL),
            (BedJetMode.DRY, HVACMode.DRY),
            (BedJetMode.HEAT, HVACMode.HEAT),
            (BedJetMode.TURBO, HVACMode.HEAT),
            (BedJetMode.EXTENDED_HEAT, HVACMode.HEAT),
        ],
    )
    def test_frame_mode_maps_to_hvac_mode(self, mode, hvac_mode) -> None:
        entity, coordinator = make_entity()
        coordinator.data = make_state(mode=mode)
        entity._async_update_attrs()
        assert entity.hvac_mode == hvac_mode

    @pytest.mark.parametrize(
        ("mode", "preset"),
        [
            (BedJetMode.TURBO, PRESET_TURBO),
            (BedJetMode.EXTENDED_HEAT, PRESET_EXTENDED_HEAT),
            (BedJetMode.HEAT, PRESET_NONE),
            (BedJetMode.STANDBY, PRESET_NONE),
        ],
    )
    def test_frame_mode_maps_to_preset(self, mode, preset) -> None:
        entity, coordinator = make_entity()
        coordinator.data = make_state(mode=mode)
        entity._async_update_attrs()
        assert entity.preset_mode == preset


class TestCurrentTemperatureQuantization:
    """current_temperature publishes whole degrees with hysteresis - see
    temperature.py. Never applies to target_temperature (see
    TestTargetTemperatureIsNeverQuantized): that is a setpoint the user
    chose, not a measured value with its own noise to smooth out.
    """

    def test_precision_is_whole_degrees(self) -> None:
        entity, _coordinator = make_entity()
        assert entity.precision == PRECISION_WHOLE

    def test_first_reading_publishes_a_whole_number(self) -> None:
        coordinator = FakeCoordinator(FakeDevice())
        coordinator.data = make_state(actual_temp_c=24.7)
        entity = BedJetClimateEntity(coordinator, "Bedjetty")
        assert entity.current_temperature == 25

    def test_dither_across_a_boundary_does_not_keep_changing(self) -> None:
        # The unit dithers by exactly one 0.5C step while parked on a
        # boundary (measured live: current_temperature alternated 76/77F -
        # 24.5/25.0C - every ~2s for hours, 1132 recorder rows in 7.5h).
        coordinator = FakeCoordinator(FakeDevice())
        coordinator.data = make_state(actual_temp_c=24.5)
        entity = BedJetClimateEntity(coordinator, "Bedjetty")
        readings = [entity.current_temperature]
        for raw in (25.0, 24.5, 25.0):
            coordinator.push(make_state(actual_temp_c=raw))
            readings.append(entity.current_temperature)
        # round(24.5) == 24 seeds the first reading; the very next reading
        # (25.0) is a full 1C from that seed - past the 0.8C deadband - so
        # it takes one settling step to lock on. Every reading after that
        # must be identical, unlike the raw signal, which keeps alternating.
        assert readings[0] == 24
        assert readings[1] == readings[2] == readings[3] == 25

    def test_real_move_past_the_deadband_does_get_published(self) -> None:
        entity, coordinator = make_entity()
        assert entity.current_temperature == 25.0  # construction reading
        coordinator.push(make_state(actual_temp_c=26.0))
        assert entity.current_temperature == 26.0


class TestTargetTemperatureIsNeverQuantized:
    """A setpoint the user chose must be reported back exactly as set."""

    def test_whole_number_setpoint_is_unaffected(self) -> None:
        entity, coordinator = make_entity()
        coordinator.push(make_state(target_temp_c=33.0))
        assert entity.target_temperature == 33.0

    def test_half_degree_setpoint_is_reported_exactly(self) -> None:
        entity, coordinator = make_entity()
        coordinator.push(make_state(target_temp_c=24.5))
        assert entity.target_temperature == 24.5


class TestServiceCalls:
    def test_set_hvac_mode_heat_sends_heat_mode(self) -> None:
        device = FakeDevice()
        entity, _coordinator = make_entity(device)
        asyncio.run(entity.async_set_hvac_mode(HVACMode.HEAT))
        assert device.mode_calls == [BedJetMode.HEAT]

    def test_set_hvac_mode_unsupported_raises_home_assistant_error(self) -> None:
        entity, _coordinator = make_entity()
        with pytest.raises(HomeAssistantError):
            asyncio.run(entity.async_set_hvac_mode(HVACMode.FAN_ONLY))

    def test_set_preset_none_from_turbo_reverts_to_heat(self) -> None:
        device = FakeDevice()
        entity, coordinator = make_entity(device)
        coordinator.data = make_state(mode=BedJetMode.TURBO)
        asyncio.run(entity.async_set_preset_mode(PRESET_NONE))
        assert device.mode_calls == [BedJetMode.HEAT]

    def test_set_preset_none_from_extended_heat_reverts_to_heat(self) -> None:
        device = FakeDevice()
        entity, coordinator = make_entity(device)
        coordinator.data = make_state(mode=BedJetMode.EXTENDED_HEAT)
        asyncio.run(entity.async_set_preset_mode(PRESET_NONE))
        assert device.mode_calls == [BedJetMode.HEAT]

    def test_set_preset_none_from_cool_is_a_no_op(self) -> None:
        device = FakeDevice()
        entity, coordinator = make_entity(device)
        coordinator.data = make_state(mode=BedJetMode.COOL)
        asyncio.run(entity.async_set_preset_mode(PRESET_NONE))
        assert device.mode_calls == []

    def test_set_preset_turbo_sends_turbo_mode(self) -> None:
        device = FakeDevice()
        entity, _coordinator = make_entity(device)
        asyncio.run(entity.async_set_preset_mode(PRESET_TURBO))
        assert device.mode_calls == [BedJetMode.TURBO]

    def test_set_preset_invalid_raises_home_assistant_error(self) -> None:
        entity, _coordinator = make_entity()
        with pytest.raises(HomeAssistantError):
            asyncio.run(entity.async_set_preset_mode("not-a-real-preset"))

    def test_set_fan_mode_strips_percent_sign(self) -> None:
        device = FakeDevice()
        entity, _coordinator = make_entity(device)
        asyncio.run(entity.async_set_fan_mode("55%"))
        assert device.fan_calls == [55]

    def test_set_temperature_while_running_writes_immediately(self) -> None:
        device = FakeDevice()
        entity, coordinator = make_entity(device)
        coordinator.push(make_state(mode=BedJetMode.HEAT))

        asyncio.run(entity.async_set_temperature(**{ATTR_TEMPERATURE: 38.0}))

        assert device.temp_calls == [38.0]

    def test_command_failure_surfaces_as_home_assistant_error(self) -> None:
        device = FakeDevice()
        device.raise_on_command = BedJetCommandError("confirmation timed out")
        entity, _coordinator = make_entity(device)
        with pytest.raises(HomeAssistantError):
            asyncio.run(entity.async_set_hvac_mode(HVACMode.HEAT))


class TestDeferredSetpointInStandby:
    """The unit silently ignores SET_TEMP while in standby (verified live:
    the confirming frame never arrives and the command burns the full 5s
    COMMAND_TIMEOUT_S). HomeKit writes TargetTemperature unconditionally, so
    the value is held locally and written when the unit leaves standby.
    """

    def test_set_while_off_does_not_write_but_shows_immediately(self) -> None:
        device = FakeDevice()
        entity, _coordinator = make_entity(device)
        writes_before = entity.write_ha_state_calls

        asyncio.run(entity.async_set_temperature(**{ATTR_TEMPERATURE: 38.0}))

        assert device.temp_calls == []  # nothing hit the wire: no 5s stall
        assert entity.target_temperature == 38.0
        assert entity.write_ha_state_calls > writes_before

    def test_deferred_value_survives_frames_that_still_report_standby(self) -> None:
        device = FakeDevice()
        entity, coordinator = make_entity(device)
        asyncio.run(entity.async_set_temperature(**{ATTR_TEMPERATURE: 38.0}))

        # The device keeps streaming its own (unchanged) target at ~4Hz.
        coordinator.push(make_state(mode=BedJetMode.STANDBY, target_temp_c=40.0))

        assert entity.target_temperature == 38.0

    def test_turning_on_sends_the_mode_then_the_deferred_setpoint(self) -> None:
        device = FakeDevice()
        entity, _coordinator = make_entity(device)
        asyncio.run(entity.async_set_temperature(**{ATTR_TEMPERATURE: 38.0}))

        asyncio.run(entity.async_set_hvac_mode(HVACMode.HEAT))

        assert device.mode_calls == [BedJetMode.HEAT]
        assert device.temp_calls == [38.0]
        assert entity.target_temperature == 38.0

    def test_deferred_setpoint_is_applied_once_not_on_every_later_mode_change(
        self,
    ) -> None:
        device = FakeDevice()
        entity, _coordinator = make_entity(device)
        asyncio.run(entity.async_set_temperature(**{ATTR_TEMPERATURE: 38.0}))

        asyncio.run(entity.async_set_hvac_mode(HVACMode.HEAT))
        asyncio.run(entity.async_set_hvac_mode(HVACMode.COOL))

        assert device.temp_calls == [38.0]

    def test_turning_off_does_not_write_the_deferred_setpoint(self) -> None:
        device = FakeDevice()
        entity, _coordinator = make_entity(device)
        asyncio.run(entity.async_set_temperature(**{ATTR_TEMPERATURE: 38.0}))

        asyncio.run(entity.async_set_hvac_mode(HVACMode.OFF))

        assert device.mode_calls == [BedJetMode.STANDBY]
        assert device.temp_calls == []
        assert entity.target_temperature == 38.0  # still pending, still shown

    def test_failed_mode_change_keeps_the_value_pending(self) -> None:
        device = FakeDevice()
        entity, _coordinator = make_entity(device)
        asyncio.run(entity.async_set_temperature(**{ATTR_TEMPERATURE: 38.0}))
        device.raise_on_command = BedJetCommandError("confirmation timed out")

        with pytest.raises(HomeAssistantError):
            asyncio.run(entity.async_set_hvac_mode(HVACMode.HEAT))

        device.raise_on_command = None
        assert entity.target_temperature == 38.0
        asyncio.run(entity.async_set_hvac_mode(HVACMode.HEAT))
        assert device.temp_calls == [38.0]

    def test_external_mode_change_drops_the_deferred_value(self) -> None:
        device = FakeDevice()
        entity, coordinator = make_entity(device)
        asyncio.run(entity.async_set_temperature(**{ATTR_TEMPERATURE: 38.0}))

        # Somebody else (phone app, the fan entity, a biorhythm program)
        # started the unit: the device's own target is authoritative now.
        coordinator.push(make_state(mode=BedJetMode.COOL, target_temp_c=40.0))

        assert entity.target_temperature == 40.0
        # And it must not resurface the next time this entity changes mode.
        asyncio.run(entity.async_set_hvac_mode(HVACMode.HEAT))
        assert device.temp_calls == []

    def test_memory_preset_keeps_its_own_stored_target(self) -> None:
        device = FakeDevice()
        entity, _coordinator = make_entity(device)
        asyncio.run(entity.async_set_temperature(**{ATTR_TEMPERATURE: 38.0}))

        asyncio.run(entity.async_set_preset_mode("M1"))

        # The preset restores its own target/fan/runtime; overriding it with
        # a setpoint the user typed while the unit was off would fight it.
        assert device.button_calls == [BedJetButton.M1]
        assert device.temp_calls == []
        asyncio.run(entity.async_set_hvac_mode(HVACMode.HEAT))
        assert device.temp_calls == []

    def test_heat_preset_applies_the_deferred_setpoint(self) -> None:
        device = FakeDevice()
        entity, _coordinator = make_entity(device)
        asyncio.run(entity.async_set_temperature(**{ATTR_TEMPERATURE: 38.0}))

        asyncio.run(entity.async_set_preset_mode(PRESET_TURBO))

        assert device.mode_calls == [BedJetMode.TURBO]
        assert device.temp_calls == [38.0]

    def test_fan_speed_is_not_deferred_in_standby(self) -> None:
        # No ground truth says SET_FAN is ignored in standby, so it is still
        # sent and confirmed exactly as before.
        device = FakeDevice()
        entity, _coordinator = make_entity(device)

        asyncio.run(entity.async_set_fan_mode("55%"))

        assert device.fan_calls == [55]
