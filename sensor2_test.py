"""Standalone test for the extra temperature sensors (DS18B20), separate from the dryer service.

Reads every 1-Wire thermometer that is not config.SENSOR_ID (sensor 1, the main sensor on GPIO4)
next to sensor 1 and prints them in one table. Sensor 1 is the reference for every comparison
and for calibration. It only reads from /sys, never touches
GPIO or the relays, so it is safe to run while squid-dryer.service is running.

    python sensor2_test.py                   # test every extra sensor, Ctrl+C to stop
    python sensor2_test.py --expect 3        # fail loudly unless 3 sensors in total (main + 2) show up
    python sensor2_test.py --count 10        # 10 readings, then exit
    python sensor2_test.py --id 28-0000001234ab   # test one specific sensor

Calibration: put all sensors side by side, let them settle, then
    python sensor2_test.py --calibrate           # match every extra sensor to the main one (120 s)
    python sensor2_test.py --calibrate --true-temp 47.0   # match them to a trusted thermometer instead
The offsets are saved to data/sensor2_calibration.json, one per sensor id, and applied by this
script from then on (--raw shows the uncalibrated readings). The dryer service does not read it.
"""
import argparse
import json
import time
from datetime import datetime
from statistics import mean

import config

# family codes the w1_therm driver handles: DS18B20 (28), DS1822 (22), DS18S20 (10), DS1825 (3b)
THERM_FAMILIES = ("28", "22", "10", "3b")

CAL_PATH = config.DB_PATH.parent / "sensor2_calibration.json"  # data/ is not in git
CAL_MIN_SAMPLES = 10
CAL_SETTLE_C = 0.15  # the error may move this much between the first and second half of a run
COL_WIDTH = 16       # "31.29 C (+0.04)"


def list_sensors(base):
    return sorted(p for p in base.glob("*-*") if p.name.split("-")[0] in THERM_FAMILIES)


def bus_of(sensor):
    """Name of the 1-Wire bus the sensor hangs on, e.g. w1_bus_master2. The numbers can swap between boots."""
    return sensor.resolve().parent.name


def read_temp(sensor):
    """Return (temp_c, None) for a good read, or (None, reason) for a bad one."""
    try:
        lines = (sensor / "w1_slave").read_text().strip().splitlines()
    except OSError as e:
        return None, f"unreadable ({e.strerror})"

    # line 1 must end with YES (CRC ok)
    if len(lines) < 2 or not lines[0].endswith("YES"):
        return None, "CRC error (check wiring and the 4.7k pull-up)"
    if "t=" not in lines[1]:
        return None, "no temperature in reply"

    try:
        temp = int(lines[1].split("t=")[1]) / 1000.0
    except ValueError:
        return None, "garbled reply"

    if temp == -127.0:
        return None, "-127 C (data line disconnected)"
    if temp == 85.0:
        return None, "85 C (power-on default, not a real reading)"
    if not config.TEMP_PLAUSIBLE_MIN_C <= temp <= config.TEMP_PLAUSIBLE_MAX_C:
        return None, f"{temp:.1f} C is outside the plausible range"

    return temp, None


def no_sensor_help(base, found):
    if not base.exists():
        print(f"{base} does not exist. 1-Wire is not enabled: add dtoverlay=w1-gpio to /boot/firmware/config.txt and reboot.")
    elif not found:
        print("No 1-Wire thermometer on the bus at all. Check wiring, the 4.7k pull-up, and that 1-Wire is enabled.")
    else:
        buses = sorted(p.name for p in base.glob("w1_bus_master*"))
        print("Only the main sensor is showing up, the extra ones are not.")
        if len(buses) < 2:
            print("Only one 1-Wire bus is active (GPIO4, the main sensor). For the extra sensors on their own pin, add")
            print("  dtoverlay=w1-gpio,gpiopin=22")
            print("to /boot/firmware/config.txt and reboot, then wire their data to GPIO22 (physical pin 15).")
        else:
            print(f"A second bus is up ({', '.join(buses)}) but no sensor answers on it.")
            print("Check the wiring: red to 3V3, black to GND, yellow/data to its own GPIO (22 for sensor 2, 23 for sensor 3),")
            print("with a 4.7k pull-up from each data wire to 3V3.")


def load_calibrations():
    try:
        cal = json.loads(CAL_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return cal if isinstance(cal, dict) else {}


def load_offset(sensor_id):
    try:
        return float(load_calibrations()[sensor_id]["offset_c"])
    except (KeyError, TypeError, ValueError):
        return 0.0


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


def calibrate(targets, ref, seconds, interval, true_temp):
    """Measure how far each target reads from the reference and save the corrections. Returns an exit code."""
    against = f"true temperature {true_temp:.2f} C" if true_temp is not None else f"sensor 1 (main) {ref.name}"
    print(f"Calibrating {', '.join(t.name for t in targets)} against the {against} for {seconds} s.")
    print("Keep the sensors touching and still. Ctrl+C aborts without saving.\n")

    errors = {t.name: [] for t in targets}  # reading minus reference, one per paired good read
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            ref_temp = true_temp if true_temp is not None else read_temp(ref)[0]
            cells = [f"reference {ref_temp:.2f} C" if ref_temp is not None else "reference BAD"]
            for t in targets:
                temp, _ = read_temp(t)
                if temp is None or ref_temp is None:
                    cells.append(f"{t.name} bad")
                    continue
                errors[t.name].append(temp - ref_temp)
                cells.append(f"{t.name} {temp:.2f} C ({errors[t.name][-1]:+.2f})")
            print(f"{datetime.now():%H:%M:%S}  " + "  |  ".join(cells), flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nAborted, nothing saved.")
        return 1

    print()
    code = 0
    for t in targets:
        errs = errors[t.name]
        if len(errs) < CAL_MIN_SAMPLES:
            print(f"{t.name}: only {len(errs)} good paired reads, need {CAL_MIN_SAMPLES}. Nothing saved.")
            code = 1
            continue

        # the first half is allowed to still be settling; the offset comes from the second half only
        half = len(errs) // 2
        first, second = errs[:half], errs[half:]
        drift = mean(second) - mean(first)
        if abs(drift) > CAL_SETTLE_C:
            print(f"{t.name}: not settled, the error moved {drift:+.2f} C between the first and second half. "
                  "Nothing saved. Leave the sensors together a few more minutes and run it again.")
            code = 1
            continue

        offset = round(-mean(second), 2)
        save_offset(t.name, offset, against, len(second))
        print(f"{t.name}: error {mean(second):+.2f} C (spread {max(second) - min(second):.2f} C, "
              f"{len(second)} reads) -> saved offset {offset:+.2f} C")
    if code == 0:
        print(f"\nSaved to {CAL_PATH}. Check it with: python sensor2_test.py --count 10")
    return code


def main():
    ap = argparse.ArgumentParser(description="Read-only test and calibration for the extra temperature sensors.")
    ap.add_argument("--id", help="sensor id to test, e.g. 28-0000001234ab (default: every sensor except the main one)")
    ap.add_argument("--interval", type=float, default=config.SENSOR_INTERVAL, help="seconds between readings")
    ap.add_argument("--count", type=int, default=0, help="stop after this many readings (default: until Ctrl+C)")
    ap.add_argument("--expect", type=int, metavar="N",
                    help="total sensors that should be on the bus, main included; fewer fails the test")
    ap.add_argument("--calibrate", nargs="?", type=int, const=120, metavar="SECONDS",
                    help="measure the offsets against sensor 1 (main) and save them (default 120 s)")
    ap.add_argument("--true-temp", type=float, metavar="C",
                    help="with --calibrate: the real temperature from a trusted thermometer, instead of sensor 1")
    ap.add_argument("--raw", action="store_true", help="ignore the saved calibration")
    args = ap.parse_args()
    if args.true_temp is not None and args.calibrate is None:
        ap.error("--true-temp only makes sense with --calibrate")

    base = config.W1_BASE
    found = list_sensors(base) if base.exists() else []
    names = [s.name for s in found]

    buses = {}
    for s in found:
        buses.setdefault(bus_of(s), []).append(f"{s.name} (sensor 1, main)" if s.name == config.SENSOR_ID else s.name)
    print(f"{len(found)} 1-Wire sensor(s) found:")
    for bus, members in sorted(buses.items()):
        print(f"  {bus}: {', '.join(members)}")
    if not found:
        print("  none")

    missing = args.expect is not None and len(found) < args.expect
    if missing:
        print(f"Expected {args.expect} sensors, found {len(found)}: {args.expect - len(found)} missing. "
              "Check its wiring (3V3, GND, data, 4.7k pull-up) and which pin its data wire is on.")

    if args.id:
        if args.id not in names:
            print(f"{args.id} is not on the bus.")
            return 1
        targets = [base / args.id]
    else:
        targets = [s for s in found if s.name != config.SENSOR_ID]
    if not targets:
        no_sensor_help(base, found)
        return 1

    ref = base / config.SENSOR_ID if config.SENSOR_ID in names and config.SENSOR_ID not in [t.name for t in targets] else None

    if args.calibrate is not None:
        if ref is None and args.true_temp is None:
            print("Sensor 1 (main) is not available as a reference. Pass --true-temp with a trusted reading instead.")
            return 1
        return calibrate(targets, ref, args.calibrate, args.interval, args.true_temp)

    offsets = {t.name: 0.0 if args.raw else load_offset(t.name) for t in targets}
    for name, offset in offsets.items():
        if offset:
            print(f"Calibration applied to {name}: {offset:+.2f} C (--raw shows the uncalibrated reading)")
    print("\nCells show the reading and, in brackets, the difference from sensor 1. Ctrl+C to stop.\n")

    cols = ([("sensor 1 (main)", ref)] if ref else []) + [(t.name, t) for t in targets]
    header = f"{'time':<8}  " + "  ".join(f"{label:<{COL_WIDTH}}" for label, _ in cols)

    good = {t.name: [] for t in targets}
    bad = {t.name: 0 for t in targets}
    n = 0
    try:
        while True:
            if n % 20 == 0:
                print(header)
            n += 1
            cells = []
            problems = []
            ref_temp = None
            if ref:
                ref_temp, err = read_temp(ref)
                cells.append(f"{ref_temp:.2f} C" if ref_temp is not None else "BAD")
                if err:
                    problems.append(f"sensor 1: {err}")
            for t in targets:
                temp, err = read_temp(t)
                if temp is None:
                    bad[t.name] += 1
                    cells.append("BAD")
                    problems.append(f"{t.name}: {err}")
                    continue
                temp += offsets[t.name]
                good[t.name].append(temp)
                diff = f" ({temp - ref_temp:+.2f})" if ref_temp is not None else ""
                cells.append(f"{temp:.2f} C{diff}")
            print(f"{datetime.now():%H:%M:%S}  " + "  ".join(f"{c:<{COL_WIDTH}}" for c in cells), flush=True)
            for p in problems:
                print(f"          ! {p}")
            if args.count and n >= args.count:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()

    print("\n--- summary ---")
    ok = not missing
    for t in targets:
        g, b = good[t.name], bad[t.name]
        stats = f", min {min(g):.2f} avg {sum(g) / len(g):.2f} max {max(g):.2f} C" if g else ""
        print(f"{t.name}: {len(g)} good, {b} bad{stats}")
        ok = ok and bool(g) and b == 0
    print("PASS" if ok else "FAIL: a sensor is missing, had bad reads, or had no reads")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
