"""Unit tests for the pure/mockable surface of collector.py.

No hardware or live InfluxDB required: sysfs reads use temp files, sensor
discovery monkeypatches W1_BASE, and Point objects are asserted via
line-protocol output (no network).
"""
import time
from pathlib import Path

import pytest

import collector


class TestDetectSensorType:
    @pytest.mark.parametrize(
        "dirname,expected",
        [
            ("28-0b2281f0d981", "DS18B20"),
            ("10-abcdef", "DS18S20"),
            ("22-abcdef", "DS1822"),
            ("3b-abcdef", "DS1825"),
            ("42-abcdef", "DS28EA00"),
            ("99-abcdef", "1W-99"),  # unknown family -> synthesized name
            ("9a-abcdef", "1W-9A"),  # unknown family, uppercased
        ],
    )
    def test_families(self, dirname, expected):
        assert collector.detect_sensor_type(Path(dirname)) == expected


class TestRead1WSensor:
    @staticmethod
    def _write_slave(tmp_path, content):
        (tmp_path / "w1_slave").write_text(content)
        return tmp_path

    def test_valid_reading(self, tmp_path):
        d = self._write_slave(
            tmp_path,
            "a1 01 4b 46 7f ff 0c 10 fc : crc=fc YES\n"
            "a1 01 4b 46 7f ff 0c 10 fc t=25062\n",
        )
        # 25062 millidegrees -> 25.06 C; F = 25.06*9/5+32 = 77.108 -> 77.11
        assert collector.read_1w_sensor(d) == (25.06, 77.11)

    def test_negative_temp(self, tmp_path):
        d = self._write_slave(tmp_path, "xx YES\nxx t=-5000\n")
        assert collector.read_1w_sensor(d) == (-5.0, 23.0)

    def test_crc_not_yes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda *_: None)  # keep retry loop fast
        d = self._write_slave(tmp_path, "a1 : crc=00 NO\na1 t=25062\n")
        assert collector.read_1w_sensor(d) == (None, None)

    def test_missing_t_marker(self, tmp_path, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        d = self._write_slave(tmp_path, "a1 YES\na1 no-temperature-here\n")
        assert collector.read_1w_sensor(d) == (None, None)

    def test_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        assert collector.read_1w_sensor(tmp_path) == (None, None)


class TestReadSensorHumidity:
    def test_valid(self, tmp_path):
        (tmp_path / "humidity").write_text("48.27\n")
        assert collector.read_sensor_humidity(tmp_path) == 48.3

    def test_missing(self, tmp_path):
        assert collector.read_sensor_humidity(tmp_path) is None

    def test_garbage(self, tmp_path):
        (tmp_path / "humidity").write_text("not-a-number")
        assert collector.read_sensor_humidity(tmp_path) is None


class TestFindTempSensors:
    def test_filters_and_sorts(self, tmp_path, monkeypatch):
        for name in ["28-a", "10-b", "w1_bus_master1", "zzz"]:
            (tmp_path / name).mkdir()
        monkeypatch.setattr(collector, "W1_BASE", tmp_path)
        assert [p.name for p in collector.find_temp_sensors()] == ["10-b", "28-a"]


class TestCollect:
    def test_temperature_tag_schema(self, monkeypatch):
        monkeypatch.setattr(collector, "find_temp_sensors", lambda: [Path("28-abc")])
        monkeypatch.setattr(collector, "read_1w_sensor", lambda d: (25.06, 77.11))
        config = {
            "collector": {"hostname": "bu-twr-02"},
            "sensors": {"28-abc": {"name": "living_room"}},
        }
        points = collector.collect(config)
        assert len(points) == 1
        lp = points[0].to_line_protocol()
        assert lp.startswith("temperature,")
        for tag in (
            "host=bu-twr-02",
            "sensor_id=28-abc",
            "sensor_type=DS18B20",
            "source=living_room",
        ):
            assert tag in lp
        assert "celsius=25.06" in lp
        assert "fahrenheit=77.11" in lp

    def test_source_and_host_defaults(self, monkeypatch):
        monkeypatch.setattr(collector, "find_temp_sensors", lambda: [Path("28-xyz")])
        monkeypatch.setattr(collector, "read_1w_sensor", lambda d: (20.0, 68.0))
        points = collector.collect({"collector": {}, "sensors": {}})
        lp = points[0].to_line_protocol()
        assert "source=28-xyz" in lp  # falls back to sensor_id
        assert "host=pi" in lp  # default hostname

    def test_bad_reading_skipped(self, monkeypatch):
        monkeypatch.setattr(collector, "find_temp_sensors", lambda: [Path("28-abc")])
        monkeypatch.setattr(collector, "read_1w_sensor", lambda d: (None, None))
        assert collector.collect({"collector": {}, "sensors": {}}) == []

    def test_humidity_measurement(self, monkeypatch):
        monkeypatch.setattr(collector, "find_temp_sensors", lambda: [Path("28-abc")])
        monkeypatch.setattr(collector, "read_sensor_humidity", lambda d: 48.3)
        config = {
            "collector": {"hostname": "h"},
            "sensors": {"28-abc": {"name": "attic", "measurements": ["humidity"]}},
        }
        points = collector.collect(config)
        assert len(points) == 1
        lp = points[0].to_line_protocol()
        assert lp.startswith("humidity,")
        assert "percent_rh=48.3" in lp


class TestWritePoints:
    def test_forwards_bucket_org_record(self):
        calls = []

        class FakeApi:
            def write(self, **kw):
                calls.append(kw)

        collector.write_points(FakeApi(), {"bucket": "b", "org": "o"}, ["p1"], "local")
        assert calls == [{"bucket": "b", "org": "o", "record": ["p1"]}]

    def test_swallows_write_errors(self):
        class FakeApi:
            def write(self, **kw):
                raise RuntimeError("influx down")

        # Must not raise: a write failure should be logged, not propagated.
        collector.write_points(FakeApi(), {"bucket": "b", "org": "o"}, ["p"], "local")
