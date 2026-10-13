from gpiozero import OutputDevice
from time import sleep

fan = OutputDevice(17, active_high=False, initial_value=False)
heater = OutputDevice(27, active_high=False, initial_value=False)

try:
    print("Fan ON");    fan.on();    sleep(2)
    print("Heater ON"); heater.on(); sleep(2)
    print("Heater OFF"); heater.off(); sleep(1)
    print("Fan OFF");   fan.off()
finally:
    heater.off()
    fan.off()
    print("All off")