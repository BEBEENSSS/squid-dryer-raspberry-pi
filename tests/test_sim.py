import config
from sim import SimOven


class FakeHeater:
    is_active = False


def test_oven_warms_with_heater_and_cools_without():
    t = [0.0]
    oven = SimOven(mono=lambda: t[0])
    oven.heater = FakeHeater()
    oven.heater.is_active = True
    for _ in range(600):
        t[0] += 1
        warm = oven.read()
    assert warm > 40  # equilibrium is ambient + heat/loss = 75 C
    oven.heater.is_active = False
    for _ in range(600):
        t[0] += 1
        cool = oven.read()
    assert cool < warm


def test_broken_sensor_reads_none():
    oven = SimOven(mono=lambda: 0.0)
    oven.broken = True
    assert oven.read() is None
    assert config.SENSOR_ID  # config still imports without hardware
