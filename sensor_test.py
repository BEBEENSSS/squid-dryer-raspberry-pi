from pathlib import Path
from time import sleep

BASE = Path("/sys/bus/w1/devices")


def find_sensors():
    # DS18B20 devices show up as folders starting with 28-
    return sorted(BASE.glob("28-*"))


def read_temp(sensor):
    """Return temperature in C, or None if the read is bad."""
    try:
        lines = (sensor / "w1_slave").read_text().strip().splitlines()
    except OSError:
        return None

    # Line 1 must end with YES (CRC ok)
    if len(lines) < 2 or not lines[0].endswith("YES"):
        return None

    # Line 2 looks like: "... t=23125"
    if "t=" not in lines[1]:
        return None

    temp = int(lines[1].split("t=")[1]) / 1000.0

    # -127 = disconnected, 85 = power-on default (not a real reading)
    if temp == -127.0 or temp == 85.0:
        return None

    return temp


sensors = find_sensors()
if not sensors:
    print("No DS18B20 found. Check wiring, the 4.7k pull-up, and that 1-Wire is enabled.")
    raise SystemExit(1)

for s in sensors:
    print(f"Found sensor: {s.name}")

try:
    while True:
        for s in sensors:
            temp = read_temp(s)
            if temp is None:
                print(f"{s.name}: BAD READ (heater should be OFF)")
            else:
                print(f"{s.name}: {temp:.2f} C")
        sleep(1)
except KeyboardInterrupt:
    print("\nStopped")