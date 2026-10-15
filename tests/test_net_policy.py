from net_policy import (BOOT_WAIT, HOTSPOT, NONE, PROBE_WIFI, PROBING, START_HOTSPOT, WIFI, WIFI_LOST,
                        NetPolicy)

GRACE, RETRY, DROP = 180, 120, 60
CONNECT, EMPTY = 300, 60  # a requested hotspot: wait for a phone, then for the last phone to leave


def policy(uptime=0, log=None):
    return NetPolicy(0.0, uptime, boot_grace=GRACE, retry_interval=RETRY, drop_grace=DROP,
                     connect_timeout=CONNECT, empty_grace=EMPTY, log=log)


def step(p, t, wifi=False, hotspot=False, clients=0):
    return p.step(t, wifi_up=wifi, hotspot_up=hotspot, clients=clients)


def raise_hotspot(p, t=GRACE):
    """Drive a fresh policy to the point where the hotspot is up and empty."""
    assert step(p, t) == START_HOTSPOT
    assert step(p, t + 1, hotspot=True) == NONE
    return t + 1


def test_router_that_boots_slower_than_the_pi_never_triggers_the_hotspot():
    p = policy()
    for t in range(0, 150, 5):
        assert step(p, t) == NONE
    assert step(p, 150, wifi=True) == NONE
    assert p.state == WIFI
    for t in range(155, 2000, 5):
        assert step(p, t, wifi=True) == NONE


def test_router_off_brings_the_hotspot_up_after_the_boot_grace_period():
    p = policy()
    assert step(p, GRACE - 1) == NONE
    assert p.state == BOOT_WAIT
    assert step(p, GRACE) == START_HOTSPOT
    assert p.state == HOTSPOT


def test_a_restart_long_after_boot_does_not_wait_the_grace_period_again():
    p = policy(uptime=10_000)
    assert step(p, 0) == START_HOTSPOT


def test_hotspot_that_failed_to_start_is_requested_again_shortly_after():
    p = policy()
    assert step(p, GRACE) == START_HOTSPOT
    assert step(p, GRACE + 5) == NONE
    assert step(p, GRACE + 10) == START_HOTSPOT


def test_a_connected_phone_is_never_kicked_off_however_long_it_stays():
    p = policy()
    t = raise_hotspot(p)
    for t in range(t, t + 3600, 5):
        assert step(p, t, hotspot=True, clients=1) == NONE
    assert p.state == HOTSPOT


def test_unreadable_client_count_counts_as_someone_connected():
    p = policy()
    t = raise_hotspot(p)
    for t in range(t, t + 1000, 5):
        assert step(p, t, hotspot=True, clients=None) == NONE


def test_empty_hotspot_retries_the_home_wifi_a_full_interval_after_the_last_phone_left():
    p = policy()
    t = raise_hotspot(p)
    assert step(p, t + 300, hotspot=True, clients=1) == NONE  # a phone was there at t + 300
    assert step(p, t + 300 + RETRY - 1, hotspot=True, clients=0) == NONE
    assert step(p, t + 300 + RETRY, hotspot=True, clients=0) == PROBE_WIFI
    assert p.state == PROBING


def test_nothing_happens_while_a_probe_is_running():
    p = policy()
    t = raise_hotspot(p)
    assert step(p, t + RETRY, hotspot=True) == PROBE_WIFI
    assert step(p, t + RETRY + 1) == NONE


def test_failed_probe_brings_the_hotspot_back_and_waits_a_full_interval_to_try_again():
    p = policy()
    t = raise_hotspot(p)
    probe_at = t + RETRY
    assert step(p, probe_at, hotspot=True) == PROBE_WIFI
    p.probe_done(probe_at + 40, joined=False)
    assert step(p, probe_at + 41, hotspot=False) == START_HOTSPOT
    assert step(p, probe_at + 42, hotspot=True) == NONE
    assert step(p, probe_at + 40 + RETRY - 1, hotspot=True) == NONE
    assert step(p, probe_at + 40 + RETRY, hotspot=True) == PROBE_WIFI


def test_successful_probe_ends_on_wifi():
    p = policy()
    t = raise_hotspot(p)
    assert step(p, t + RETRY, hotspot=True) == PROBE_WIFI
    p.probe_done(t + RETRY + 10, joined=True)
    assert p.state == WIFI
    assert step(p, t + RETRY + 11, wifi=True) == NONE


def test_wifi_without_internet_stays_on_wifi():
    # internet is not an input of the policy at all, so a LAN with a dead uplink looks like any other
    p = policy()
    step(p, 5, wifi=True)
    for t in range(10, 5000, 5):
        assert step(p, t, wifi=True) == NONE
    assert p.state == WIFI


def test_dropped_wifi_gets_a_grace_period_then_one_explicit_try_then_the_hotspot():
    p = policy()
    step(p, 5, wifi=True)
    assert step(p, 1000, wifi=False) == NONE
    assert p.state == WIFI_LOST
    assert step(p, 1000 + DROP - 1) == NONE
    assert step(p, 1000 + DROP) == PROBE_WIFI
    p.probe_done(1000 + DROP + 40, joined=False)
    assert step(p, 1000 + DROP + 41) == START_HOTSPOT
    assert p.state == HOTSPOT


def test_wifi_that_comes_back_by_itself_cancels_the_fallback():
    p = policy()
    step(p, 5, wifi=True)
    step(p, 1000, wifi=False)
    assert step(p, 1030, wifi=True) == NONE
    assert p.state == WIFI


def test_joining_wifi_while_the_hotspot_is_nominally_up_returns_to_wifi():
    p = policy()
    raise_hotspot(p)
    assert step(p, 500, wifi=True) == NONE
    assert p.state == WIFI


def test_every_transition_is_logged_once():
    lines = []
    p = policy(log=lines.append)
    step(p, 5, wifi=True)
    step(p, 6, wifi=True)
    step(p, 100, wifi=False)
    assert len(lines) == 2
    assert "boot_wait -> wifi" in lines[0]
    assert "wifi -> wifi_lost" in lines[1]


# ---------------- a hotspot an operator asked for ----------------

def requested_hotspot(t=1000):
    """On Wi-Fi, then asked for the hotspot at t, which comes up a moment later."""
    p = policy()
    step(p, 5, wifi=True)
    assert p.request_hotspot(t) == START_HOTSPOT
    assert p.state == HOTSPOT and p.requested and p.reason == "requested"
    assert step(p, t + 1, hotspot=True) == NONE
    return p, t


def test_requested_hotspot_gives_up_when_nobody_joins_within_the_connect_timeout():
    p, t = requested_hotspot()
    assert step(p, t + CONNECT - 1, hotspot=True) == NONE
    assert step(p, t + CONNECT, hotspot=True) == PROBE_WIFI
    assert p.reason == "requested" and p.state == PROBING
    p.probe_done(t + CONNECT + 10, joined=True)
    assert p.state == WIFI and p.reason == "no_client_timeout"


def test_requested_hotspot_stays_up_for_as_long_as_a_phone_is_connected():
    p, t = requested_hotspot()
    for now in range(t + 2, t + 7200, 5):
        assert step(p, now, hotspot=True, clients=1) == NONE
    assert p.state == HOTSPOT


def test_requested_hotspot_returns_to_wifi_a_short_grace_after_the_last_phone_left():
    p, t = requested_hotspot()
    assert step(p, t + 100, hotspot=True, clients=1) == NONE
    assert step(p, t + 100 + EMPTY - 1, hotspot=True, clients=0) == NONE
    assert step(p, t + 100 + EMPTY, hotspot=True, clients=0) == PROBE_WIFI
    p.probe_done(t + 100 + EMPTY + 10, joined=True)
    assert p.state == WIFI and p.reason == "clients_left"


def test_a_phone_that_comes_back_inside_the_grace_keeps_the_requested_hotspot():
    p, t = requested_hotspot()
    step(p, t + 10, hotspot=True, clients=1)
    assert step(p, t + 10 + EMPTY - 5, hotspot=True, clients=0) == NONE
    assert step(p, t + 10 + EMPTY - 1, hotspot=True, clients=1) == NONE
    assert step(p, t + 10 + 2 * EMPTY - 2, hotspot=True, clients=0) == NONE
    assert p.state == HOTSPOT


def test_unreadable_client_count_keeps_a_requested_hotspot_up():
    p, t = requested_hotspot()
    for now in range(t + 2, t + 3 * CONNECT, 5):
        assert step(p, now, hotspot=True, clients=None) == NONE


def test_failed_return_to_wifi_leaves_the_fallback_hotspot_and_retries_on_the_slow_interval():
    p, t = requested_hotspot()
    assert step(p, t + CONNECT, hotspot=True) == PROBE_WIFI
    p.probe_done(t + CONNECT + 40, joined=False)
    assert p.state == HOTSPOT and not p.requested and p.reason == "fallback"
    assert step(p, t + CONNECT + 41) == START_HOTSPOT
    assert step(p, t + CONNECT + 42, hotspot=True) == NONE
    # the short requested-hotspot timers no longer apply: a full retry interval is waited out
    assert step(p, t + CONNECT + 40 + EMPTY + 1, hotspot=True) == NONE
    assert step(p, t + CONNECT + 40 + RETRY, hotspot=True) == PROBE_WIFI


def test_requested_hotspot_that_never_comes_up_gives_up_too():
    p = policy()
    step(p, 5, wifi=True)
    assert p.request_hotspot(1000) == START_HOTSPOT
    assert step(p, 1005, hotspot=False) == NONE
    assert step(p, 1010, hotspot=False) == START_HOTSPOT  # asked again every 10 s
    assert step(p, 1000 + CONNECT, hotspot=False) == PROBE_WIFI


def test_wifi_still_connected_does_not_cancel_a_requested_hotspot_that_has_not_started():
    p = policy()
    step(p, 5, wifi=True)
    p.request_hotspot(1000)
    assert step(p, 1010, wifi=True) == START_HOTSPOT
    assert p.state == HOTSPOT and p.requested


def test_switching_back_to_wifi_on_request_probes_at_once_even_with_a_phone_connected():
    p, t = requested_hotspot()
    step(p, t + 10, hotspot=True, clients=2)
    assert p.request_wifi(t + 11) == PROBE_WIFI
    assert p.state == PROBING
    p.probe_done(t + 30, joined=True)
    assert p.state == WIFI and p.reason == "switched_back"


def test_asking_for_wifi_while_already_on_wifi_does_nothing():
    p = policy()
    step(p, 5, wifi=True)
    assert p.request_wifi(10) == NONE
    assert p.state == WIFI


def test_asking_for_the_hotspot_while_the_fallback_hotspot_is_up_only_changes_the_rules():
    p = policy()
    t = raise_hotspot(p)
    assert p.request_hotspot(t + 10) == NONE  # the radio is left alone, no phone is kicked
    assert p.requested
    assert step(p, t + 10 + CONNECT - 1, hotspot=True) == NONE
    assert step(p, t + 10 + CONNECT, hotspot=True) == PROBE_WIFI


def test_the_reason_explains_how_a_wifi_connection_came_about():
    p = policy()
    assert p.reason == "boot"
    step(p, 5, wifi=True)
    assert p.reason == "boot"
    step(p, 100, wifi=False)
    step(p, 100 + DROP)
    p.probe_done(200, joined=True)
    assert p.reason == "wifi_back"
