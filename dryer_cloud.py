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


def coalesce_commands(items):
    """Keep task and settings commands in order, but only the latest manual request.

    Manual requests are a wanted state, not events, so applying every intermediate one
    would only flap the relays.
    """
    last_manual = None
    for i, (kind, _) in enumerate(items):
        if kind == "manual":
            last_manual = i
    return [item for i, item in enumerate(items) if item[0] != "manual" or i == last_manual]


class ManualControl:
    """The commands/fan + commands/heater behaviour, used when no task is active.

    commands/heater=true means "manual heating enabled": the Pi then holds the saved target
    temperature itself, with hysteresis, and keeps doing so offline. The heater cut-off pauses it.
    """

    def __init__(self, out, sensor, store, sync, tm, mono=time.monotonic, cfg=config):
        self.out = out
        self.sensor = sensor
        self.store = store
        self.sync = sync
        self.tm = tm
        self.mono = mono
        self.cfg = cfg
        self.enabled = False
        self.block = None             # why a heater request was refused, for the dashboard
        self.failsafe_tripped = True  # heater blocked after boot until the operator re-enables it

    def _event(self, message):
        print(message)
        self.store.log_event("manual", message)

    def _reset_heater_command(self):
        self.sync.request_write("commands/heater", False)

    def heater_off(self):
        self.out.heater.off()

    def _disable(self, reason, block):
        self.enabled = False
        self.block = block
        self.heater_off()
        self._reset_heater_command()
        self._event(reason)

    def apply(self, want_fan, want_heater):
        """Apply the wanted state from Firebase with the safety rules."""
        if self.tm.busy():
            if want_fan or want_heater:
                self._event("manual command ignored: drying task active")
            return
        if self.tm.cooling_active():
            # cooling is driven by the measured temperature, not by a stale request
            if want_fan or want_heater:
                self._event("manual command ignored: machine is cooling")
                self.sync.request_write("commands/heater", False)
                self.sync.request_write("commands/fan", False)
            return
        fan = self.out.fan

        if want_fan:
            fan.on()
        else:
            self.enabled = False
            self.heater_off()  # never heat without airflow
            fan.off()

        reject = None
        if not want_heater:
            self.enabled = False
            self.block = None
            self.heater_off()
        elif self.failsafe_tripped:
            self.enabled = False
            self.heater_off()
            reject, self.block = "heater blocked after restart, re-enable it", "reboot_block"
        elif not fan.is_active:
            self.enabled = False
            self.heater_off()
            reject, self.block = "heater needs the fan on", "no_fan"
        elif self.sensor.temp() is None:
            self.enabled = False
            self.heater_off()
            reject, self.block = "heater blocked, no valid temperature reading", "sensor_fault"
        else:
            self.enabled = True
            self.block = None
            self._regulate()

        if reject:
            self._reset_heater_command()
            self._event(reject)

    def _regulate(self):
        """Hold the saved target. Faults come first and switch manual heating off."""
        cfg = self.cfg
        temp = self.sensor.temp()
        if temp is None:
            return self._disable("heater off: temperature sensor failed", "sensor_fault")
        if temp >= cfg.TEMP_MAX_C:
            return self._disable(f"heater off: over temperature ({temp:.1f} C)", "over_temperature")
        if not self.out.fan.is_active:
            return self._disable("heater off: fan is not running", "no_fan")

        settings = self.tm.settings
        cut = self.tm.cutoff.update(temp, settings.get("heater_cutoff_c"))
        target = settings.get("manual_target_temp_c")
        if cut or temp >= target:
            self.heater_off()
        elif temp <= target - cfg.MANUAL_HYSTERESIS_C and not self.out.heater.is_active:
            self.out.heater.on()

    def tick(self):
        """Faults, reboot block and thermostat for manual heating."""
        if self.tm.busy() or self.tm.cooling_active():
            self.enabled = False
            return

        # first contact after boot: clear any stale heater command before it can apply
        if self.failsafe_tripped and self.sync.online:
            self._reset_heater_command()
            self.failsafe_tripped = False
            self._event("connection ok, manual heater stays off until re-enabled")

        if self.enabled:
            self._regulate()
        elif self.out.heater.is_active:
            self.heater_off()  # nothing asked for heat, so nothing may be heating
        # keep the cut-off latch current while idle so the dashboard shows the real condition
        self.tm.cutoff.update(self.sensor.temp(), self.tm.settings.get("heater_cutoff_c"))


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

    def heater_block(ok):
        """The active safety condition keeping the heater off, or None (None removes the key)."""
        if not ok:
            return "sensor_fault"
        if sensor.temp_c >= config.TEMP_MAX_C:
            return "over_temperature"
        if tm.cutoff.active:
            return "cutoff"
        return manual.block

    def status():
        ok = sensor.ok()
        machine = tm.machine_status()
        # a new task may start while cooling once the machine is cool enough to accept it
        cool_enough = ok and sensor.temp_c < config.TEMP_MAX_C - config.TEMP_RESUME_DROP
        return {
            "fan": out.fan.is_active,
            "heater": out.heater.is_active,
            "temp": round(sensor.temp_c, 1) if ok else None,  # None removes the key
            "sensor_ok": ok,
            "temp_max": config.TEMP_MAX_C,
            "status": machine,
            "cooling": tm.cooling_active(),
            "safe_temp": tm.safe_temp_c,
            "heater_cutoff": tm.heater_cutoff_c,
            "heater_cutoff_margin": config.HEATER_CUTOFF_MARGIN_C,
            "target_temp": tm.settings.get("manual_target_temp_c"),
            "target_humidity": tm.settings.get("manual_target_humidity"),
            "settings_updated_at": tm.settings.updated_at,
            "manual_heating": manual.enabled,
            "heater_block": heater_block(ok),
            "ready": machine in ("ready", "cooling") and cool_enough and not out.heater.is_active,
        }

    sync = CloudSync(open_store, tm, status, commands, boot_id)
    manual = ManualControl(out, sensor, store, sync, tm)

    # get a first reading before the recovery check starts
    sensor.poll()
    store.log_event("device_started", "device started", data={
        "boot_changed": prev_boot != boot_id, "previous_boot_id": prev_boot,
        "clean_shutdown": clean, "clock_synced": clock_synced(),
    })
    tm.startup(prev_boot, clean)

    def read_sensor():
        # the 1-Wire read takes about a second, so it must not hold up command handling
        while not stop.wait(config.SENSOR_INTERVAL):
            sensor.poll()

    threading.Thread(target=read_sensor, daemon=True, name="sensor").start()
    threading.Thread(target=sync.run, args=(stop,), daemon=True, name="cloud-sync").start()
    print("Running. Control it from Firebase.")

    try:
        next_tick = time.monotonic()
        last_published = None
        while not stop.is_set():
            # wake as soon as a command arrives instead of sleeping out the tick
            try:
                items = [commands.get(timeout=max(0.0, next_tick - time.monotonic()))]
            except queue.Empty:
                items = []
            while True:
                try:
                    items.append(commands.get_nowait())
                except queue.Empty:
                    break

            for kind, payload in coalesce_commands(items):
                if kind == "task":
                    ack = tm.handle_command(payload)
                    if ack:
                        print("command:", ack)
                elif kind == "settings":
                    tm.apply_settings(payload)
                else:
                    manual.apply(*payload)

            now = time.monotonic()
            if now >= next_tick:
                tm.tick()
                manual.tick()
                next_tick = now + config.LOOP_INTERVAL

            # report a confirmed change at once (commands, thermostat, cooling end, failsafe)
            confirmed = (out.fan.is_active, out.heater.is_active, tm.machine_status())
            if items or confirmed != last_published:
                last_published = confirmed
                sync.notify()
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
