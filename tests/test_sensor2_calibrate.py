"""sensor2_test.py --calibrate with the DHT22 as the top sensor: temperature is matched to sensor 1, humidity to a
trusted hygrometer, and both end up in the calibration file. No hardware and no waiting: sensors and clock are fakes."""
from pathlib import Path

import pytest

import calibration
import config
import dht22
import sensor2_test

REF = Path("/sys/bus/w1/devices") / config.SENSOR_ID


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Sensor 1 reads 30.00 C. The DHT22 is whatever rig.dht(n) says for its n-th read."""
    monkeypatch.setattr(calibration, "CAL_PATH", tmp_path / "cal.json")
    monkeypatch.setattr(sensor2_test, "time", FakeClock())
    monkeypatch.setattr(sensor2_test, "read_temp", lambda sensor: (30.0, None))

    class Rig:
        reads = 0
        dht = staticmethod(lambda n: dht22.Reading(30.4, 55.0, None))

    def fake_read(pin=None, **kwargs):
        Rig.reads += 1
        return Rig.dht(Rig.reads)

    monkeypatch.setattr(sensor2_test.dht22, "read", fake_read)
    return Rig


def run(true_temp=None, true_humidity=None, seconds=60):
    return sensor2_test.calibrate([sensor2_test.Dht22Sensor(config.DHT22_PIN)], REF, seconds, 2.0,
                                  true_temp, true_humidity)


def test_the_top_sensor_is_the_dht22_on_gpio22():
    sensor = sensor2_test.Dht22Sensor(config.DHT22_PIN)
    assert (config.DHT22_PIN, sensor.name) == (22, "dht22-gpio22")
    assert sensor2_test.has_humidity(sensor)
    assert not sensor2_test.has_humidity(REF)


def test_temperature_is_matched_to_sensor_1_and_humidity_to_the_hygrometer(rig, capsys):
    assert run(true_humidity=50.0) == 0
    assert calibration.zone_offset(config.DHT22_ID) == -0.4               # reads 30.4 against sensor 1's 30.0
    assert calibration.load_humidity_offset(config.DHT22_ID) == -5.0      # reads 55.0 against a true 50.0
    entry = calibration.load_calibrations()[config.DHT22_ID]
    assert entry["against"] == f"sensor 1 (main) {config.SENSOR_ID}"
    assert entry["against_rh"] == "true humidity 50.00 %RH"
    assert "saved offset -5.00 %RH" in capsys.readouterr().out


def test_the_saved_offsets_correct_a_reading(rig):
    run(true_humidity=50.0)
    reading = dht22.Reading(30.4, 55.0, None)
    assert round(reading.temp_c + calibration.zone_offset(config.DHT22_ID), 2) == 30.0
    assert calibration.corrected_humidity(reading.humidity, calibration.load_humidity_offset(config.DHT22_ID)) == 50.0


def test_without_a_hygrometer_only_temperature_is_calibrated_and_humidity_is_kept(rig, capsys):
    calibration.save_humidity_offset(config.DHT22_ID, 2.5, "true humidity 60.00 %RH", 30)   # from an earlier run
    assert run() == 0
    assert calibration.zone_offset(config.DHT22_ID) == -0.4
    assert calibration.load_humidity_offset(config.DHT22_ID) == 2.5
    assert "humidity not calibrated" in capsys.readouterr().out


def test_a_trusted_thermometer_replaces_sensor_1_as_the_temperature_reference(rig):
    assert run(true_temp=30.2) == 0
    assert calibration.zone_offset(config.DHT22_ID) == -0.2
    assert calibration.load_calibrations()[config.DHT22_ID]["against"] == "true temperature 30.20 C"


def test_a_humidity_that_is_still_drifting_is_not_saved_but_temperature_is(rig, capsys):
    rig.dht = staticmethod(lambda n: dht22.Reading(30.4, 40.0 + n * 0.5, None))
    assert run(true_humidity=50.0) == 1
    assert calibration.zone_offset(config.DHT22_ID) == -0.4
    assert calibration.load_humidity_offset(config.DHT22_ID) == 0.0
    assert "humidity not settled" in capsys.readouterr().out


def test_too_many_failed_reads_save_nothing(rig, capsys):
    rig.dht = staticmethod(lambda n: dht22.Reading(None, None, "checksum mismatch") if n % 4 else
                           dht22.Reading(30.4, 55.0, None))
    assert run(true_humidity=50.0) == 1                       # 30 reads, 7 good: below the 10 needed
    assert calibration.load_calibrations() == {}
    assert "only 7 good paired temperature reads" in capsys.readouterr().out


def test_a_dht22_that_never_answers_saves_nothing(rig):
    rig.dht = staticmethod(lambda n: dht22.Reading(None, None, "no response"))
    assert run(true_humidity=50.0) == 1
    assert calibration.load_calibrations() == {}
