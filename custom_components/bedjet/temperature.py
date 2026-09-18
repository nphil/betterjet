"""Whole-degree publishing for measured temperatures, with hysteresis.

Shared by climate.py (``current_temperature``) and sensor.py (the ambient and
outlet temperature sensors) - every MEASURED temperature this integration
reports. Never applied to a setpoint (``target_temp_c``): a value the user
dialed in must be reported back exactly as set.

Every BedJet temperature is decoded in 0.5C steps (``actual_temp_step / 2``
in pybedjet's codec; half-degree readings are real and expected, e.g. an
ambient step of 51 decodes to 25.5C). The unit also dithers by exactly one
such step while the true temperature sits on a boundary: measured live,
``climate.master_bedroom_bedjet``'s ``current_temperature`` alternated
76/77F (24.5/25.0C) every ~2s for hours - 1,132 recorder rows in 7.5h with
nothing else about the entity changing - and the ambient temperature sensor
was writing 8.2 rows/min the same way.

Plain rounding cannot fix this on its own. The dither (0.5C) is exactly the
rounding grid's own quantum, so a reading parked on a whole-degree boundary
still flips the rounded result every time the device's own noise pushes it
to the other side: round(24.5) == 24 but round(25.0) == 25 (Python rounds
ties to even), so alternating those two raw values through a stateless
round() reproduces the exact same alternation the recorder was choking on.
Quantizing to a coarser grid only relocates the boundary; it does not widen
it. Hysteresis does: `TemperatureQuantizer` only moves the published value
once a reading has drifted a full deadband away from what is already
published, which a single 0.5C step never does.
"""

from __future__ import annotations

#: Whole-degree publish grid - what the user asked to see, distinct from the
#: device's native 0.5C decoding grid.
QUANTIZE_STEP_C = 1.0

#: How far a raw reading must drift from the last PUBLISHED value before a
#: new value is published. Must clear one device step (0.5C) so the unit's
#: own single-step boundary dither (see module docstring) never crosses it,
#: while staying under a full degree so a genuine move is still published
#: promptly rather than lagging by almost two grid cells.
QUANTIZE_DEADBAND_C = 0.8


class TemperatureQuantizer:
    """Turn a stream of raw Celsius readings into whole-degree publishes.

    Each instance holds its own last-published value, so every entity using
    one needs its own `TemperatureQuantizer` - sharing one across entities
    (e.g. the climate entity and the ambient sensor) would let one entity's
    readings suppress another's unrelated changes.
    """

    def __init__(
        self, *, step: float = QUANTIZE_STEP_C, deadband: float = QUANTIZE_DEADBAND_C
    ) -> None:
        self._step = step
        self._deadband = deadband
        self._published: float | None = None

    def push(self, raw_c: float | None) -> float | None:
        """Feed one raw Celsius reading, return the value to publish.

        No previously published value: seeds it with ``round(raw_c)``,
        unconditionally. Otherwise republishes the last published value
        unchanged unless `raw_c` has drifted a full deadband away from it,
        in which case it re-seeds the same way.

        `raw_c` is compared against the last PUBLISHED (already quantized)
        value, never against the last raw one: anchoring on raw readings
        would let a slow drift made of many sub-deadband steps accumulate
        without limit, since no single step would ever clear the deadband on
        its own. Anchoring on the published value bounds the worst-case gap
        between what is published and reality to one deadband's width.

        `None` (unknown/unavailable) passes straight through and clears the
        published value, so the next real reading is seeded fresh - a value
        from before the gap is not a meaningful baseline to resume from.
        """
        if raw_c is None:
            self._published = None
            return None
        if self._published is None or abs(raw_c - self._published) >= self._deadband:
            self._published = round(raw_c / self._step) * self._step
        return self._published
