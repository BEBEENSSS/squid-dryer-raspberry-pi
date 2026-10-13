import threading
from gpiozero import OutputDevice

FAN_PIN = 17
HEATER_PIN = 27
HEATER_MAX_SECONDS = 600  # heater auto-off after 10 minutes

fan = OutputDevice(FAN_PIN, active_high=False, initial_value=False)
heater = OutputDevice(HEATER_PIN, active_high=False, initial_value=False)
heater_timer = None

def heater_off():
    global heater_timer
    if heater_timer:
        heater_timer.cancel()
        heater_timer = None
    heater.off()

def auto_off():
    heater.off()
    print("\nHeater auto-off: max runtime reached")

def heater_on():
    global heater_timer
    if not fan.is_active:
        print("Refused: turn the fan on first")
        return
    heater.on()
    if heater_timer:
        heater_timer.cancel()
    heater_timer = threading.Timer(HEATER_MAX_SECONDS, auto_off)
    heater_timer.daemon = True
    heater_timer.start()

def status():
    print(f"fan={'ON' if fan.is_active else 'off'} | "
          f"heater={'ON' if heater.is_active else 'off'}")

HELP = "Commands: fan on | fan off | heater on | heater off | status | quit"
print(HELP)

try:
    while True:
        cmd = input("> ").strip().lower()
        if cmd == "fan on":
            fan.on()
        elif cmd == "fan off":
            heater_off()   # never leave the heater on without airflow
            fan.off()
        elif cmd == "heater on":
            heater_on()
        elif cmd == "heater off":
            heater_off()
        elif cmd == "status":
            pass
        elif cmd in ("quit", "exit"):
            break
        else:
            print(HELP)
            continue
        status()
except (KeyboardInterrupt, EOFError):
    pass
finally:
    heater_off()
    fan.off()
    print("All off")