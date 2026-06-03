"""
Replay a past race through the Open Pit Wall display.

Loads a completed session from the FastF1 API and animates the timing
board lap by lap so you can test the display before Monaco weekend.

Usage:
    .venv/bin/python replay_past_race.py                    # 2025 Monaco GP race
    .venv/bin/python replay_past_race.py --year 2025 --round "Monaco" --session Q
    .venv/bin/python replay_past_race.py --speed 5          # 5× faster stepping
    .venv/bin/python replay_past_race.py --lap 40           # jump to lap 40

Available session types: R (race), Q (qualifying), S (sprint), FP1/FP2/FP3
"""

import argparse
import os
import time
import threading

import fastf1
import pandas as pd
from rich.console import Console

from pit_wall import (
    TimingState,
    build_timing_table,
    build_race_control_panel,
)
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

os.makedirs("cache", exist_ok=True)
fastf1.Cache.enable_cache("cache")


COMPOUND_STATUS = {
    "SOFT":   "SOFT",
    "MEDIUM": "MEDIUM",
    "HARD":   "HARD",
    "INTERMEDIATE": "INTER",
    "WET":    "WET",
}


def timedelta_to_str(td) -> str:
    if pd.isnull(td):
        return ""
    total_seconds = td.total_seconds()
    minutes = int(total_seconds // 60)
    seconds = total_seconds % 60
    if minutes > 0:
        return f"{minutes}:{seconds:06.3f}"
    return f"{seconds:.3f}"


def load_session(year: int, round_name: str, session_type: str):
    console = Console()
    console.print(f"[cyan]Loading {year} {round_name} — {session_type}...[/cyan]")
    session = fastf1.get_session(year, round_name, session_type)
    session.load(telemetry=False, weather=True, messages=True)
    return session


def build_driver_list(session) -> dict:
    """Build the driver_list dict in the same format as the live feed."""
    driver_list = {}
    for drv in session.drivers:
        info = session.get_driver(drv)
        driver_list[drv] = {
            "RacingNumber": drv,
            "Tla": info.get("Abbreviation", drv),
            "BroadcastName": info.get("FullName", ""),
            "TeamColour": info.get("TeamColor", "ffffff") or "ffffff",
        }
    return driver_list


def build_timing_snapshot(session, laps_df: pd.DataFrame, lap_number: int) -> dict:
    """
    Build a timing_data 'Lines' snapshot for all drivers at the end of `lap_number`.
    Returns dict matching TimingState.timing_data["Lines"] format.
    """
    lines = {}

    # Laps up to and including this lap
    laps_so_far = laps_df[laps_df["LapNumber"] <= lap_number]

    # Best lap per driver (overall fastest across all drivers for purple)
    all_valid = laps_so_far.dropna(subset=["LapTime"])
    overall_fastest = all_valid["LapTime"].min() if not all_valid.empty else None

    for drv in session.drivers:
        drv_laps = laps_so_far[laps_so_far["DriverNumber"] == drv]
        if drv_laps.empty:
            continue

        # Position on this lap
        current_lap_rows = laps_df[
            (laps_df["LapNumber"] == lap_number) &
            (laps_df["DriverNumber"] == drv)
        ]
        position = int(current_lap_rows["Position"].iloc[0]) if not current_lap_rows.empty and not pd.isnull(current_lap_rows["Position"].iloc[0]) else 99

        # Last lap time
        last_lap_row = drv_laps.iloc[-1]
        last_lap_time = last_lap_row["LapTime"]
        last_lap_str = timedelta_to_str(last_lap_time)

        # Best lap time for this driver
        drv_valid = drv_laps.dropna(subset=["LapTime"])
        best_lap_time = drv_valid["LapTime"].min() if not drv_valid.empty else None
        best_lap_str = timedelta_to_str(best_lap_time)

        # Status codes for colouring
        def lap_status(lap_td, is_last: bool = False) -> int:
            if pd.isnull(lap_td):
                return 0
            if overall_fastest is not None and lap_td == overall_fastest:
                return 2048  # purple
            if best_lap_time is not None and lap_td == best_lap_time and is_last:
                return 2049  # green (personal best on this lap)
            return 0

        last_status = lap_status(last_lap_time, is_last=True)
        best_status = 2048 if (best_lap_time is not None and best_lap_time == overall_fastest) else 0

        # Sector times for last lap
        sectors = []
        for col in ["Sector1Time", "Sector2Time", "Sector3Time"]:
            val = last_lap_row.get(col)
            sec_str = timedelta_to_str(val) if not pd.isnull(val) else ""
            sectors.append({"Value": sec_str, "Status": 0})

        # Gap to leader (rough: sum of lap deltas isn't right but good enough for replay)
        pit_stops = int(drv_laps["PitInTime"].notna().sum())

        lines[drv] = {
            "Position": position,
            "GapToLeader": "",   # computed below
            "IntervalToPositionAhead": {"Value": ""},
            "LastLapTime": {"Value": last_lap_str, "Status": last_status},
            "BestLapTime": {"Value": best_lap_str, "Status": best_status},
            "Sectors": sectors,
            "NumberOfPitStops": pit_stops,
            "InPit": bool(not pd.isnull(last_lap_row.get("PitInTime", float("nan")))),
            "PitOut": bool(not pd.isnull(last_lap_row.get("PitOutTime", float("nan")))),
            "Stopped": False,
        }

    # Compute gaps to leader
    sorted_drivers = sorted(lines.items(), key=lambda kv: kv[1]["Position"])
    if sorted_drivers:
        leader_num = sorted_drivers[0][0]
        leader_laps = laps_so_far[laps_so_far["DriverNumber"] == leader_num]
        leader_best = leader_laps.dropna(subset=["LapTime"])["LapTime"].min() if not leader_laps.empty else None

        prev_best = leader_best
        for i, (drv, info) in enumerate(sorted_drivers):
            drv_laps = laps_so_far[laps_so_far["DriverNumber"] == drv]
            drv_best = drv_laps.dropna(subset=["LapTime"])["LapTime"].min() if not drv_laps.empty else None

            if i == 0:
                lines[drv]["GapToLeader"] = ""
                lines[drv]["IntervalToPositionAhead"] = {"Value": ""}
            else:
                if drv_best is not None and leader_best is not None:
                    gap = (drv_best - leader_best).total_seconds()
                    lines[drv]["GapToLeader"] = f"+{gap:.3f}"
                if drv_best is not None and prev_best is not None:
                    interval = (drv_best - prev_best).total_seconds()
                    lines[drv]["IntervalToPositionAhead"] = {"Value": f"+{interval:.3f}"}
            prev_best = drv_best

    return lines


def build_app_snapshot(session, laps_df: pd.DataFrame, lap_number: int) -> dict:
    """Build timing_app 'Lines' snapshot (tyre data) at lap_number."""
    app_lines = {}
    laps_so_far = laps_df[laps_df["LapNumber"] <= lap_number]

    for drv in session.drivers:
        drv_laps = laps_so_far[laps_so_far["DriverNumber"] == drv]
        if drv_laps.empty:
            continue

        stints = {}
        stint_idx = 0
        for _, lap in drv_laps.iterrows():
            if not pd.isnull(lap.get("PitOutTime", float("nan"))):
                stint_idx += 1
            compound = COMPOUND_STATUS.get(str(lap.get("Compound", "")), str(lap.get("Compound", "")))
            tyre_life = int(lap.get("TyreLife", 0) or 0)
            stints[str(stint_idx)] = {
                "Compound": compound,
                "TotalLaps": tyre_life,
            }

        app_lines[drv] = {"Stints": stints}

    return app_lines


def build_race_control(session, lap_number: int) -> list:
    """Extract race control messages up to this lap."""
    messages = []
    try:
        rc = session.race_control_messages
        if rc is None or rc.empty:
            return []
        for _, row in rc.iterrows():
            msg_lap = row.get("Lap", 0) or 0
            if msg_lap <= lap_number:
                messages.append({
                    "Lap": str(int(msg_lap)) if msg_lap else "",
                    "Flag": str(row.get("Flag", "")),
                    "Category": str(row.get("Category", "")),
                    "Message": str(row.get("Message", "")),
                })
    except Exception:
        pass
    return messages


def run_replay(session, start_lap: int, speed: float):
    console = Console()
    laps_df = session.laps

    total_laps = int(laps_df["LapNumber"].max())
    start_lap = max(1, min(start_lap, total_laps))

    driver_list = build_driver_list(session)
    event_name = session.event["EventName"]
    session_name = session.name

    # Build static session/weather info
    session_info = {
        "Meeting": {"Name": event_name},
        "Name": session_name,
    }
    track_status = {"Status": "1"}
    weather = {}
    try:
        w = session.weather_data
        if w is not None and not w.empty:
            last_w = w.iloc[-1]
            weather = {
                "AirTemp": f"{last_w.get('AirTemp', '—'):.1f}",
                "TrackTemp": f"{last_w.get('TrackTemp', '—'):.1f}",
                "Rainfall": "1" if last_w.get("Rainfall", False) else "0",
            }
    except Exception:
        pass

    console.print(f"\n[green]Replaying:[/green] {event_name} — {session_name}")
    console.print(f"Laps {start_lap}–{total_laps}  |  speed ×{speed}")
    console.print("[dim]Press Ctrl+C to exit[/dim]\n")

    delay = 1.5 / speed

    with Live(console=console, refresh_per_second=4, screen=True) as live:
        try:
            for lap_number in range(start_lap, total_laps + 1):
                timing_lines = build_timing_snapshot(session, laps_df, lap_number)
                app_lines = build_app_snapshot(session, laps_df, lap_number)
                race_control = build_race_control(session, lap_number)

                lap_count = {
                    "CurrentLap": lap_number,
                    "TotalLaps": total_laps,
                }

                # Update track status from race control flags
                for msg in race_control:
                    flag = msg.get("Flag", "")
                    status_map = {
                        "RED": "5", "YELLOW": "2", "GREEN": "1",
                        "SAFETY CAR": "4", "VIRTUAL SAFETY CAR": "6",
                        "CLEAR": "1",
                    }
                    if flag in status_map:
                        track_status = {"Status": status_map[flag]}

                timing_table = build_timing_table(
                    driver_list, timing_lines, app_lines,
                    track_status, session_info, lap_count, weather,
                )
                rc_panel = build_race_control_panel(race_control)

                footer = Text(
                    f"  REPLAY  ×{speed}  —  {event_name}",
                    style="dim"
                )

                layout = Layout()
                layout.split_column(
                    Layout(timing_table, name="timing"),
                    Layout(rc_panel, name="rc", size=10),
                    Layout(footer, name="footer", size=1),
                )
                live.update(layout)
                time.sleep(delay)

        except KeyboardInterrupt:
            pass

    console.print("[green]Replay complete.[/green]")


def main():
    parser = argparse.ArgumentParser(
        description="Replay a past race in the Open Pit Wall display"
    )
    parser.add_argument("--year", type=int, default=2025,
                        help="Season year (default: 2025)")
    parser.add_argument("--round", dest="round_name", default="Monaco",
                        help="Round name or number (default: Monaco)")
    parser.add_argument("--session", default="R",
                        help="Session type: R, Q, S, FP1, FP2, FP3 (default: R)")
    parser.add_argument("--speed", type=float, default=3.0,
                        help="Replay speed multiplier (default: 3×)")
    parser.add_argument("--lap", type=int, default=1,
                        help="Starting lap number (default: 1)")
    args = parser.parse_args()

    session = load_session(args.year, args.round_name, args.session)
    run_replay(session, start_lap=args.lap, speed=args.speed)


if __name__ == "__main__":
    main()
