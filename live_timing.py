"""
Live timing listener for Monaco GP 2026.

Run this during an active session (practice, qualifying, or race).
FastF1 connects to the F1 live timing feed via SignalR.

Usage:
    .venv/bin/python live_timing.py
"""

import fastf1
from fastf1 import livetiming
from fastf1.livetiming.client import SignalRClient

import os

# Cache directory for any static data fetched alongside live data
os.makedirs("cache", exist_ok=True)
fastf1.Cache.enable_cache("cache")


def main():
    # Record the live timing stream to a file so you can replay it later.
    # When a session is live, SignalRClient connects to the F1 timing feed.
    output_file = "monaco_2026_live.txt"

    print("Connecting to F1 live timing feed...")
    print(f"Recording to: {output_file}")
    print("Press Ctrl+C to stop.\n")

    client = SignalRClient(filename=output_file, filemode="w", debug=False)

    try:
        client.start()
    except KeyboardInterrupt:
        print("\nStopped recording.")


if __name__ == "__main__":
    main()
