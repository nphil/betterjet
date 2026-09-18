"""Tests for TemperatureQuantizer: whole-degree publishing with hysteresis.

Covers the exact regression this class exists to fix - a BedJet dithering by
its own 0.5C step while parked on a boundary (measured live:
climate.master_bedroom_bedjet's current_temperature alternated 76/77F for
hours, 1132 recorder rows in 7.5h) - plus the deadband boundary, the None
reset, and per-instance state isolation. See climate.py and sensor.py for how
this is wired into the entities that use it.
"""

from __future__ import annotations

from custom_components.bedjet.temperature import TemperatureQuantizer


def test_first_reading_publishes_a_whole_number() -> None:
    quantizer = TemperatureQuantizer()
    assert quantizer.push(24.7) == 25


def test_first_reading_of_an_already_whole_value_is_unchanged() -> None:
    quantizer = TemperatureQuantizer()
    assert quantizer.push(20.0) == 20.0


def test_first_reading_ties_round_half_to_even() -> None:
    # The exact arithmetic that makes plain rounding insufficient: a value
    # sitting on a whole-degree boundary and the very next 0.5C step land on
    # opposite sides of it purely from Python's round-half-to-even tie-break,
    # not from any real temperature change.
    assert TemperatureQuantizer().push(24.5) == 24
    assert TemperatureQuantizer().push(25.5) == 26


def test_reading_within_the_deadband_republishes_the_same_value() -> None:
    quantizer = TemperatureQuantizer()
    quantizer.push(25.0)
    assert quantizer.push(25.4) == 25.0  # 0.4C away, under the 0.8C deadband


def test_reading_at_the_deadband_boundary_does_publish() -> None:
    # abs(raw - published) uses a strict "<" for "unchanged" (see the
    # contract in temperature.py), so a move of exactly the deadband counts
    # as real, not suppressed.
    quantizer = TemperatureQuantizer()
    quantizer.push(25.0)
    assert quantizer.push(25.8) == 26.0


def test_alternating_dither_settles_and_stops_changing() -> None:
    """Feed the exact one-step 0.5C dither measured live around a boundary.

    round(24.5) == 24 seeds the first reading; the next reading (25.0) is a
    full 1C away from that seed - past the 0.8C deadband - so it takes one
    settling step to lock on. Every reading after that must be identical,
    unlike the raw signal, which keeps alternating forever: this is what
    turns unbounded recorder churn into at most one extra transition, ever.
    """
    quantizer = TemperatureQuantizer()
    published = [quantizer.push(raw) for raw in (24.5, 25.0, 24.5, 25.0)]
    assert published == [24, 25, 25, 25]


def test_real_move_past_the_deadband_publishes_the_new_value() -> None:
    quantizer = TemperatureQuantizer()
    quantizer.push(25.0)
    assert quantizer.push(26.0) == 26.0


def test_none_reading_publishes_none() -> None:
    quantizer = TemperatureQuantizer()
    quantizer.push(25.0)
    assert quantizer.push(None) is None


def test_reading_after_none_is_treated_as_a_first_reading() -> None:
    # If the stale published value (20) survived the None gap, 20.6 would
    # fall inside the 0.8C deadband and get suppressed back to 20 instead of
    # publishing its own round(20.6) == 21.
    quantizer = TemperatureQuantizer()
    quantizer.push(20.0)
    assert quantizer.push(None) is None
    assert quantizer.push(20.6) == 21


def test_instances_do_not_share_published_state() -> None:
    a = TemperatureQuantizer()
    b = TemperatureQuantizer()
    a.push(20.0)
    assert b.push(20.6) == 21  # b's first reading, unaffected by a's history
