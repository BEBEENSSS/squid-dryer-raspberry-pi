"""Per-sensor temperature corrections.

`sensor2_test.py --calibrate` measures how far each extra sensor reads from sensor 1 and saves one
offset per sensor id; the dryer service applies them to the middle and top sensors. Both go through
this module so the test script and the service can never disagree about the file or its format.
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


def load_offset(sensor_id):
    """Saved correction in C for one sensor, 0.0 when there is none or the file is unusable."""
    try:
        offset = float(load_calibrations()[sensor_id]["offset_c"])
    except (KeyError, TypeError, ValueError):
        return 0.0
    return offset if math.isfinite(offset) else 0.0  # NaN/inf are not corrections


def zone_offset(sensor_id):
    """The correction the service applies: none for sensor 1, the saved one for any other sensor."""
    return 0.0 if sensor_id == config.SENSOR_ID else load_offset(sensor_id)


def save_offset(sensor_id, offset, against, samples):
    cal = load_calibrations()
    cal[sensor_id] = {
        "offset_c": offset,
        "against": against,
        "samples": samples,
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    CAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    CAL_PATH.write_text(json.dumps(cal, indent=2) + "\n")
