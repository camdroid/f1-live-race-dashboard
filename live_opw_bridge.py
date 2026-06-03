"""
Live OPW Bridge — streams the F1 live timing feed to Open Pit Wall dashboards.

Connects to the F1 SignalR live timing feed, decodes CarData.z and Position.z
in real-time, and re-broadcasts the data in Open Pit Wall's WebSocket protocol
so that OPW dashboards can be used with a live race.

Usage:
    .venv/bin/python live_opw_bridge.py [--port 8765] [--no-auth] [--record FILE]

Then point any OPW dashboard at ws://localhost:8765 as normal.

Run the example dashboard from open-pit-wall:
    cd ~/projects/open-pit-wall/examples/driver-telemetry-trace
    python3 main.py --driver VER --host 127.0.0.1 --port 8765
"""

import argparse
import asyncio
import base64
import json
import logging
import threading
import time
import zlib
from datetime import datetime, timezone
from typing import Any

import requests
import websockets
from signalrcore.hub_connection_builder import HubConnectionBuilder
from signalrcore.messages.completion_message import CompletionMessage

import fastf1
from fastf1.internals.f1auth import get_auth_token


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("opw-bridge")

# Coordinates from 2020+ are in 1/10 metre
POSITION_SCALE = 0.1

# CarData channel indices
CH_RPM = "0"
CH_SPEED = "2"
CH_GEAR = "3"
CH_THROTTLE = "4"
CH_BRAKE = "5"
CH_DRS = "45"

TYRE_COMPOUND_MAP = {
    "SOFT": "SOFT", "MEDIUM": "MEDIUM", "HARD": "HARD",
    "INTERMEDIATE": "INTER", "INTER": "INTER",
    "WET": "WET", "TEST_UNKNOWN": "UNKNOWN", "UNKNOWN": "UNKNOWN",
}

OPW_CHANNELS = {
    "telemetry.drivers": "Broadcast per-driver telemetry for each frame.",
    "leaderboard": "Broadcast leaderboard telemetry for each frame.",
    "race_control": "Broadcast merged race control and track status messages.",
    "telemetry.weather": "Broadcast weather snapshots when present on a frame.",
    "telemetry.lap": "Broadcast the leader lap and replay timestamp for each frame.",
}


# ---------------------------------------------------------------------------
# Shared live state
# ---------------------------------------------------------------------------
class LiveState:
    def __init__(self):
        self._lock = threading.Lock()

        # driver_number (str) -> latest telemetry dict
        self.telemetry: dict[str, dict] = {}

        # driver_number -> {"Tla": "VER", "TeamColour": "..."}
        self.driver_info: dict[str, dict] = {}

        # driver_number -> {"Compound": "SOFT", "TotalLaps": 5}
        self.stints: dict[str, dict] = {}

        # driver_number -> position in race (int)
        self.positions: dict[str, int] = {}

        # session-level info
        self.session_info: dict = {}
        self.lap_count: dict = {}
        self.weather: dict = {}
        self.race_control: list[dict] = []

        self.session_start_utc: str = datetime.now(timezone.utc).isoformat()

    def apply_car_data(self, payload: str | dict):
        """Decode and merge CarData.z payload into per-driver telemetry."""
        data = _decode_compressed(payload) if isinstance(payload, str) else payload
        if not data:
            return
        with self._lock:
            for entry in data.get("Entries", []):
                for drv, car in entry.get("Cars", {}).items():
                    channels = car.get("Channels", {})
                    t = self.telemetry.setdefault(drv, {})
                    if CH_RPM in channels:
                        t["rpm"] = int(channels[CH_RPM])
                    if CH_SPEED in channels:
                        t["speed"] = float(channels[CH_SPEED])
                    if CH_GEAR in channels:
                        t["gear"] = int(channels[CH_GEAR])
                    if CH_THROTTLE in channels:
                        t["throttle"] = float(channels[CH_THROTTLE])
                    if CH_BRAKE in channels:
                        t["brake"] = float(channels[CH_BRAKE]) * 100
                    if CH_DRS in channels:
                        t["drs_status"] = int(channels[CH_DRS])

    def apply_position_data(self, payload: str | dict):
        """Decode and merge Position.z payload into per-driver telemetry."""
        data = _decode_compressed(payload) if isinstance(payload, str) else payload
        if not data:
            return
        with self._lock:
            for sample in data.get("Position", []):
                for drv, pos in sample.get("Entries", {}).items():
                    t = self.telemetry.setdefault(drv, {})
                    x = pos.get("X")
                    y = pos.get("Y")
                    z = pos.get("Z")
                    if x is not None:
                        t["x"] = float(x) * POSITION_SCALE
                    if y is not None:
                        t["y"] = float(y) * POSITION_SCALE
                    if z is not None:
                        t["z"] = float(z) * POSITION_SCALE

    def apply_driver_list(self, data: dict):
        with self._lock:
            for num, info in data.items():
                if isinstance(info, dict):
                    self.driver_info.setdefault(num, {}).update(info)

    def apply_timing_data(self, data: dict):
        with self._lock:
            for num, t in data.get("Lines", {}).items():
                if isinstance(t, dict):
                    pos = t.get("Position")
                    if pos:
                        self.positions[num] = int(pos)

    def apply_timing_app(self, data: dict):
        with self._lock:
            for num, info in data.get("Lines", {}).items():
                stints = info.get("Stints", {})
                if stints:
                    latest = stints[max(stints.keys(), key=int)]
                    self.stints[num] = latest

    def apply_weather(self, data: dict):
        with self._lock:
            self.weather.update(data)

    def apply_session_info(self, data: dict):
        with self._lock:
            self.session_info.update(data)

    def apply_lap_count(self, data: dict):
        with self._lock:
            self.lap_count.update(data)

    def apply_race_control(self, data: dict):
        with self._lock:
            for msg in data.get("Messages", {}).values():
                if isinstance(msg, dict):
                    self.race_control.append(msg)
                    if len(self.race_control) > 50:
                        self.race_control.pop(0)

    def snapshot(self) -> dict:
        """Return a point-in-time copy of all state."""
        with self._lock:
            return {
                "telemetry": {k: dict(v) for k, v in self.telemetry.items()},
                "driver_info": {k: dict(v) for k, v in self.driver_info.items()},
                "stints": {k: dict(v) for k, v in self.stints.items()},
                "positions": dict(self.positions),
                "session_info": dict(self.session_info),
                "lap_count": dict(self.lap_count),
                "weather": dict(self.weather),
                "race_control": list(self.race_control),
            }


def _decode_compressed(text: str) -> dict | None:
    """Decode a zlib+base64 compressed F1 timing payload."""
    try:
        if text and text[0] == "{":
            return json.loads(text)
        text = text.strip('"')
        raw = zlib.decompress(base64.b64decode(text), -zlib.MAX_WBITS)
        return json.loads(raw.decode("utf-8-sig"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# F1 SignalR client
# ---------------------------------------------------------------------------
class LiveF1Client:
    _connection_url = "wss://livetiming.formula1.com/signalrcore"
    _negotiate_url = "https://livetiming.formula1.com/signalrcore/negotiate"

    _topics = [
        "Heartbeat", "DriverList", "ExtrapolatedClock", "RaceControlMessages",
        "SessionInfo", "SessionStatus", "TimingAppData", "TimingData",
        "TrackStatus", "WeatherData", "LapCount",
        "CarData.z", "Position.z",
    ]

    def __init__(self, state: LiveState, record_file: str | None = None,
                 no_auth: bool = False):
        self.state = state
        self.record_file = record_file
        self.no_auth = no_auth
        self._connection = None
        self._output_file = None
        self._connected = False

    def _parse_message(self, topic: str, raw_data):
        """Route a decoded message to the appropriate state handler."""
        if topic == "CarData.z":
            self.state.apply_car_data(raw_data)
        elif topic == "Position.z":
            self.state.apply_position_data(raw_data)
        elif topic == "DriverList":
            self.state.apply_driver_list(raw_data if isinstance(raw_data, dict) else {})
        elif topic == "TimingData":
            self.state.apply_timing_data(raw_data if isinstance(raw_data, dict) else {})
        elif topic == "TimingAppData":
            self.state.apply_timing_app(raw_data if isinstance(raw_data, dict) else {})
        elif topic == "WeatherData":
            self.state.apply_weather(raw_data if isinstance(raw_data, dict) else {})
        elif topic == "SessionInfo":
            self.state.apply_session_info(raw_data if isinstance(raw_data, dict) else {})
        elif topic == "LapCount":
            self.state.apply_lap_count(raw_data if isinstance(raw_data, dict) else {})
        elif topic == "RaceControlMessages":
            self.state.apply_race_control(raw_data if isinstance(raw_data, dict) else {})

    def _on_message(self, msg):
        if self._output_file:
            self._output_file.write(str(msg) + "\n")
            self._output_file.flush()

        if isinstance(msg, CompletionMessage):
            for topic, data in (msg.result or {}).items():
                if isinstance(data, str):
                    data = _decode_compressed(data) or data
                self._parse_message(topic, data)

        elif isinstance(msg, list) and len(msg) >= 2:
            topic = msg[0]
            raw = msg[1]
            # CarData.z and Position.z arrive as compressed strings
            if topic in ("CarData.z", "Position.z"):
                self._parse_message(topic, raw)
            else:
                if isinstance(raw, str):
                    try:
                        raw = json.loads(
                            raw.replace("'", '"')
                               .replace('True', 'true')
                               .replace('False', 'false')
                        )
                    except (json.JSONDecodeError, ValueError):
                        pass
                self._parse_message(topic, raw)

    def start(self):
        if self.record_file:
            self._output_file = open(self.record_file, "w")

        headers = {}
        r = requests.options(self._negotiate_url, headers=headers)
        if "AWSALBCORS" in r.cookies:
            headers["Cookie"] = f"AWSALBCORS={r.cookies['AWSALBCORS']}"

        options = {
            "verify_ssl": True,
            "access_token_factory": None if self.no_auth else get_auth_token,
            "headers": headers,
        }

        self._connection = (
            HubConnectionBuilder()
            .with_url(self._connection_url, options=options)
            .build()
        )
        self._connection.on_open(self._on_open)
        self._connection.on_close(self._on_close)
        self._connection.on("feed", self._on_message)
        self._connection.start()

    def _on_open(self):
        self._connected = True
        log.info("Connected to F1 live timing feed")
        self._connection.send(
            "Subscribe", [self._topics], on_invocation=self._on_message
        )

    def _on_close(self):
        self._connected = False
        log.warning("Disconnected from F1 live timing feed")

    def stop(self):
        if self._connection:
            self._connection.stop()
        if self._output_file:
            self._output_file.close()


# ---------------------------------------------------------------------------
# OPW WebSocket server
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _build_driver_payload(num: str, snap: dict) -> dict | None:
    t = snap["telemetry"].get(num, {})
    info = snap["driver_info"].get(num, {})
    stint = snap["stints"].get(num, {})

    # Need at minimum an x/y position to be meaningful
    if "x" not in t and "y" not in t:
        return None

    compound = TYRE_COMPOUND_MAP.get(
        str(stint.get("Compound", "")).upper(), "UNKNOWN"
    )

    return {
        "driver_code": info.get("Tla", num),
        "current_lap_time": 0.0,
        "speed": round(t.get("speed", 0.0), 3),
        "rpm": int(t.get("rpm", 0)),
        "throttle": round(t.get("throttle", 0.0), 3),
        "brake": round(t.get("brake", 0.0), 3),
        "gear": int(t.get("gear", 0)),
        "drs_status": int(t.get("drs_status", 0)),
        "current_tyre": compound,
        "position": {
            "x": round(t.get("x", 0.0), 3),
            "y": round(t.get("y", 0.0), 3),
            "z": round(t.get("z", 0.0), 3),
            "dist_metres_around_track": 0.0,
            "dist_percentage_around_track": 0.0,
        },
    }


def _build_leaderboard(snap: dict) -> list[dict]:
    positions = snap["positions"]
    driver_info = snap["driver_info"]
    sorted_drivers = sorted(positions.items(), key=lambda kv: kv[1])
    return [
        {
            "position": pos,
            "driver_code": driver_info.get(num, {}).get("Tla", num),
            "dist_metres_from_leader": 0.0,
        }
        for num, pos in sorted_drivers
    ]


def _build_weather(snap: dict) -> dict | None:
    w = snap["weather"]
    if not w:
        return None
    return {
        "track_temp": float(w.get("TrackTemp", 0) or 0),
        "air_temp": float(w.get("AirTemp", 0) or 0),
        "humidity": float(w.get("Humidity", 0) or 0),
        "wind_speed": float(w.get("WindSpeed", 0) or 0),
        "wind_direction": float(w.get("WindDirection", 0) or 0),
        "rain_state": "WET" if w.get("Rainfall") not in ("0", 0, False, "False", "") else "DRY",
    }


def _build_frames(snap: dict) -> dict[str, Any]:
    """Build one set of OPW channel messages from a state snapshot."""
    timestamp = _now_iso()
    current_lap = snap["lap_count"].get("CurrentLap", 0)

    driver_payloads = []
    for num in snap["telemetry"]:
        payload = _build_driver_payload(num, snap)
        if payload:
            driver_payloads.append(payload)

    frames: dict[str, Any] = {}

    if driver_payloads:
        frames["telemetry.drivers"] = {
            "timestamp": timestamp,
            "event": "telemetry.drivers",
            "payload": driver_payloads,
        }
        for p in driver_payloads:
            frames[f"telemetry.drivers.{p['driver_code']}"] = {
                "timestamp": timestamp,
                "event": f"telemetry.drivers.{p['driver_code']}",
                "payload": p,
            }

    leaderboard = _build_leaderboard(snap)
    if leaderboard:
        frames["leaderboard"] = {
            "timestamp": timestamp,
            "event": "leaderboard",
            "payload": {"drivers": leaderboard},
        }

    frames["telemetry.lap"] = {
        "timestamp": timestamp,
        "event": "telemetry.lap",
        "payload": {
            "current_lap": current_lap,
            "elapsed_seconds": 0.0,
        },
    }

    weather = _build_weather(snap)
    if weather:
        frames["telemetry.weather"] = {
            "timestamp": timestamp,
            "event": "telemetry.weather",
            "payload": weather,
        }

    # Race control — emit each unsent message once
    if snap["race_control"]:
        last = snap["race_control"][-1]
        frames["race_control"] = {
            "timestamp": timestamp,
            "event": "race_control",
            "payload": {
                "message": last.get("Message", ""),
                "flag": last.get("Flag", ""),
                "scope": last.get("Scope", "Track"),
                "sector": last.get("RacingNumber", 0),
                "current_lap": current_lap,
            },
        }

    return frames


class OPWServer:
    def __init__(self, state: LiveState, host: str = "localhost", port: int = 8765):
        self.state = state
        self.host = host
        self.port = port
        # websocket -> set of subscribed channels
        self._clients: dict[Any, set[str]] = {}
        self._lock = asyncio.Lock()
        self._driver_codes: set[str] = set()

    def _welcome_message(self) -> str:
        snap = self.state.snapshot()
        driver_codes = [
            info.get("Tla", num)
            for num, info in snap["driver_info"].items()
        ]
        driver_channels = [f"telemetry.drivers.{c}" for c in driver_codes]
        return json.dumps({
            "type": "welcome",
            "default_channel": "telemetry.drivers",
            "channels": OPW_CHANNELS,
            "driver_channel_pattern": "telemetry.drivers.{DRIVER_CODE}",
            "session": {
                "source": "F1 Live Timing",
                "total_laps": snap["lap_count"].get("TotalLaps", "?"),
                "driver_count": len(snap["driver_info"]),
                "driver_codes": driver_codes,
                "driver_channels": driver_channels,
                "replay_speed": 1.0,
                "loop_forever": False,
            },
        })

    async def _handle_client(self, ws):
        async with self._lock:
            self._clients[ws] = set()

        log.info(f"Client connected: {ws.remote_address}")
        await ws.send(self._welcome_message())

        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if msg.get("action") == "subscribe":
                    channels = set(msg.get("channels", []))
                    async with self._lock:
                        self._clients[ws] = channels
                    await ws.send(json.dumps({
                        "type": "subscribed",
                        "channels": list(channels),
                    }))
                    log.info(f"Client subscribed to: {channels}")

        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            async with self._lock:
                self._clients.pop(ws, None)
            log.info(f"Client disconnected: {ws.remote_address}")

    async def _broadcast_loop(self):
        """Publish state snapshots at 10 Hz to all subscribed clients."""
        while True:
            await asyncio.sleep(0.1)

            snap = self.state.snapshot()
            if not snap["telemetry"]:
                continue

            frames = _build_frames(snap)

            async with self._lock:
                clients = dict(self._clients)

            dead = []
            for ws, subscribed in clients.items():
                if not subscribed:
                    continue
                to_send = []
                for ch in subscribed:
                    if ch in frames:
                        to_send.append(frames[ch])
                    # single-driver channel pattern
                    elif ch.startswith("telemetry.drivers.") and ch in frames:
                        to_send.append(frames[ch])

                for msg in to_send:
                    try:
                        await ws.send(json.dumps(msg))
                    except websockets.exceptions.ConnectionClosed:
                        dead.append(ws)
                        break

            if dead:
                async with self._lock:
                    for ws in dead:
                        self._clients.pop(ws, None)

    async def serve(self):
        log.info(f"OPW WebSocket server starting on ws://{self.host}:{self.port}")
        async with websockets.serve(self._handle_client, self.host, self.port):
            await self._broadcast_loop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Bridge F1 live timing to Open Pit Wall dashboards"
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-auth", action="store_true")
    parser.add_argument("--record", metavar="FILE", default=None,
                        help="Also save raw SignalR feed to a file")
    args = parser.parse_args()

    state = LiveState()
    client = LiveF1Client(state, record_file=args.record, no_auth=args.no_auth)

    def run_f1_client():
        try:
            client.start()
        except Exception as e:
            log.error(f"F1 client error: {e}")

    log.info("Connecting to F1 live timing feed...")
    threading.Thread(target=run_f1_client, daemon=True).start()

    server = OPWServer(state, host=args.host, port=args.port)
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        log.info("Shutting down.")
        client.stop()


if __name__ == "__main__":
    main()
