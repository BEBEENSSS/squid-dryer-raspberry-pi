import queue
import signal
import threading
import time
from pathlib import Path

import config
from cloud_sync import CloudSync
from hardware import Outputs, Sensor
from task_manager import TaskManager
from task_store import TaskStore

TIMESYNC_FLAG = Path("/run/systemd/timesync/synchronized")


def read_boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def clock_synced():
    """True once systemd-timesyncd has synced the clock this boot (the Pi has no RTC)."""
    return TIMESYNC_FLAG.exists()


class ManualControl:
    """The original commands/fan + commands/heater behaviour, used when no task is active."""

    def __init__(self, out, sensor, store, sync, tm, mono=time.monotonic, cfg=config):
        self.out = out
        self.sensor = sensor
        self.store = store
        self.sync = sync
        self.tm = tm
        self.mono = mono
        self.cfg = cfg
        self.heater_since = None
        self.failsafe_tripped = True  # heater blocked until Firebase confirms a fresh command

    def _event(self, message):
        print(message)
        self.store.log_event("manual", message)

    def _reset_heater_command(self):
        self.sync.request_write("commands/heater", False)

    def heater_off(self):
        self.heater_since = None
        self.out.heater.off()

    def apply(self, want_fan, want_heater):
        """Apply the wanted state from Firebase with the safety rules."""
        if self.tm.busy():
            if want_fan or want_heater:
                self._event("manual command ignored: drying task active")
            return
        cfg, fan = self.cfg, self.out.fan
        reject = None
        self.tm.cancel_cooldown()

        if want_fan:
            fan.on()
        else:
            self.heater_off()  # never heat without airflow
            fan.off()

        temp = self.sensor.temp()
        if not want_heater:
            self.heater_off()
        elif self.failsafe_tripped:
            self.heater_off()
            reject = "heater blocked after restart or connection loss, re-enable it"
        elif not fan.is_active:
            self.heater_off()
            reject = "heater needs the fan on"
        elif temp is None:
            self.heater_off()
            reject = "heater blocked, no valid temperature reading"
        elif temp >= cfg.TEMP_MAX_C - cfg.TEMP_RESUME_DROP:
            self.heater_off()
            reject = f"heater blocked, too hot ({temp:.1f} C), let it cool"
        elif not self.out.heater.is_active:
            self.out.heater.on()
            self.heater_since = self.mono()

        if reject:
            self._reset_heater_command()
            self._event(reject)

    def tick(self):
        """Limits, auto-off and offline failsafe for manual heating."""
        if self.tm.busy():
            self.heater_since = None
            return
        cfg = self.cfg

        # first contact / reconnect: clear any stale heater command before it can apply
        if self.failsafe_tripped and self.sync.online:
            self._reset_heater_command()
            self.failsafe_tripped = False
            self._event("connection ok, manual heater stays off until re-enabled")

        if not self.out.heater.is_active:
            return
        temp = self.sensor.temp()
        since_ok = self.sync.seconds_since_ok()
        if temp is None:
            reason = "heater off: temperature sensor failed"
        elif temp >= cfg.TEMP_MAX_C:
            reason = f"heater off: over temperature ({temp:.1f} C)"
        elif self.heater_since is not None and self.mono() - self.heater_since >= cfg.HEATER_MAX_SECONDS:
            reason = "heater auto-off: max runtime reached"
        elif since_ok is None or since_ok > cfg.OFFLINE_FAILSAFE_SECONDS:
            self.failsafe_tripped = True
            reason = "FAILSAFE: Firebase unreachable, manual heater off"
        else:
            return
        self.heater_off()
        self._reset_heater_command()
        self._event(reason)


def main():
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    out = Outputs()  # both relays off before anything else happens
    sensor = Sensor()
    boot_id = read_boot_id()

    def open_store():
        return TaskStore(config.DB_PATH, boot_id, clock_synced)

    store = open_store()
    prev_boot = store.get_meta("boot_id")
    clean = store.get_meta("clean_shutdown") == "1"
    store.set_meta(boot_id=boot_id, clean_shutdown=0)

    commands = queue.Queue()
    sync = None

    def on_task_start():
        # a stale manual command must not take over when the task ends
        sync.request_write("commands/heater", False)
        sync.request_write("commands/fan", False)

    tm = TaskManager(store, out, sensor, boot_id, clock_synced, on_task_start=on_task_start)

    def status():
        ok = sensor.ok()
        machine = tm.machine_status()
        return {
            "fan": out.fan.is_active,
            "heater": out.heater.is_active,
            "temp": round(sensor.temp_c, 1) if ok else None,  # None removes the key
            "sensor_ok": ok,
            "temp_max": config.TEMP_MAX_C,
            "status": machine,
            "ready": machine == "ready" and ok and not out.heater.is_active,
        }

    sync = CloudSync(open_store, tm, status, commands, boot_id)
    manual = ManualControl(out, sensor, store, sync, tm)

    # get a first reading before the recovery check starts
    sensor.poll()
    last_read = time.monotonic()
    store.log_event("device_started", "device started", data={
        "boot_changed": prev_boot != boot_id, "previous_boot_id": prev_boot,
        "clean_shutdown": clean, "clock_synced": clock_synced(),
    })
    tm.startup(prev_boot, clean)

    threading.Thread(target=sync.run, args=(stop,), daemon=True, name="cloud-sync").start()
    print("Running. Control it from Firebase.")

    try:
        while not stop.wait(config.LOOP_INTERVAL):
            now = time.monotonic()
            if now - last_read >= config.SENSOR_INTERVAL:
                sensor.poll()
                last_read = now

            while True:
                try:
                    kind, payload = commands.get_nowait()
                except queue.Empty:
                    break
                if kind == "task":
                    ack = tm.handle_command(payload)
                    if ack:
                        print("command:", ack)
                else:
                    manual.apply(*payload)

            tm.tick()
            manual.tick()
    finally:
        out.all_off()
        tm.shutdown()
        if stop.is_set():  # a crash is not a clean shutdown
            store.log_event("device_stopped", "device stopped")
            store.set_meta(clean_shutdown=1)
        # one last publish, but never wait long for the network
        t = threading.Thread(target=sync.sync_once, daemon=True)
        t.start()
        t.join(5)
        print("All off")


if __name__ == "__main__":
    main()
