"""database.rules.json and config.py state the same ranges. If one is edited without the other, a value the Pi accepts
can be refused by Firebase (or the reverse) and nobody notices until a save fails on the dashboard."""
import json
import re
from pathlib import Path

import config
import machine_settings

RULES = Path(__file__).resolve().parent.parent / "database.rules.json"
RANGE = re.compile(r">= *(-?[0-9.]+) *&& *newData\.val\(\) *<= *(-?[0-9.]+)")


def settings_rules():
    return json.loads(RULES.read_text(encoding="utf-8"))["rules"]["devices"]["$deviceId"]["settings"]


def test_every_setting_range_in_the_rules_matches_config():
    rules = settings_rules()
    for key, (_, _, lo, hi) in machine_settings.FIELDS.items():
        match = RANGE.search(rules[key][".validate"])
        assert match, f"{key} has no numeric range in the rules"
        assert (float(match[1]), float(match[2])) == (float(getattr(config, lo)), float(getattr(config, hi))), key


def test_every_setting_can_be_marked_as_changed():
    rules = settings_rules()
    assert set(machine_settings.FIELDS) <= set(rules["changed_at"])
