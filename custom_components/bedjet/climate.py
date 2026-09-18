"""BedJet climate entity."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_WHOLE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BedJetConfigEntry
from .entity import BedJetEntity
from .pybedjet import BedJetButton, BedJetMode
from .temperature import TemperatureQuantizer

_LOGGER = logging.getLogger(__name__)

# Static per Home Assistant convention, not read from the per-mode min/max the
# device reports in status frames (upstream ha-bedjet issue #61: those churn
# on every mode change and are not meant to be entity-wide limits).
MIN_TEMP_C = 19.0
MAX_TEMP_C = 43.0

PRESET_NONE = "none"
PRESET_TURBO = "Turbo"
PRESET_EXTENDED_HEAT = "Extended Heat"

MODE_TO_HVAC_MODE = {
    BedJetMode.STANDBY: HVACMode.OFF,
    BedJetMode.WAIT: HVACMode.OFF,
    BedJetMode.COOL: HVACMode.COOL,
    BedJetMode.DRY: HVACMode.DRY,
    BedJetMode.HEAT: HVACMode.HEAT,
    BedJetMode.TURBO: HVACMode.HEAT,
    BedJetMode.EXTENDED_HEAT: HVACMode.HEAT,
}
HVAC_MODE_TO_MODE = {
    HVACMode.OFF: BedJetMode.STANDBY,
    HVACMode.COOL: BedJetMode.COOL,
    HVACMode.DRY: BedJetMode.DRY,
    HVACMode.HEAT: BedJetMode.HEAT,
}
MODE_TO_PRESET = {
    BedJetMode.TURBO: PRESET_TURBO,
    BedJetMode.EXTENDED_HEAT: PRESET_EXTENDED_HEAT,
}
# "None" only means anything as a preset revert while a HEAT-family preset
# (Turbo/Extended Heat) is active; setting it from Cool/Dry/off must not
# force a mode switch.
PRESET_TO_MODE = {
    PRESET_TURBO: BedJetMode.TURBO,
    PRESET_EXTENDED_HEAT: BedJetMode.EXTENDED_HEAT,
}
PRESET_REVERT_MODES = (BedJetMode.TURBO, BedJetMode.EXTENDED_HEAT)
MEMORY_PRESET_KEYS = ("m1_name", "m2_name", "m3_name")
MEMORY_PRESET_BUTTONS = (BedJetButton.M1, BedJetButton.M2, BedJetButton.M3)
MEMORY_PRESET_DEFAULT_LABELS = ("M1", "M2", "M3")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BedJetConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the climate platform for BedJet."""
    coordinator = entry.runtime_data
    async_add_entities([BedJetClimateEntity(coordinator, entry.title)])


class BedJetClimateEntity(BedJetEntity, ClimateEntity):
    """Representation of a BedJet device as a climate entity."""

    _attr_fan_modes = [f"{speed}%" for speed in range(5, 101, 5)]
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.DRY]
    # ClimateEntity declares these without defaults; a listener fan-out before
    # the first decoded frame (coordinator.data is None) otherwise makes HA's
    # state calculation raise AttributeError and log an error per entity.
    _attr_fan_mode: str | None = None
    _attr_hvac_mode: HVACMode | None = None
    _attr_preset_mode: str | None = None
    _attr_max_temp = MAX_TEMP_C
    _attr_min_temp = MIN_TEMP_C
    _attr_name = None
    _attr_preset_modes = [
        PRESET_NONE,
        PRESET_TURBO,
        PRESET_EXTENDED_HEAT,
        *MEMORY_PRESET_DEFAULT_LABELS,
    ]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    # Whole-degree display: native unit is Celsius, so ClimateEntity's
    # unit-based default (PRECISION_TENTHS for Celsius) would otherwise show
    # 0.5C steps the user does not want - see temperature.py for how the
    # published value itself is also quantized, not just its display.
    _attr_precision = PRECISION_WHOLE

    def __init__(self, coordinator, name: str) -> None:
        """Initialize a BedJet climate entity."""
        self._attr_unique_id = coordinator.device.address
        self._memory_preset_buttons: dict[str, BedJetButton] = dict(
            zip(MEMORY_PRESET_DEFAULT_LABELS, MEMORY_PRESET_BUTTONS, strict=True)
        )
        # Setpoint requested while the unit was in standby, not yet written
        # to the device - see `async_set_temperature`.
        self._deferred_target_c: float | None = None
        # One quantizer per entity instance - see temperature.py. Must be
        # created before the base class's __init__ calls
        # _async_update_attrs() below.
        self._quantizer = TemperatureQuantizer()
        super().__init__(coordinator, name)

    @property
    def _in_standby(self) -> bool:
        """True when the last frame showed the unit parked in STANDBY.

        WAIT (a biorhythm program between steps) is deliberately excluded:
        the only mode a setpoint write is *known* to be swallowed in is
        STANDBY, verified live, and guessing about WAIT would trade a 5s
        stall for a silently dropped command.
        """
        state = self.coordinator.data
        return state is not None and state.mode is BedJetMode.STANDBY

    @callback
    def _async_update_attrs(self) -> None:
        """Handle updating _attr values."""
        if (state := self.coordinator.data) is None:
            return
        # Whole-degree publish with hysteresis: the device dithers by its
        # own 0.5C step while parked on a boundary, and plain rounding
        # cannot absorb that because the dither *is* the rounding grid's
        # quantum (round(24.5) == 24 but round(25.0) == 25) - see
        # temperature.py. target_temp_c is a setpoint the user chose, so it
        # is reported exactly as the device confirms it, never quantized.
        self._attr_current_temperature = self._quantizer.push(state.actual_temp_c)
        self._attr_target_temperature = state.target_temp_c
        self._attr_fan_mode = f"{state.fan_percent}%"
        self._attr_hvac_mode = MODE_TO_HVAC_MODE.get(state.mode, HVACMode.OFF)
        self._attr_preset_mode = MODE_TO_PRESET.get(state.mode, PRESET_NONE)

        if self._deferred_target_c is not None:
            if state.mode is BedJetMode.STANDBY:
                # Still in standby, so the device is still streaming its old
                # target: keep showing what the user asked for instead of
                # snapping the thermostat card back.
                self._attr_target_temperature = self._deferred_target_c
            else:
                # The unit left standby without going through this entity
                # (phone app, the fan entity, a memory preset, a biorhythm
                # program): the device's own target is authoritative now, so
                # drop the deferred value rather than fighting it.
                self._deferred_target_c = None

        device = self._device
        labels = [
            getattr(device, key, None) or default
            for key, default in zip(
                MEMORY_PRESET_KEYS, MEMORY_PRESET_DEFAULT_LABELS, strict=True
            )
        ]
        self._memory_preset_buttons = dict(zip(labels, MEMORY_PRESET_BUTTONS, strict=True))
        self._attr_preset_modes = [
            PRESET_NONE,
            PRESET_TURBO,
            PRESET_EXTENDED_HEAT,
            *labels,
        ]

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set new target fan mode.

        Not deferred in standby, unlike the setpoint: nothing in the ESPHome
        protocol map, this fork's codec notes, or the live captures says the
        unit ignores SET_FAN while in standby, so it is sent as always and
        confirmed against the next frame.
        """
        await self._async_send_command(
            self._device.set_fan_percent, int(fan_mode.removesuffix("%"))
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode.

        Also reached by `async_turn_on`, whose ClimateEntity implementation
        picks the first supported mode and calls this - so turning the unit
        on applies a deferred setpoint too.
        """
        if (mode := HVAC_MODE_TO_MODE.get(hvac_mode)) is None:
            raise HomeAssistantError(f"Unsupported HVAC mode: {hvac_mode}")
        await self._async_send_mode(mode)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode."""
        if preset_mode == PRESET_NONE:
            state = self.coordinator.data
            if state is not None and state.mode in PRESET_REVERT_MODES:
                await self._async_send_mode(BedJetMode.HEAT)
            return
        if (mode := PRESET_TO_MODE.get(preset_mode)) is not None:
            await self._async_send_mode(mode)
            return
        if (button := self._memory_preset_buttons.get(preset_mode)) is not None:
            await self._async_send_command(self._device.press_button, button)
            # A memory preset restores its own stored target, fan speed and
            # runtime; a setpoint deferred while off must not override the
            # preset the user just asked for.
            self._deferred_target_c = None
            return
        raise HomeAssistantError(f"{preset_mode} is not a valid preset for {self.name}")

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature, deferring the write while in standby.

        A BedJet in standby silently ignores SET_TEMP (verified live: the
        confirming frame never arrives and the command burns the full
        COMMAND_TIMEOUT_S before failing). HomeKit writes TargetTemperature
        as its own service call regardless of mode, so from the Home app
        that was a routine 5s spinner ending in an error and a snap-back.
        Instead the value is held locally, shown immediately, and written
        the moment this entity takes the unit out of standby.
        """
        if ATTR_HVAC_MODE in kwargs:
            _LOGGER.warning(
                "Changing HVAC mode while setting temperature for %s is not "
                "supported. Please call `climate.set_hvac_mode` first",
                self.entity_id,
            )
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        if self._in_standby:
            self._deferred_target_c = temperature
            self._attr_target_temperature = temperature
            self.async_write_ha_state()
            _LOGGER.debug(
                "%s: deferring target %.1fC until the unit leaves standby",
                self.entity_id,
                temperature,
            )
            return
        await self._async_send_command(self._device.set_temperature_c, temperature)

    async def _async_send_mode(self, mode: BedJetMode) -> None:
        """Send a mode change, then any setpoint deferred while in standby.

        Order matters twice over. On the wire: the setpoint write is only
        accepted once the unit is out of standby, and `set_mode` does not
        return until a frame confirms the new mode, so by the time the second
        command goes out the device is genuinely ready for it. In this
        process: the deferred value has to be read *before* awaiting, because
        pybedjet resolves a command's future and fans the confirming frame
        out to `_async_update_attrs` synchronously inside its notify handler
        - and that frame, no longer showing STANDBY, clears
        `_deferred_target_c` before this coroutine is resumed. Reading it
        afterwards would silently drop every deferred setpoint.

        Left untouched if `set_mode` raises: the unit never left standby, so
        the value stays pending and displayed.
        """
        temperature = self._deferred_target_c
        await self._async_send_command(self._device.set_mode, mode)
        if mode is BedJetMode.STANDBY or temperature is None:
            return
        self._deferred_target_c = None
        await self._async_send_command(self._device.set_temperature_c, temperature)
