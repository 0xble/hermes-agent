"""``maintenance.deletion_window`` gates heavy background deletes to a local-time window."""
from datetime import datetime

from hermes_cli.maintenance_window import window_contains


def _at(hour, minute=0):
    return datetime(2026, 10, 12, hour, minute)


def test_window_bounds_are_start_inclusive_end_exclusive():
    assert not window_contains("02:00-05:00", _at(1, 59))
    assert window_contains("02:00-05:00", _at(2, 0))
    assert window_contains("02:00-05:00", _at(4, 59))
    assert not window_contains("02:00-05:00", _at(5, 0))


def test_window_may_wrap_midnight_and_empty_means_any_time():
    assert window_contains("23:00-01:00", _at(23, 30)) and window_contains("23:00-01:00", _at(0, 30))
    assert not window_contains("23:00-01:00", _at(12, 0))
    assert window_contains("", _at(12, 0)) and window_contains(None, _at(12, 0))


def test_invalid_window_falls_back_to_the_night_default():
    assert not window_contains("nonsense", _at(14, 0))
    assert window_contains("nonsense", _at(3, 0))
