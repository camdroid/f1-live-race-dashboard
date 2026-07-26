"""Tests for the --delay feature: parse_delay, DelayedRouter, client wiring."""

import time

import pytest

from live_opw_bridge import DelayedRouter, LiveF1Client, LiveState, parse_delay


# ---------------------------------------------------------------------------
# parse_delay
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    ("90", 90.0),
    ("90s", 90.0),
    ("30m", 1800.0),
    ("1.5h", 5400.0),
    ("0", 0.0),
    (" 45 s ", 45.0),
    ("2H", 7200.0),
])
def test_parse_delay_valid(value, expected):
    assert parse_delay(value) == expected


@pytest.mark.parametrize("value", ["abc", "", "-30", "30x", "m30", "1:30"])
def test_parse_delay_invalid(value):
    with pytest.raises(Exception):
        parse_delay(value)


# ---------------------------------------------------------------------------
# DelayedRouter
# ---------------------------------------------------------------------------
TIMING_DATA = {"Lines": {"1": {"Position": "1"}}}
DRIVER_LIST = {"1": {"Tla": "VER"}}


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_messages_are_held_back_for_the_delay():
    state = LiveState()
    router = DelayedRouter(state, delay=0.4)
    try:
        router.route("TimingData", TIMING_DATA)
        time.sleep(0.1)
        assert state.positions == {}, "message must not arrive before the delay"
        assert _wait_until(lambda: state.positions == {"1": 1})
    finally:
        router.stop()


def test_metadata_topics_bypass_the_delay():
    state = LiveState()
    router = DelayedRouter(state, delay=60.0)
    try:
        router.route("DriverList", DRIVER_LIST)
        assert state.driver_info["1"]["Tla"] == "VER", (
            "DriverList must be routed immediately, not delayed"
        )
    finally:
        router.stop()


def test_messages_keep_their_order():
    state = LiveState()
    router = DelayedRouter(state, delay=0.2)
    try:
        router.route("TimingData", {"Lines": {"1": {"Position": "5"}}})
        router.route("TimingData", {"Lines": {"1": {"Position": "3"}}})
        assert _wait_until(lambda: state.positions.get("1") == 3)
    finally:
        router.stop()


def test_stop_prevents_pending_delivery():
    state = LiveState()
    router = DelayedRouter(state, delay=0.3)
    router.route("TimingData", TIMING_DATA)
    router.stop()
    time.sleep(0.6)
    assert state.positions == {}


# ---------------------------------------------------------------------------
# LiveF1Client wiring (feed messages only — never connects)
# ---------------------------------------------------------------------------
def test_client_with_delay_buffers_feed_messages(tmp_path):
    record_file = tmp_path / "rec.txt"
    state = LiveState()
    client = LiveF1Client(state, delay=0.4)
    client._output_file = open(record_file, "w")
    try:
        client._on_message(["TimingData", TIMING_DATA])
        client._output_file.flush()
        # Recording happens in real time, before the delayed state update.
        assert "TimingData" in record_file.read_text()
        assert state.positions == {}
        assert _wait_until(lambda: state.positions == {"1": 1})
    finally:
        client.stop()


def test_client_without_delay_routes_immediately():
    state = LiveState()
    client = LiveF1Client(state)
    client._on_message(["TimingData", TIMING_DATA])
    assert state.positions == {"1": 1}
