"""Unit tests for the pure helpers in tui.py.

Importing tui pulls in textual/tomlkit/plotext (installed deps) but starts no
UI. Only the pure, dependency-free helpers are exercised here.
"""
import pytest

import tui


class TestMeasurementSelectConverters:
    @pytest.mark.parametrize(
        "measurements,expected",
        [
            (["temperature", "humidity"], "temperature+humidity"),
            (["humidity"], "humidity"),
            (["temperature"], "temperature"),
            ([], "temperature"),  # default fallthrough
        ],
    )
    def test_to_select_val(self, measurements, expected):
        assert tui.measurements_to_select_val(measurements) == expected

    @pytest.mark.parametrize(
        "val,expected",
        [
            ("temperature+humidity", ["temperature", "humidity"]),
            ("humidity", ["humidity"]),
            ("temperature", ["temperature"]),
            (None, ["temperature"]),  # unknown -> default
        ],
    )
    def test_to_measurements(self, val, expected):
        assert tui.select_val_to_measurements(val) == expected

    @pytest.mark.parametrize(
        "val", ["temperature", "humidity", "temperature+humidity"]
    )
    def test_round_trip(self, val):
        assert tui.measurements_to_select_val(tui.select_val_to_measurements(val)) == val


class TestSafeBtnId:
    def test_spaces_become_underscores(self):
        assert tui._safe_btn_id("living room") == "sf_living_room"

    def test_hyphen_preserved(self):
        assert tui._safe_btn_id("basement-east") == "sf_basement-east"

    def test_special_chars_sanitized(self):
        assert tui._safe_btn_id("a.b/c") == "sf_a_b_c"
