"""Standalone test for the extra temperature sensors, separate from the dryer service.

The extra sensors are every 1-Wire thermometer that is not config.SENSOR_ID (sensor 1, the main sensor on
GPIO4) and the DHT22 on config.DHT22_PIN (GPIO22), the top sensor, which also reads humidity. They are read next
to sensor 1 and printed in one table. Sensor 1 is the reference for every comparison and for calibration.
It only reads /sys and the DHT22's pin, never touches the relays. The service reads the DHT22 too, and GPIO22
carries only one reader at a time, so stop squid-dryer.service while testing the DHT22 (or pass --no-dht).

    python sensor2_test.py                   # test every extra sensor, Ctrl+C to stop
    python sensor2_test.py --expect 3        # fail loudly unless 3 sensors in total (main + 2) show up
    python sensor2_test.py --count 10        # 10 readings, then exit
    python sensor2_test.py --id 28-0000001234ab   # test one specific sensor (the DHT22's id is dht22-gpio22)
    python sensor2_test.py --no-dht          # leave the DHT22 out

Calibration: put all sensors side by side, let them settle, then
    python sensor2_test.py --calibrate           # match every extra sensor to the main one (120 s)
    python sensor2_test.py --calibrate --true-temp 47.0   # match them to a trusted thermometer instead
    python sensor2_test.py --calibrate --true-humidity 55.0   # also match the DHT22's humidity to a trusted hygrometer
There is no humidity reference on the Pi, so the humidity offset is only measured when --true-humidity is given.
The offsets are saved to data/sensor2_calibration.json, one per sensor id, and applied by this
script from then on (--raw shows the uncalibrated readings). The dryer service applies the same
offsets to the middle and top sensors (never to sensor 1) when it starts, so restart it after
recalibrating. The file is read and written through calibration.py.
"""
import argparse
import time
from datetime import datetime
from statistics import mean

import calibration
import config
import dht22

# family codes the w1_therm driver handles: DS18B20 (28), DS1822 (22), DS18S20 (10), DS1825 (3b)
THERM_FAMILIES = ("28", "22", "10", "3b")

CAL_MIN_SAMPLES = 10
CAL_SETTLE_C = 0.15   # the error may move this much between the first and second half of a run
CAL_SETTLE_RH = 1.0   # same for the humidity error, in %RH (a DHT22 is only good to ~2 %RH)
COL_WIDTH = 16        # "31.29 C (+0.04)"
DHT_COL_WIDTH = 28    # "31.29 C (+0.04)  45.1 %RH"
DHT_PROBE_ATTEMPTS = 3
DHT_MAX_BAD_FRACTION = 0.2  # a DHT22 misses an occasional read; more than this is a wiring or timing problem


class Dht22Sensor:
    """The top sensor: a DHT22 on one GPIO pin instead of a 1-Wire thermometer. Reads humidity too."""

    def __init__(self, pin):
        self.pin = pin
        self.name = config.DHT22_ID if pin == config.DHT22_PIN else f"dht22-gpio{pin}"


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


def read(sensor):
    """Return (temp_c, humidity, None) for a good read, or (None, None, reason) for a bad one.
    Humidity is None for a 1-Wire thermometer, which has none."""
    if isinstance(sensor, Dht22Sensor):
        return dht22.read(sensor.pin)
    temp, err = read_temp(sensor)
    return temp, None, err


def has_humidity(sensor):
    return isinstance(sensor, Dht22Sensor)


def probe_dht(sensor):
    """Look for the DHT22 before the run (it misses a read now and then, so a few tries). Returns a reading."""
    for attempt in range(DHT_PROBE_ATTEMPTS):
        if attempt:
            time.sleep(config.DHT22_MIN_INTERVAL_S)
        reading = dht22.read(sensor.pin)
        if reading.error is None:
            break
    return reading


def no_sensor_help(base, found):
    if not base.exists():
        print(f"{base} does not exist. 1-Wire is not enabled: add dtoverlay=w1-gpio to /boot/firmware/config.txt and reboot.")
    elif not found:
        print("No 1-Wire thermometer on the bus at all. Check wiring, the 4.7k pull-up, and that 1-Wire is enabled.")
    else:
        buses = sorted(p.name for p in base.glob("w1_bus_master*"))
        print("Only the main sensor is showing up, the extra ones are not.")
        if len(buses) < 2:
            print("Only one 1-Wire bus is active (GPIO4, the main sensor). For the middle sensor on its own pin, add")
            print("  dtoverlay=w1-gpio,gpiopin=23")
            print("to /boot/firmware/config.txt and reboot, then wire its data to GPIO23 (physical pin 16).")
            print(f"GPIO{config.DHT22_PIN} is the DHT22's: it must not have a 1-Wire overlay.")
        else:
            print(f"A second bus is up ({', '.join(buses)}) but no sensor answers on it.")
            print("Check the wiring: red to 3V3, black to GND, yellow/data to GPIO23 (the middle sensor),")
            print("with a 4.7k pull-up from each data wire to 3V3.")


def settled_errors(name, what, errs, settle, unit):
    """The second half of the paired errors, or None (after saying why) if there are too few or they have not settled.
    The first half is allowed to still be settling; the offset comes from the second half only."""
    if len(errs) < CAL_MIN_SAMPLES:
        print(f"{name}: only {len(errs)} good paired {what} reads, need {CAL_MIN_SAMPLES}. Nothing saved.")
        return None
    half = len(errs) // 2
    first, second = errs[:half], errs[half:]
    drift = mean(second) - mean(first)
    if abs(drift) > settle:
        print(f"{name}: {what} not settled, the error moved {drift:+.2f} {unit} between the first and second half. "
              "Nothing saved. Leave the sensors together a few more minutes and run it again.")
        return None
    return second


def calibrate(targets, ref, seconds, interval, true_temp, true_humidity=None):
    """Measure how far each target reads from the reference and save the corrections. Returns an exit code.
    Humidity is matched to true_humidity (a trusted hygrometer) on the targets that measure it."""
    against = f"true temperature {true_temp:.2f} C" if true_temp is not None else f"sensor 1 (main) {ref.name}"
    print(f"Calibrating {', '.join(t.name for t in targets)} against the {against} for {seconds} s.")
    if true_humidity is not None:
        print(f"Humidity is matched to the true humidity {true_humidity:.1f} %RH.")
    print("Keep the sensors touching and still. Ctrl+C aborts without saving.\n")

    errors = {t.name: [] for t in targets}  # reading minus reference, one per paired good read
    rh_errors = {t.name: [] for t in targets if has_humidity(t)}
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            ref_temp = true_temp if true_temp is not None else read_temp(ref)[0]
            cells = [f"reference {ref_temp:.2f} C" if ref_temp is not None else "reference BAD"]
            for t in targets:
                temp, humidity, _ = read(t)
                if temp is None:
                    cells.append(f"{t.name} bad")
                    continue
                cell = f"{t.name} {temp:.2f} C"
                if ref_temp is not None:
                    errors[t.name].append(temp - ref_temp)
                    cell += f" ({errors[t.name][-1]:+.2f})"
                if humidity is not None:
                    cell += f" {humidity:.1f} %RH"
                    if true_humidity is not None:
                        rh_errors[t.name].append(humidity - true_humidity)
                        cell += f" ({rh_errors[t.name][-1]:+.1f})"
                cells.append(cell)
            print(f"{datetime.now():%H:%M:%S}  " + "  |  ".join(cells), flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nAborted, nothing saved.")
        return 1

    print()
    code = 0
    for t in targets:
        second = settled_errors(t.name, "temperature", errors[t.name], CAL_SETTLE_C, "C")
        if second is None:
            code = 1
        else:
            offset = round(-mean(second), 2)
            calibration.save_offset(t.name, offset, against, len(second))
            print(f"{t.name}: error {mean(second):+.2f} C (spread {max(second) - min(second):.2f} C, "
                  f"{len(second)} reads) -> saved offset {offset:+.2f} C")

        if not has_humidity(t):
            continue
        if true_humidity is None:
            print(f"{t.name}: humidity not calibrated, there is no reference for it. "
                  "Pass --true-humidity with a trusted hygrometer reading to calibrate it.")
            continue
        second = settled_errors(t.name, "humidity", rh_errors[t.name], CAL_SETTLE_RH, "%RH")
        if second is None:
            code = 1
            continue
        offset_rh = round(-mean(second), 2)
        calibration.save_humidity_offset(t.name, offset_rh, f"true humidity {true_humidity:.2f} %RH", len(second))
        print(f"{t.name}: humidity error {mean(second):+.2f} %RH (spread {max(second) - min(second):.2f} %RH, "
              f"{len(second)} reads) -> saved offset {offset_rh:+.2f} %RH")
    if code == 0:
        print(f"\nSaved to {calibration.CAL_PATH}. Check it with: python sensor2_test.py --count 10")
        print("Restart squid-dryer.service so the dryer picks the new offsets up.")
    return code


def main():
    ap = argparse.ArgumentParser(description="Read-only test and calibration for the extra temperature sensors.")
    ap.add_argument("--id", help="sensor id to test, e.g. 28-0000001234ab or dht22-gpio22 "
                                 "(default: every sensor except the main one)")
    ap.add_argument("--interval", type=float, default=config.SENSOR_INTERVAL, help="seconds between readings")
    ap.add_argument("--count", type=int, default=0, help="stop after this many readings (default: until Ctrl+C)")
    ap.add_argument("--expect", type=int, metavar="N",
                    help="total sensors that should answer, main included; fewer fails the test")
    ap.add_argument("--calibrate", nargs="?", type=int, const=120, metavar="SECONDS",
                    help="measure the offsets against sensor 1 (main) and save them (default 120 s)")
    ap.add_argument("--true-temp", type=float, metavar="C",
                    help="with --calibrate: the real temperature from a trusted thermometer, instead of sensor 1")
    ap.add_argument("--true-humidity", type=float, metavar="PCT",
                    help="with --calibrate: the real relative humidity from a trusted hygrometer, to calibrate the DHT22's humidity")
    ap.add_argument("--dht-pin", type=int, default=config.DHT22_PIN, metavar="GPIO",
                    help=f"GPIO the DHT22 data wire is on (default {config.DHT22_PIN})")
    ap.add_argument("--no-dht", action="store_true", help="leave the DHT22 out")
    ap.add_argument("--raw", action="store_true", help="ignore the saved calibration")
    args = ap.parse_args()
    if args.true_temp is not None and args.calibrate is None:
        ap.error("--true-temp only makes sense with --calibrate")
    if args.true_humidity is not None and args.calibrate is None:
        ap.error("--true-humidity only makes sense with --calibrate")
    if args.true_humidity is not None and not 0.0 <= args.true_humidity <= 100.0:
        ap.error("--true-humidity is a relative humidity, 0 to 100")
    if args.true_humidity is not None and args.no_dht:
        ap.error("--true-humidity calibrates the DHT22, which --no-dht leaves out")

    base = config.W1_BASE
    found = list_sensors(base) if base.exists() else []
    names = [s.name for s in found]
    dht = None if args.no_dht else Dht22Sensor(args.dht_pin)

    buses = {}
    for s in found:
        buses.setdefault(bus_of(s), []).append(f"{s.name} (sensor 1, main)" if s.name == config.SENSOR_ID else s.name)
    print(f"{len(found)} 1-Wire sensor(s) found:")
    for bus, members in sorted(buses.items()):
        print(f"  {bus}: {', '.join(members)}")
    if not found:
        print("  none")

    if args.id:
        if dht and args.id == dht.name:
            targets = [dht]
        elif args.id in names:
            targets = [base / args.id]
        else:
            print(f"{args.id} is not on the bus.")
            return 1
    else:
        targets = [s for s in found if s.name != config.SENSOR_ID] + ([dht] if dht else [])

    dht_found = False
    if dht in targets:
        probe = probe_dht(dht)
        dht_found = probe.error is None
        if dht_found:
            print(f"DHT22 on GPIO{dht.pin} ({dht.name}): found, {probe.temp_c:.1f} C {probe.humidity:.1f} %RH")
        else:
            print(f"DHT22 on GPIO{dht.pin} ({dht.name}): not answering: {probe.error}")

    total = len(found) + dht_found
    missing = args.expect is not None and total < args.expect
    if missing:
        print(f"Expected {args.expect} sensors, found {total}: {args.expect - total} missing. "
              "Check its wiring (3V3, GND, data, 4.7k pull-up) and which pin its data wire is on.")

    if not targets:
        no_sensor_help(base, found)
        return 1

    ref = base / config.SENSOR_ID if config.SENSOR_ID in names and config.SENSOR_ID not in [t.name for t in targets] else None

    # the DHT22 measures once per 2 s: asking sooner just returns the previous reading again
    interval = max(args.interval, config.DHT22_MIN_INTERVAL_S) if dht in targets else args.interval

    if args.calibrate is not None:
        if ref is None and args.true_temp is None:
            print("Sensor 1 (main) is not available as a reference. Pass --true-temp with a trusted reading instead.")
            return 1
        return calibrate(targets, ref, args.calibrate, interval, args.true_temp, args.true_humidity)

    offsets = {t.name: 0.0 if args.raw else calibration.load_offset(t.name) for t in targets}
    rh_offsets = {t.name: 0.0 if args.raw else calibration.load_humidity_offset(t.name)
                  for t in targets if has_humidity(t)}
    for name, offset in offsets.items():
        if offset:
            print(f"Calibration applied to {name}: {offset:+.2f} C (--raw shows the uncalibrated reading)")
    for name, offset in rh_offsets.items():
        if offset:
            print(f"Calibration applied to {name}: {offset:+.2f} %RH (--raw shows the uncalibrated reading)")
    print("\nCells show the reading and, in brackets, the difference from sensor 1. Ctrl+C to stop.\n")

    cols = ([("sensor 1 (main)", COL_WIDTH, ref)] if ref else []) + \
           [(t.name, DHT_COL_WIDTH if has_humidity(t) else COL_WIDTH, t) for t in targets]
    header = f"{'time':<8}  " + "  ".join(f"{label:<{width}}" for label, width, _ in cols)

    good = {t.name: [] for t in targets}
    good_rh = {t.name: [] for t in targets if has_humidity(t)}
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
                temp, humidity, err = read(t)
                if temp is None:
                    bad[t.name] += 1
                    cells.append("BAD")
                    problems.append(f"{t.name}: {err}")
                    continue
                temp += offsets[t.name]
                good[t.name].append(temp)
                diff = f" ({temp - ref_temp:+.2f})" if ref_temp is not None else ""
                cell = f"{temp:.2f} C{diff}"
                if humidity is not None:
                    humidity = calibration.corrected_humidity(humidity, rh_offsets[t.name])
                    good_rh[t.name].append(humidity)
                    cell += f"  {humidity:.1f} %RH"
                cells.append(cell)
            print(f"{datetime.now():%H:%M:%S}  " + "  ".join(f"{c:<{width}}" for c, (_, width, _) in zip(cells, cols)), flush=True)
            for p in problems:
                print(f"          ! {p}")
            if args.count and n >= args.count:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print()

    print("\n--- summary ---")
    ok = not missing
    for t in targets:
        g, b = good[t.name], bad[t.name]
        stats = f", min {min(g):.2f} avg {sum(g) / len(g):.2f} max {max(g):.2f} C" if g else ""
        if has_humidity(t) and good_rh[t.name]:
            h = good_rh[t.name]
            stats += f"; humidity min {min(h):.1f} avg {sum(h) / len(h):.1f} max {max(h):.1f} %RH"
        print(f"{t.name}: {len(g)} good, {b} bad{stats}")
        if has_humidity(t):
            ok = ok and bool(g) and b <= DHT_MAX_BAD_FRACTION * (len(g) + b)
        else:
            ok = ok and bool(g) and b == 0
    print("PASS" if ok else "FAIL: a sensor is missing, had bad reads, or had no reads")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
