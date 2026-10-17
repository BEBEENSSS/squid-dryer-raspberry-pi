"""Per-sensor corrections.

`sensor2_test.py --calibrate` measures how far each extra sensor reads from sensor 1 and saves one
temperature offset per sensor id; the dryer service applies them to the middle and top sensors. The DHT22
(config.DHT22_ID) also reads humidity: `--true-humidity` measures its offset against a trusted hygrometer and
saves it next to the temperature one. Both go through this module so the test script and the service can
never disagree about the file or its format.
Sensor 1 is the reference the others are matched to, so it is never offset, whatever the file says.
The service reads the file once at start-up: restart it after recalibrating.
"""

import json
import math
from datetime import datetime

import config

CAL_PATH = config.DB_PATH.parent / "sensor2_calibration.json"  # data/ is not in git


def load_calibrations():
    try:
        cal = json.loads(CAL_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return cal if isinstance(cal, dict) else {}


def _load_value(sensor_id, key):
    try:
        value = float(load_calibrations()[sensor_id][key])
    except (KeyError, TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0  # NaN/inf are not corrections


def load_offset(sensor_id):
    """Saved temperature correction in C for one sensor, 0.0 when there is none or the file is unusable."""
    return _load_value(sensor_id, "offset_c")


def load_humidity_offset(sensor_id):
    """Saved humidity correction in %RH for one sensor, 0.0 when there is none or the file is unusable."""
    return _load_value(sensor_id, "offset_rh")


def zone_offset(sensor_id):
    """The correction the service applies: none for sensor 1, the saved one for any other sensor."""
    return 0.0 if sensor_id == config.SENSOR_ID else load_offset(sensor_id)


def corrected_humidity(value, offset_rh):
    """A humidity reading plus its correction, kept inside 0-100 (a corrected reading cannot be drier than dry)."""
    return min(max(value + offset_rh, config.HUMIDITY_PLAUSIBLE_MIN), config.HUMIDITY_PLAUSIBLE_MAX)


def _save(sensor_id, fields):
    cal = load_calibrations()
    entry = cal.get(sensor_id)
    entry = dict(entry) if isinstance(entry, dict) else {}
    entry.update(fields)  # the other quantity's offset stays: calibrating temperature must not drop humidity
    cal[sensor_id] = entry
    CAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    CAL_PATH.write_text(json.dumps(cal, indent=2) + "\n")


def save_offset(sensor_id, offset, against, samples):
    _save(sensor_id, {
        "offset_c": offset,
        "against": against,
        "samples": samples,
        "at": datetime.now().isoformat(timespec="seconds"),
    })


def save_humidity_offset(sensor_id, offset_rh, against, samples):
    _save(sensor_id, {
        "offset_rh": offset_rh,
        "against_rh": against,
        "samples_rh": samples,
        "at_rh": datetime.now().isoformat(timespec="seconds"),
    })
