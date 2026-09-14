"""Diagnostics support for the BedJet integration."""

from __future__ import annotations

from typing import Any

from bluetooth_data_tools import monotonic_time_coarse

from homeassistant.core import HomeAssistant

from . import BedJetConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BedJetConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    device = coordinator.device
    state = coordinator.data

    # device.last_frame_at and BluetoothServiceInfoBleak.time are both
    # monotonic-clock stamps (confirmed with the library author); comparing
    # against monotonic_time_coarse() - the same "now" function Home
    # Assistant's own bluetooth component uses against .time - keeps both
    # comparisons on a clock that's actually guaranteed comparable, unlike
    # wall-clock time.time().
    now = monotonic_time_coarse()
    last_frame_at = device.last_frame_at

    return {
        "connection": {
            "connected": device.connected,
            "available": device.available,
            "hold": device.hold_connection,
            "last_frame_at": last_frame_at,
            "seconds_since_last_frame": (
                round(now - last_frame_at, 1) if last_frame_at is not None else None
            ),
        },
        "state": None
        if state is None
        else {
            "mode": state.mode.name,
            "target_temp_c": state.target_temp_c,
            "actual_temp_c": state.actual_temp_c,
            "ambient_temp_c": state.ambient_temp_c,
            "fan_step": state.fan_step,
            "fan_percent": state.fan_percent,
            "time_remaining_s": state.time_remaining.total_seconds(),
            "max_runtime_s": state.max_runtime.total_seconds(),
            "min_temp_c": state.min_temp_c,
            "max_temp_c": state.max_temp_c,
            "turbo_time": state.turbo_time,
            "shutdown_reason": state.shutdown_reason,
            "notification": (
                state.notification.name if state.notification is not None else None
            ),
            "update_phase": state.update_phase,
            "leds_enabled": state.leds_enabled,
            "beeps_muted": state.beeps_muted,
            "units_setup": state.units_setup,
            "connection_test_passed": state.connection_test_passed,
            "dual_zone": state.dual_zone,
            "bio_sequence_step": state.bio_sequence_step,
            "is_partial": state.is_partial,
            "raw": state.raw.hex(),
        },
    }
