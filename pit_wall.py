"""
Open Pit Wall — live F1 timing terminal dashboard.

Connects to the F1 live timing feed and renders a live timing board
similar to the official F1 timing app.

Usage:
    .venv/bin/python pit_wall.py [--no-auth] [--record FILE]

Options:
    --no-auth       Connect without F1 authentication (may give partial data)
    --record FILE   Also save the raw feed to a file (default: session_recording.txt)
"""

import argparse
import copy
import json
import threading
import time
from datetime import datetime

from rich.columns import Columns
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from signalrcore.hub_connection_builder import HubConnectionBuilder
from signalrcore.messages.completion_message import CompletionMessage
import requests

import fastf1
from fastf1.internals.f1auth import get_auth_token


# ---------------------------------------------------------------------------
# Lap time status codes from F1 timing feed
# ---------------------------------------------------------------------------
STATUS_OVERALL_FASTEST = 2048   # purple
STATUS_PERSONAL_BEST = 2049     # green
STATUS_NO_TIME = 0


def _style_for_status(status: int) -> str:
    if status == STATUS_OVERALL_FASTEST:
        return "bold magenta"
    if status == STATUS_PERSONAL_BEST:
        return "bold green"
    return "yellow"


# ---------------------------------------------------------------------------
# Track status codes
# ---------------------------------------------------------------------------
TRACK_STATUS = {
    "1": ("green",  "Track Clear"),
    "2": ("yellow", "Yellow Flag"),
    "4": ("yellow", "Safety Car"),
    "5": ("red",    "Red Flag"),
    "6": ("yellow", "VSC Deployed"),
    "7": ("yellow", "VSC Ending"),
}

COMPOUND_COLOUR = {
    "SOFT":  "bold red",
    "MEDIUM": "bold yellow",
    "HARD":  "bold white",
    "INTER": "bold green",
    "WET":   "bold blue",
    "TEST_UNKNOWN": "dim",
    "UNKNOWN": "dim",
}


def _deep_merge(base: dict, update: dict) -> dict:
    """Recursively merge update into base (in-place on base)."""
    for k, v in update.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


# ---------------------------------------------------------------------------
# Live state store
# ---------------------------------------------------------------------------
class TimingState:
    def __init__(self):
        self._lock = threading.Lock()
        self.driver_list: dict = {}       # racing_number -> driver info
        self.timing_data: dict = {}       # Lines: racing_number -> timing
        self.timing_app: dict = {}        # Lines: racing_number -> stint info
        self.timing_stats: dict = {}
        self.track_status: dict = {}
        self.session_info: dict = {}
        self.lap_count: dict = {}
        self.weather: dict = {}
        self.race_control: list = []
        self.connected = False
        self.last_update = None

    def apply(self, topic: str, data):
        with self._lock:
            self.last_update = time.time()
            if topic == "DriverList":
                _deep_merge(self.driver_list, data)
            elif topic == "TimingData":
                lines = data.get("Lines", {})
                if lines:
                    if "Lines" not in self.timing_data:
                        self.timing_data["Lines"] = {}
                    _deep_merge(self.timing_data["Lines"], lines)
            elif topic == "TimingAppData":
                lines = data.get("Lines", {})
                if lines:
                    if "Lines" not in self.timing_app:
                        self.timing_app["Lines"] = {}
                    _deep_merge(self.timing_app["Lines"], lines)
            elif topic == "TimingStats":
                _deep_merge(self.timing_stats, data)
            elif topic == "TrackStatus":
                _deep_merge(self.track_status, data)
            elif topic == "SessionInfo":
                _deep_merge(self.session_info, data)
            elif topic == "LapCount":
                _deep_merge(self.lap_count, data)
            elif topic == "WeatherData":
                _deep_merge(self.weather, data)
            elif topic == "RaceControlMessages":
                msgs = data.get("Messages", {})
                for v in msgs.values():
                    if isinstance(v, dict):
                        self.race_control.append(v)
                        if len(self.race_control) > 20:
                            self.race_control.pop(0)

    def snapshot(self):
        with self._lock:
            return (
                copy.deepcopy(self.driver_list),
                copy.deepcopy(self.timing_data),
                copy.deepcopy(self.timing_app),
                copy.deepcopy(self.track_status),
                copy.deepcopy(self.session_info),
                copy.deepcopy(self.lap_count),
                copy.deepcopy(self.weather),
                copy.deepcopy(self.race_control),
                self.connected,
                self.last_update,
            )


# ---------------------------------------------------------------------------
# SignalR client subclass that feeds into TimingState
# ---------------------------------------------------------------------------
class PitWallClient:
    _connection_url = 'wss://livetiming.formula1.com/signalrcore'
    _negotiate_url = 'https://livetiming.formula1.com/signalrcore/negotiate'

    _topics = [
        "Heartbeat", "DriverList", "ExtrapolatedClock", "RaceControlMessages",
        "SessionInfo", "SessionStatus", "TimingAppData", "TimingStats",
        "TrackStatus", "WeatherData", "TimingData", "TopThree", "LapCount",
    ]

    def __init__(self, state: TimingState, record_file: str = None,
                 no_auth: bool = False):
        self.state = state
        self.record_file = record_file
        self.no_auth = no_auth
        self._connection = None
        self._output_file = None

    def _parse_and_apply(self, topic: str, raw_data):
        if isinstance(raw_data, str):
            try:
                data = json.loads(
                    raw_data.replace("'", '"')
                             .replace('True', 'true')
                             .replace('False', 'false')
                )
            except (json.JSONDecodeError, ValueError):
                return
        else:
            data = raw_data
        self.state.apply(topic, data)

    def _on_message(self, msg):
        if isinstance(msg, CompletionMessage):
            # Initial snapshot: result is {topic: data, ...}
            if msg.result:
                for topic, data in msg.result.items():
                    self._parse_and_apply(topic, data)
            if self._output_file:
                for topic, data in (msg.result or {}).items():
                    line = str([topic, json.dumps(data), ''])
                    self._output_file.write(line + '\n')
                    self._output_file.flush()

        elif isinstance(msg, list) and len(msg) >= 2:
            # Feed update: [topic, data, timestamp]
            topic = msg[0]
            data = msg[1]
            self._parse_and_apply(topic, data)
            if self._output_file:
                self._output_file.write(str(msg) + '\n')
                self._output_file.flush()

    def start(self):
        if self.record_file:
            self._output_file = open(self.record_file, 'w')

        headers = {}
        r = requests.options(self._negotiate_url, headers=headers)
        if 'AWSALBCORS' in r.cookies:
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
        self._connection.on('feed', self._on_message)
        self._connection.start()

    def _on_open(self):
        self.state.connected = True
        self._connection.send(
            "Subscribe", [self._topics], on_invocation=self._on_message
        )

    def _on_close(self):
        self.state.connected = False

    def stop(self):
        if self._connection:
            self._connection.stop()
        if self._output_file:
            self._output_file.close()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _get_stint(app_lines: dict, racing_number: str) -> tuple[str, int]:
    """Return (compound, laps_on_tire) for a driver."""
    driver_app = app_lines.get(racing_number, {})
    stints = driver_app.get("Stints", {})
    if not stints:
        return "", 0
    latest_key = max(stints.keys(), key=lambda k: int(k))
    stint = stints[latest_key]
    compound = stint.get("Compound", "")
    laps = int(stint.get("TotalLaps", 0) or 0)
    return compound, laps


def _sector_text(sectors) -> list[Text]:
    """Build sector time Text objects from a list or dict of sectors."""
    if isinstance(sectors, dict):
        sectors = [sectors[k] for k in sorted(sectors.keys())]
    texts = []
    for s in sectors[:3]:
        val = s.get("Value", "") if isinstance(s, dict) else ""
        status = s.get("Status", 0) if isinstance(s, dict) else 0
        if not val:
            t = Text("---.---", style="dim")
        else:
            t = Text(val.ljust(7), style=_style_for_status(status))
        texts.append(t)
    while len(texts) < 3:
        texts.append(Text("---.---", style="dim"))
    return texts


def build_timing_table(
    driver_list, timing_lines, app_lines,
    track_status, session_info, lap_count, weather
) -> Table:
    event = session_info.get("Meeting", {}).get("Name", "—")
    session_type = session_info.get("Name", "—")

    track_code = track_status.get("Status", "1")
    track_color, track_label = TRACK_STATUS.get(track_code, ("white", "Unknown"))

    current_lap = lap_count.get("CurrentLap", "—")
    total_laps = lap_count.get("TotalLaps", "—")
    lap_str = f"Lap {current_lap}/{total_laps}" if current_lap != "—" else ""

    air_temp = weather.get("AirTemp", "—")
    track_temp = weather.get("TrackTemp", "—")
    rain = weather.get("Rainfall", "0")
    rain_str = " | Rain" if rain not in ("0", 0, False, "False", "") else ""

    header = Text()
    header.append(f"  {event} — {session_type}", style="bold white")
    if lap_str:
        header.append(f"  {lap_str}", style="cyan")
    header.append(f"  [{track_label}]", style=f"bold {track_color}")
    header.append(
        f"  Air {air_temp}°C  Track {track_temp}°C{rain_str}",
        style="dim"
    )

    table = Table(
        title=header,
        show_header=True,
        header_style="bold",
        border_style="bright_black",
        row_styles=["", "dim"],
        expand=True,
    )

    table.add_column("Pos", width=4, justify="right")
    table.add_column("Driver", width=4)
    table.add_column("Gap", width=10, justify="right")
    table.add_column("Int", width=10, justify="right")
    table.add_column("Last Lap", width=10, justify="right")
    table.add_column("Best Lap", width=10, justify="right")
    table.add_column("S1", width=7)
    table.add_column("S2", width=7)
    table.add_column("S3", width=7)
    table.add_column("Tyre", width=7)
    table.add_column("Age", width=4, justify="right")
    table.add_column("Pits", width=4, justify="right")

    # Sort drivers by position
    drivers_by_pos = sorted(
        timing_lines.items(),
        key=lambda kv: int(kv[1].get("Position", 99) or 99)
    )

    for racing_number, t in drivers_by_pos:
        driver = driver_list.get(racing_number, {})
        abbr = driver.get("Tla", racing_number)
        team_color = driver.get("TeamColour", "ffffff")

        pos = str(t.get("Position", ""))
        gap = str(t.get("GapToLeader", ""))
        interval_obj = t.get("IntervalToPositionAhead", {})
        interval = interval_obj.get("Value", "") if isinstance(interval_obj, dict) else ""

        last_obj = t.get("LastLapTime", {})
        last_val = last_obj.get("Value", "") if isinstance(last_obj, dict) else ""
        last_status = last_obj.get("Status", 0) if isinstance(last_obj, dict) else 0

        best_obj = t.get("BestLapTime", {})
        best_val = best_obj.get("Value", "") if isinstance(best_obj, dict) else ""
        best_status = best_obj.get("Status", 0) if isinstance(best_obj, dict) else 0

        sectors = t.get("Sectors", [])
        s1, s2, s3 = _sector_text(sectors)

        compound, laps = _get_stint(app_lines, racing_number)
        compound_style = COMPOUND_COLOUR.get(compound, "white")
        compound_short = {"SOFT": "S", "MEDIUM": "M", "HARD": "H",
                          "INTER": "I", "WET": "W"}.get(compound, compound[:1] if compound else "?")

        pits = str(t.get("NumberOfPitStops", 0) or 0)

        in_pit = t.get("InPit", False)
        pit_out = t.get("PitOut", False)
        stopped = t.get("Stopped", False)

        try:
            team_rgb = tuple(int(team_color[i:i+2], 16) for i in (0, 2, 4))
            driver_style = f"rgb({team_rgb[0]},{team_rgb[1]},{team_rgb[2]})"
        except Exception:
            driver_style = "white"

        abbr_text = Text(abbr, style=f"bold {driver_style}")
        if in_pit:
            abbr_text.append(" P", style="bold yellow")
        elif pit_out:
            abbr_text.append(" O", style="bold cyan")
        elif stopped:
            abbr_text.append(" S", style="bold red")

        last_text = Text(last_val or "—", style=_style_for_status(last_status))
        best_text = Text(best_val or "—", style=_style_for_status(best_status))

        table.add_row(
            pos,
            abbr_text,
            gap or "—",
            interval or "—",
            last_text,
            best_text,
            s1, s2, s3,
            Text(compound_short, style=compound_style),
            str(laps) if laps else "—",
            pits,
        )

    return table


def build_race_control_panel(messages: list) -> Panel:
    lines = []
    for msg in reversed(messages[-6:]):
        flag = msg.get("Flag", "")
        category = msg.get("Category", "")
        message = msg.get("Message", "")
        lap = msg.get("Lap", "")
        lap_str = f"[Lap {lap}] " if lap else ""

        flag_style = {
            "YELLOW": "yellow", "RED": "red", "GREEN": "green",
            "BLUE": "blue", "CHEQUERED": "white", "CLEAR": "green",
            "SAFETY CAR": "yellow", "VIRTUAL SAFETY CAR": "yellow",
        }.get(flag, "white")

        line = Text()
        line.append(f"{lap_str}", style="dim")
        if flag:
            line.append(f"[{flag}] ", style=f"bold {flag_style}")
        line.append(message)
        lines.append(line)

    content = Text("\n").join(lines) if lines else Text("No messages yet", style="dim")
    return Panel(content, title="Race Control", border_style="bright_black", height=10)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Open Pit Wall — F1 Live Timing")
    parser.add_argument("--no-auth", action="store_true",
                        help="Connect without F1 auth (may give partial data)")
    parser.add_argument("--record", metavar="FILE", default="session_recording.txt",
                        help="File to record raw feed (default: session_recording.txt)")
    args = parser.parse_args()

    state = TimingState()
    client = PitWallClient(state, record_file=args.record, no_auth=args.no_auth)

    console = Console()

    def connect_in_background():
        try:
            client.start()
        except Exception as e:
            console.print(f"[red]Connection error: {e}[/red]")

    t = threading.Thread(target=connect_in_background, daemon=True)
    t.start()

    console.print("[cyan]Connecting to F1 live timing feed...[/cyan]")
    console.print(f"[dim]Recording to: {args.record}[/dim]")
    console.print("[dim]Press Ctrl+C to exit[/dim]\n")

    with Live(console=console, refresh_per_second=2, screen=True) as live:
        try:
            while True:
                (
                    driver_list, timing_data, timing_app,
                    track_status, session_info, lap_count, weather,
                    race_control, connected, last_update,
                ) = state.snapshot()

                timing_lines = timing_data.get("Lines", {})
                app_lines = timing_app.get("Lines", {})

                if not timing_lines:
                    status = "[cyan]Waiting for data...[/cyan]"
                    if not connected and last_update is None:
                        status = "[yellow]Connecting...[/yellow]"
                    elif not connected:
                        status = "[red]Disconnected[/red]"
                    live.update(Panel(status, title="Open Pit Wall"))
                else:
                    timing_table = build_timing_table(
                        driver_list, timing_lines, app_lines,
                        track_status, session_info, lap_count, weather,
                    )
                    rc_panel = build_race_control_panel(race_control)

                    now = datetime.now().strftime("%H:%M:%S")
                    conn_indicator = (
                        "[green]● LIVE[/green]" if connected
                        else "[red]● OFFLINE[/red]"
                    )
                    footer = Text(f" {now}  {conn_indicator}", style="dim")

                    from rich.layout import Layout
                    layout = Layout()
                    layout.split_column(
                        Layout(timing_table, name="timing"),
                        Layout(rc_panel, name="rc", size=10),
                        Layout(footer, name="footer", size=1),
                    )
                    live.update(layout)

                time.sleep(0.5)

        except KeyboardInterrupt:
            pass
        finally:
            client.stop()

    console.print("[green]Disconnected. Goodbye.[/green]")


if __name__ == "__main__":
    main()
