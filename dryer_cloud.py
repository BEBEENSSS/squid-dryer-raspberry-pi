import os
import queue
import signal
import threading
import time
from pathlib import Path

# SIMULATE=1 runs the whole controller with mock pins and a simulated chamber. The pin factory
# has to be chosen before gpiozero is imported by hardware.
SIMULATE = os.environ.get("SIMULATE") == "1"
if SIMULATE:
    os.environ.setdefault("GPIOZERO_PIN_FACTORY", "mock")

import calibration  # noqa: E402
import config  # noqa: E402
import sd_notify  # noqa: E402
from cloud_sync import CloudSync, firebase_base  # noqa: E402
from command_age import LocalCommandMark  # noqa: E402
from hardware import Outputs, Sensor, SensorArray, make_reader  # noqa: E402
from link_monitor import LinkMonitor  # noqa: E402
from local_api import LocalApi  # noqa: E402
from netinfo import lan_ip, read_network_status  # noqa: E402
from network_control import NetworkControl, network_ack_payload  # noqa: E402
from snapshot import build_snapshot, zone_state  # noqa: E402
from task_manager import TaskManager  # noqa: E402
from task_store import TaskStore  # noqa: E402
from temp_sync import Gauge, TempRecorder, TempUploader  # noqa: E402

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
        self.last_command_id = None     # newest manual command_id received, even if it was refused
        self.last_command_source = None  # "online", "local" or None
        self.last_command_at = None      # server time (ms) it was received, None when unknown
        self.wanted = {"fan": False, "heater": False}  # the newest request, from either panel
        self._latch_seen_off = False

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

    def apply(self, want_fan, want_heater, command_id=None, source=None):
        """Apply the wanted state from Firebase with the safety rules."""
        self.wanted = {"fan": bool(want_fan), "heater": bool(want_heater)}
        if command_id is not None:
            self.last_command_id, self.last_command_source = command_id, source
            clock = getattr(self.sync, "server_clock", None)
            now = clock.now_ms() if clock else None
            self.last_command_at = int(now) if now is not None else None
        if self.out.heater.latched is None:
            self._latch_seen_off = False
        elif not want_heater:
            self._latch_seen_off = True
        elif self._latch_seen_off:
            # the operator switched heating off and on again after the fault: that clears it
            self.out.heater.clear_latch()
            self.block = None
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
        hot = self.tm.hottest()  # the hottest healthy sensor, so a hot spot sensor 1 cannot see still counts
        hot = temp if hot is None else hot
        if hot >= self.tm.temp_max_c:  # the saved maximum, read locally, so it holds without the network
            return self._disable(f"heater off: over temperature ({hot:.1f} C)", "over_temperature")
        if not self.out.fan.is_active:
            return self._disable("heater off: fan is not running", "no_fan")
        if self.out.heater.latched:
            return self._disable(f"heater off: {self.out.heater.latched}", self.out.heater.latched)

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
    oven = None
    offsets = {}  # calibration applied to each sensor, for the start-up event
    if SIMULATE:
        from sim import SimOven
        oven = SimOven()
        oven.heater = out.heater
        zones = SensorArray({"bottom": Sensor(oven.read),
                             "middle": Sensor(lambda: oven.read_zone("middle")),
                             "top": Sensor(lambda: oven.read_zone("top"))})
        print("SIMULATE=1: mock relays and a simulated chamber, no hardware is touched")
    else:
        sensors = {}
        for zone in config.ZONES:
            sensor_id = config.ZONE_SENSOR_IDS[zone]
            offsets[zone] = calibration.zone_offset(sensor_id)  # always 0 for sensor 1, the reference
            sensors[zone] = Sensor(make_reader(sensor_id, offsets[zone]))
        zones = SensorArray(sensors, ids=config.ZONE_SENSOR_IDS)
    sensor = zones.control  # sensor 1 alone holds the thermostat and the heater interlock
    out.attach_sensor(sensor)
    boot_id = read_boot_id()

    def open_store():
        return TaskStore(config.DB_PATH, boot_id, clock_synced)

    store = open_store()
    prev_boot = store.get_meta("boot_id")
    clean = store.get_meta("clean_shutdown") == "1"
    store.set_meta(boot_id=boot_id, clean_shutdown=0)

    commands = queue.Queue()
    sync = None
    # readings recorded but not yet confirmed by Firebase; a number, so the control loop never queries for it
    temp_pending = Gauge(store.temp_history_pending_count())
    link = LinkMonitor()

    def on_task_start():
        # a stale manual command must not take over when the task ends
        sync.request_write("commands/heater", False)
        sync.request_write("commands/fan", False)

    tm = TaskManager(store, out, sensor, boot_id, clock_synced, on_task_start=on_task_start, zones=zones)

    def heater_block(ok):
        """The active safety condition keeping the heater off, or None (None removes the key)."""
        if not ok:
            return "sensor_fault"
        hot = tm.hottest()
        if (sensor.temp_c if hot is None else hot) >= tm.temp_max_c:
            return "over_temperature"
        if out.heater.latched:
            return out.heater.latched
        if tm.cutoff.active:
            return "cutoff"
        return manual.block

    def network_status():
        return read_network_status(config.NET_STATUS_PATH, config.NET_STATUS_MAX_AGE_SECONDS)

    def status():
        ok = sensor.ok()
        machine = tm.machine_status()
        net = network_status() or {}  # empty when netwatch is not running: the keys are then removed
        # a new task may start while cooling once the machine is cool enough to accept it
        hot = tm.hottest()
        cool_enough = ok and (sensor.temp_c if hot is None else hot) < tm.temp_max_c - config.TEMP_RESUME_DROP
        return {
            "fan": out.fan.is_active,
            "heater": out.heater.is_active,
            # temp and sensor_ok are the bottom sensor (sensor 1), as they always were
            "temp": round(sensor.temp_c, 1) if ok else None,  # None removes the key
            "sensor_ok": ok,
            **zone_state(zones),
            "temp_max": tm.temp_max_c,  # the maximum the machine enforces, not a target
            "status": machine,
            "cooling": tm.cooling_active(),
            "safe_temp": tm.safe_temp_c,
            "heater_cutoff": tm.heater_cutoff_c,
            "heater_cutoff_margin": config.HEATER_CUTOFF_MARGIN_C,
            "target_temp": tm.settings.get("manual_target_temp_c"),  # manual heating and the next task
            "target_humidity": tm.settings.get("manual_target_humidity"),
            "settings_updated_at": tm.settings.updated_at,
            "manual_heating": manual.enabled,
            "heater_block": heater_block(ok),
            "ready": (machine in ("ready", "cooling") and cool_enough and not out.heater.is_active
                      and zones.configured()),
            "command_id": manual.last_command_id,         # None removes the key
            "command_source": manual.last_command_source,
            "command_at": manual.last_command_at,
            "clock_synced": bool(clock_synced()),
            "temp_pending": temp_pending.value,
            "local_url": f"http://{ip}:{config.LOCAL_API_PORT}" if (ip := lan_ip()) else None,
            # which network the machine is on and why; only the Wi-Fi side of this ever reaches Firebase
            "network": net.get("network"),
            "network_since": int(net["since"] * 1000) if isinstance(net.get("since"), (int, float)) else None,
            "network_reason": net.get("reason"),
            "hotspot_connect_timeout_s": net.get("connect_timeout_s"),
            "hotspot_empty_grace_s": net.get("empty_grace_s"),
        }

    local_mark = LocalCommandMark()  # the operator at the machine outranks older requests from the cloud
    tm.local_mark = local_mark
    netctl = NetworkControl(now_ms=lambda: now_ms())  # now_ms is defined below, before it is first called
    sync = CloudSync(open_store, tm, status, commands, boot_id, local_mark=local_mark,
                     network_ack_fn=lambda: network_ack_payload(netctl.last_ack), link=link)
    # The temperature history has its own threads and its own Firebase round trips: a backlog, a timeout or a
    # trim must never delay a command or the control loop.
    uploader = TempUploader(open_store, firebase_base, link, temp_pending)
    link.on_recovered(uploader.recovered)
    recorder = TempRecorder(open_store, zones, clock_synced, temp_pending, wake=uploader.wake)
    snapshot = {"value": None}   # swapped whole by the control loop, read by the HTTP thread
    task_request = {"value": None}  # newest task request seen from either panel
    last_tick = {"at": time.monotonic()}

    def now_ms():
        """Server time when known, otherwise the Pi clock (which may be wrong until NTP syncs)."""
        server = sync.server_clock.now_ms()
        return int(server) if server is not None else int(time.time() * 1000)

    def publish_snapshot():
        snapshot["value"] = build_snapshot(
            state=status(), manual=dict(manual.wanted), settings=tm.settings, request=task_request["value"],
            report=tm.snapshot(), ack=store.last_command(), pi_now_ms=now_ms(),
            cloud_online=sync.online, loop_age_s=time.monotonic() - last_tick["at"],
            wifi=network_status(), network_ack=network_ack_payload(netctl.last_ack), link=link.snapshot())
    manual = ManualControl(out, sensor, store, sync, tm)
    tm.server_now_ms = sync.server_clock.now_ms  # command age is judged on server time, not the Pi clock

    # get a first reading before the recovery check starts
    for s in zones.sensors.values():
        s.poll()
    store.log_event("device_started", "device started", data={
        "boot_changed": prev_boot != boot_id, "previous_boot_id": prev_boot,
        "clean_shutdown": clean, "clock_synced": clock_synced(),
        "sensor_offsets_c": offsets, "zones_unconfigured": zones.unconfigured(),
    })
    tm.startup(prev_boot, clean)

    def read_sensor(s):
        # the 1-Wire read takes about a second, so it must not hold up command handling. One thread per
        # sensor: read one after another the three would take sensor 1 past DWELL_MAX_GAP_SECONDS, and a
        # slow or dead upper sensor must never delay the one that holds the heater thermostat.
        while not stop.wait(config.SENSOR_INTERVAL):
            s.poll()

    for zone, s in zones.sensors.items():
        threading.Thread(target=read_sensor, args=(s,), daemon=True, name=f"sensor-{zone}").start()
    threading.Thread(target=sync.run, args=(stop,), daemon=True, name="cloud-sync").start()
    threading.Thread(target=recorder.run, args=(stop,), daemon=True, name="temp-recorder").start()
    threading.Thread(target=uploader.run, args=(stop,), daemon=True, name="temp-uploader").start()
    api = LocalApi(get_snapshot=lambda: snapshot["value"] and dict(
        snapshot["value"], loop_age_s=round(time.monotonic() - last_tick["at"], 2)),
        commands=commands, store=store, local_mark=local_mark)
    if api.start():
        print(f"Local panel on port {api.port}")
    else:
        store.log_event("local_api_unavailable", f"local panel could not listen on port {api.port}")
    print("Running. Control it from Firebase or the local panel.")
    sd_notify.ready()

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
                    task_request["value"] = payload
                    ack = tm.handle_command(payload, cloud=True)
                    if ack:
                        print("command:", ack)
                elif kind == "task_local":
                    # stamped here so the request is aged on the same clock as one from Firebase
                    payload = dict(payload, requested_at=now_ms())
                    task_request["value"] = payload
                    ack = tm.handle_command(payload)
                    if ack:
                        print("command:", ack)
                elif kind in ("network", "network_local"):
                    # only the request file changes: the relays and the drying task are not touched
                    request, verdict = (payload["request"], payload["verdict"]) if kind == "network" else (payload, None)
                    ack = netctl.handle(request, verdict)
                    if ack:
                        print("network:", ack)
                        store.log_event("network_request", f"switch to {ack['mode']} {ack['result']}",
                                        data={"command_id": ack["command_id"], "reason": ack["reason"],
                                              "source": "online" if kind == "network" else "local"})
                elif kind == "settings":
                    tm.apply_settings(payload)
                elif kind == "settings_local":
                    # newer than anything applied, so an older write still queued in Firebase cannot undo it
                    tm.apply_settings(dict(payload, updated_at=max(tm.settings.updated_at + 1, now_ms())))
                else:
                    manual.apply(*payload)

            now = time.monotonic()
            if now >= next_tick:
                sd_notify.watchdog()  # only a loop that is still ticking keeps systemd satisfied
                last_tick["at"] = time.monotonic()
                latched = out.heater.check()
                if latched:
                    store.log_event("heater_guard", f"heater latched off: {latched}")
                tm.tick()
                manual.tick()
                next_tick = now + config.LOOP_INTERVAL
                publish_snapshot()

            # report a confirmed change at once (commands, thermostat, cooling end, failsafe)
            confirmed = (out.fan.is_active, out.heater.is_active, tm.machine_status())
            if items or confirmed != last_published:
                last_published = confirmed
                sync.notify()
    finally:
        api.stop()
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
