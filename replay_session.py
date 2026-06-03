"""
Load and analyze a recorded live timing session (or past session from the API).

If running after the race, FastF1 can also load Monaco directly:
    session = fastf1.get_session(2026, "Monaco", "R")
    session.load()
"""

import fastf1
import os

os.makedirs("cache", exist_ok=True)
fastf1.Cache.enable_cache("cache")


def load_from_recording(filename: str):
    """Load a session from a recorded live timing file."""
    session = fastf1.get_session(2026, "Monaco", "R")
    session.load(livedata=fastf1.livetiming.LiveTimingData(filename))
    return session


def load_from_api():
    """Load Monaco race from the F1 timing API (available after the session ends)."""
    session = fastf1.get_session(2026, "Monaco", "R")
    session.load()
    return session


def print_summary(session):
    laps = session.laps
    print(f"\n=== {session.event['EventName']} {session.name} ===")
    print(f"Total laps recorded: {len(laps)}")

    fastest = laps.pick_fastest()
    print(f"Fastest lap: {fastest['Driver']} — {fastest['LapTime']}")

    print("\nTop 5 fastest laps:")
    top5 = laps.groupby("Driver")["LapTime"].min().sort_values().head(5)
    print(top5.to_string())


if __name__ == "__main__":
    recording = "monaco_2026_live.txt"
    if os.path.exists(recording):
        print(f"Loading from recording: {recording}")
        session = load_from_recording(recording)
    else:
        print("No recording found — loading from F1 API (session must have ended).")
        session = load_from_api()

    print_summary(session)
