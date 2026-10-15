import os
import sys
from pathlib import Path

os.environ["GPIOZERO_PIN_FACTORY"] = "mock"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from gpiozero import Device  # noqa: E402

import config  # noqa: E402
from hardware import Outputs, Sensor, SensorArray  # noqa: E402
from task_manager import TaskManager  # noqa: E402
from task_store import TaskStore  # noqa: E402


class Clock:
    def __init__(self):
        self.mono = 1000.0
        self.wall = 1_800_000_000.0
        self.synced = True

    def advance(self, s):
        self.mono += s
        self.wall += s


class Oven:
    """Temperature source. Follows the heater unless pinned with .fixed / .broken."""

    def __init__(self, temp=30.0):
        self.temp = temp
        self.fixed = None
        self.broken = False
        self.heater = None
        # the middle and top sensors: they follow the bottom one (self.temp) unless pinned or broken
        self.zone_drop = {"middle": 0.0, "top": 0.0}   # degrees cooler than the bottom
        self.zone_lag = {"middle": 0.0, "top": 0.0}    # seconds they trail the bottom (0 = instantly)
        self.zone_fixed = {}                           # zone -> pinned reading
        self.zone_broken = set()
        self.zone_temp = {"middle": temp, "top": temp}  # state of a lagging zone

    def read(self):
        if self.broken:
            return None
        return self.fixed if self.fixed is not None else round(self.temp, 2)

    def read_zone(self, zone):
        if zone in self.zone_broken:
            return None
        if zone in self.zone_fixed:
            return self.zone_fixed[zone]
        if self.zone_lag[zone] <= 0:
            return round(self.temp - self.zone_drop[zone], 2)
        return round(self.zone_temp[zone], 2)

    def step(self, s):
        if self.heater is not None and self.heater.is_active:
            self.temp += 0.5 * s
        else:
            self.temp = max(28.0, self.temp - 0.2 * s)
        for zone, lag in self.zone_lag.items():
            if lag > 0:
                goal = self.temp - self.zone_drop[zone]
                self.zone_temp[zone] += (goal - self.zone_temp[zone]) * min(1.0, s / lag)


class FakeBase:
    """In-memory stand-in for a firebase_admin Reference at devices/<id>."""

    def __init__(self):
        self.data = {}
        self.fail = False
        self.updates = 0
        self.server_ms = None  # when set, server timestamps resolve to it like the real database
        self.ack_lost = False  # the next update is stored, then raises: the reply never arrived
        self.fail_updates = 0  # how many of the next updates fail before storing anything (a flaky link)

    def _check(self):
        if self.fail:
            raise ConnectionError("network down")

    def child(self, path):
        base = self

        class Ref:
            def get(self_, shallow=False):
                base._check()
                node = base.data
                for part in path.split("/"):
                    node = node.get(part) if isinstance(node, dict) else None
                if shallow and isinstance(node, dict):
                    return {key: True for key in node}  # keys only, like a shallow read
                return node

        return Ref()

    def update(self, values):
        self._check()
        self.updates += 1
        if self.fail_updates > 0:
            self.fail_updates -= 1
            raise TimeoutError("update timed out")
        self._apply(values)
        if self.ack_lost:
            self.ack_lost = False
            raise TimeoutError("reply lost after the update was stored")

    def _apply(self, values):
        for path, value in values.items():
            parts = path.split("/")
            node = self.data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            if value == {".sv": "timestamp"} and self.server_ms is not None:
                value = int(self.server_ms)
            if value is None:
                node.pop(parts[-1], None)
            else:
                node[parts[-1]] = value

    def get(self, path):
        return self.child(path).get()


class Rig:
    """One controller instance on a shared DB, with a fake clock and sensor."""

    instances = []

    def __init__(self, tmp_path, clock=None, oven=None, boot_id="boot-1", prev_boot=None, clean=False,
                 zoned=False, zone_ids=None):
        """`zoned=True` adds the middle and top sensors (read from the oven). `zone_ids` is the id map the
        array is checked against; leave it out for "ids not applicable"."""
        Rig.instances.append(self)
        self.tmp_path = tmp_path
        self.clock = clock or Clock()
        self.oven = oven or Oven()
        self.boot_id = boot_id
        self.zoned, self.zone_ids = zoned, zone_ids
        self.out = Outputs()
        self.oven.heater = self.out.heater
        self.sensor = Sensor(self.oven.read, clock=lambda: self.clock.mono)
        self.zones = None
        if zoned:
            mono = lambda: self.clock.mono  # noqa: E731
            self.zones = SensorArray(
                {"bottom": self.sensor,
                 "middle": Sensor(lambda: self.oven.read_zone("middle"), clock=mono),
                 "top": Sensor(lambda: self.oven.read_zone("top"), clock=mono)}, ids=zone_ids)
        self.sensors = list(self.zones.sensors.values()) if self.zones else [self.sensor]
        self.store = TaskStore(tmp_path / "dryer.db", boot_id, lambda: self.clock.synced,
                               wall=lambda: self.clock.wall)
        prev = self.store.get_meta("boot_id") if prev_boot is None else prev_boot
        clean = clean or self.store.get_meta("clean_shutdown") == "1"
        self.store.set_meta(boot_id=boot_id, clean_shutdown=0)
        self.tm = TaskManager(self.store, self.out, self.sensor, boot_id, lambda: self.clock.synced,
                              mono=lambda: self.clock.mono, wall=lambda: self.clock.wall, zones=self.zones)
        for s in self.sensors:
            s.poll()
        self.tm.startup(prev, clean)
        self._since_read = 0.0
        self._n = 0

    def run(self, seconds, step=1.0):
        t = 0.0
        while t < seconds:
            self.clock.advance(step)
            self.oven.step(step)
            self._since_read += step
            if self._since_read >= config.SENSOR_INTERVAL:
                for s in self.sensors:
                    s.poll()
                self._since_read = 0.0
            self.tm.tick()
            t += step

    def start(self, task_id="T1", duration_min=10, warmup=False, **kw):
        """Send a start command.

        Most tests are about drying, so by default the task is moved straight into drying the
        way the warm-up hand-over does it. Pass warmup=True to exercise the real warm-up phase.
        """
        self._n += 1
        cmd = {"command_id": kw.pop("command_id", f"c{self._n}-{task_id}"), "command": "start",
               "task_id": task_id, "weight_kg": 2.5, "batch_count": 20, "row_levels": "2",
               "duration_minutes": duration_min}
        cmd.update(kw)
        ack = self.tm.handle_command(cmd)
        if ack and ack["result"] == "accepted" and not warmup:
            self.tm._begin_running(0.0, self.tm.mono(), ("task_running", "drying started", {}),
                                   drying_started_wall=self.clock.wall,
                                   target_confirmed_wall=self.clock.wall)
        return ack

    def cmd(self, action, task_id=None, **kw):
        self._n += 1
        return self.tm.handle_command({"command_id": f"c{self._n}-{action}", "command": action,
                                       "task_id": task_id, **kw})

    def crash(self):
        """Process dies: nothing gets a chance to run, relays drop with it."""
        self.store.close()
        if not self.out.heater.closed:
            self.out.close()

    def stop(self):
        """Clean SIGTERM path."""
        self.tm.shutdown()
        self.store.set_meta(clean_shutdown=1)
        self.crash()

    def reboot(self, downtime, new_boot=True, temp=None, synced=True, clean=False):
        """Return a new Rig as if the controller came back after `downtime` seconds."""
        self.crash() if not clean else self.stop()
        clock = self.clock
        clock.wall += downtime
        clock.mono = 5.0 if new_boot else clock.mono + downtime  # monotonic restarts on a new boot
        clock.synced = synced
        if temp is not None:
            self.oven.temp = temp
        return Rig(self.tmp_path, clock, self.oven,
                   boot_id=self.boot_id + "x" if new_boot else self.boot_id,
                   zoned=self.zoned, zone_ids=self.zone_ids)

    def task(self, task_id="T1"):
        return self.store.get_task(task_id)


@pytest.fixture(autouse=True)
def _pins():
    yield
    for r in Rig.instances:  # close explicitly, GC-time close deadlocks gpiozero's pin lock
        r.crash()
    Rig.instances.clear()
    if Device.pin_factory is not None:  # pure-logic tests never create a device
        Device.pin_factory.reset()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r


@pytest.fixture
def zrig(tmp_path):
    """A rig with all three sensors (bottom, middle, top), ids not checked."""
    yield Rig(tmp_path, zoned=True)
