"""
OPW Track Map — shows driver positions on the circuit, fed from an
Open Pit Wall WebSocket broadcaster (or the live_opw_bridge.py).

Displays the real circuit shape (loaded from FastF1) alongside the OPW
telemetry trace dashboard.

Run the OPW broadcaster first, then:

    cd ~/projects/f1-race-replay
    ./env/bin/python opw_track_map.py --year 2026 --round "Canadian Grand Prix"

Optional arguments:
    --host  127.0.0.1   WebSocket host (default: 127.0.0.1)
    --port  8765        WebSocket port (default: 8765)
    --year  2026        Season year for circuit geometry (default: 2026)
    --round "Canadian"  Round name/number for geometry (default: Canadian Grand Prix)
    --session R         Session type for geometry (default: R)
    --rotation 0        Degrees to rotate the circuit (default: 0)
"""

import argparse
import asyncio
import json
import math
import os
import sys
import threading

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QStatusBar, QListWidget, QListWidgetItem,
)
from PySide6.QtGui import QFont, QColor
from websockets.asyncio.client import connect

# _TrackMapWidget lives in the f1-race-replay submodule under vendor/.
# Add it to sys.path so its `src.*` imports resolve as the package expects.
_VENDOR_REPLAY = os.path.join(os.path.dirname(__file__), "vendor", "f1-race-replay")
if _VENDOR_REPLAY not in sys.path:
    sys.path.insert(0, _VENDOR_REPLAY)

from src.insights.track_position_window import _TrackMapWidget


# ---------------------------------------------------------------------------
# Palette (matches f1-race-replay's original)
# ---------------------------------------------------------------------------
_PALETTE = [
    "#E8002D", "#FF8000", "#00D2BE", "#1565C0", "#F596C8",
    "#DC0000", "#B6BABD", "#5E8FAA", "#2293D1", "#FFF500",
    "#006F62", "#900000", "#0090FF", "#FF87BC", "#64C4FF",
    "#358C75", "#AAAAAA", "#6CD3BF", "#ABB7C4", "#C92D4B",
]


# ---------------------------------------------------------------------------
# WebSocket client thread
# ---------------------------------------------------------------------------
class OPWClient(QObject):
    """Connects to the OPW WebSocket and emits signals with parsed data."""

    positions_updated = Signal(dict, dict, str)   # {code: (x, y)}, colors, leader
    status_changed = Signal(str)
    welcome_received = Signal(dict)
    announcements_updated = Signal(list)          # [{"text", "lap"}, ...]

    def __init__(self, host: str, port: int):
        super().__init__()
        self._host = host
        self._port = port
        self._running = False
        self._thread = None
        self._colors: dict[str, str] = {}
        self._color_idx = 0

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        asyncio.run(self._stream())

    async def _stream(self):
        url = f"ws://{self._host}:{self._port}"
        retry_delay = 2.0
        while self._running:
            self.status_changed.emit("Connecting…")
            try:
                await self._connect(url)
                retry_delay = 2.0  # reset on clean disconnect
            except Exception as e:
                self.status_changed.emit(f"Retrying in {retry_delay:.0f}s… ({e})")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, 15.0)

    async def _connect(self, url: str):
        async with connect(url, ping_interval=20, max_size=None) as ws:
            self.status_changed.emit("Connected")

            welcome_raw = await ws.recv()
            welcome = json.loads(welcome_raw)
            self.welcome_received.emit(welcome)

            await ws.send(json.dumps({
                "action": "subscribe",
                "channels": ["telemetry.drivers", "leaderboard",
                             "telemetry.lap", "race_control"],
            }))

            driver_xy: dict[str, tuple] = {}
            leader_code: str | None = None

            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                event = msg.get("event", "")

                if event == "telemetry.drivers":
                    for entry in msg.get("payload", []):
                        code = entry.get("driver_code", "")
                        if not code:
                            continue
                        pos = entry.get("position", {})
                        x = pos.get("x")
                        y = pos.get("y")
                        if x is not None and y is not None:
                            driver_xy[code] = (float(x), float(y))
                            self._ensure_color(code)

                elif event == "leaderboard":
                    drivers = msg.get("payload", {}).get("drivers", [])
                    if drivers:
                        leader_code = drivers[0].get("driver_code")

                elif event == "race_control":
                    anns = msg.get("payload", {}).get("announcements", [])
                    if anns:
                        self.announcements_updated.emit(list(anns))

                if driver_xy:
                    self.positions_updated.emit(
                        dict(driver_xy),
                        dict(self._colors),
                        leader_code or "",
                    )

    def _ensure_color(self, code: str):
        if code not in self._colors:
            self._colors[code] = _PALETTE[self._color_idx % len(_PALETTE)]
            self._color_idx += 1


# ---------------------------------------------------------------------------
# Circuit geometry loader (FastF1, run once in a background thread)
# ---------------------------------------------------------------------------
def load_circuit_geometry(year: int, round_name: str, session_type: str,
                          rotation_deg: float, callback):
    """
    Load circuit layout from FastF1 in a background thread, then call
    callback(x_c, y_c, x_i, y_i, x_o, y_o, rotation_deg, circuit_length_m).
    """
    def _load_one(fastf1, np, yr, sess):
        """Load circuit outline from one session. Raises if no telemetry."""
        session = fastf1.get_session(yr, round_name, sess)
        session.load(telemetry=True, laps=True, weather=False, messages=False)

        fastest = session.laps.pick_fastest()
        tel = fastest.get_telemetry()

        x = tel["X"].tolist()
        y = tel["Y"].tolist()
        if len(x) < 3:
            raise ValueError("no position telemetry")

        # Build inner/outer edges by offsetting the centre line perpendicular
        track_width = 150  # ~15 m in 1/10 metre units
        x_inner, y_inner, x_outer, y_outer = [], [], [], []
        n = len(x)
        for i in range(n):
            prev_i = (i - 1) % n
            next_i = (i + 1) % n
            tx = x[next_i] - x[prev_i]
            ty = y[next_i] - y[prev_i]
            tlen = math.hypot(tx, ty) or 1.0
            nx_v = -ty / tlen * track_width
            ny_v = tx / tlen * track_width
            x_inner.append(x[i] + nx_v)
            y_inner.append(y[i] + ny_v)
            x_outer.append(x[i] - nx_v)
            y_outer.append(y[i] - ny_v)

        diffs = np.sqrt(np.diff(tel["X"].values) ** 2 + np.diff(tel["Y"].values) ** 2)
        circuit_length_m = float(diffs.sum()) * 0.1  # 1/10 metre units → metres

        return x, y, x_inner, y_inner, x_outer, y_outer, circuit_length_m

    def _load():
        import os
        import fastf1
        import numpy as np

        cache_dir = os.path.join(os.path.dirname(__file__), ".fastf1-cache")
        os.makedirs(cache_dir, exist_ok=True)
        fastf1.Cache.enable_cache(cache_dir)

        # The live race isn't in the historical API yet, so fall back to the
        # same circuit from earlier sessions/years. The layout is identical.
        candidates = [
            (year, session_type),
            (year, "Q"),
            (year - 1, "R"),
            (year - 1, "Q"),
            (year - 2, "R"),
        ]
        for yr, sess in candidates:
            try:
                geom = _load_one(fastf1, np, yr, sess)
            except Exception as e:
                print(f"[geometry] {yr} {round_name} {sess}: {e}")
                continue
            if (yr, sess) != (year, session_type):
                print(f"[geometry] Using {yr} {round_name} {sess} layout "
                      f"(live session not yet in historical API)")
            x, y, xi, yi, xo, yo, length = geom
            callback(x, y, xi, yi, xo, yo, rotation_deg, length)
            return

        print("[geometry] Failed to load circuit from any session — "
              "track map will use the circular schematic.")

    threading.Thread(target=_load, daemon=True).start()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class OPWTrackMapWindow(QMainWindow):

    _geometry_ready = Signal(list, list, list, list, list, list, float, float)

    def __init__(self, host: str, port: int, year: int, round_name: str,
                 session_type: str, rotation_deg: float):
        super().__init__()
        self.setWindowTitle("OPW Track Map")
        self.setMinimumSize(540, 580)
        self.setStyleSheet("background: #1a1a1a; color: #cccccc;")

        self._circuit_length_m: float | None = None

        # Projection table (metre-space centerline) for mapping live X/Y → lap
        # fraction. Built when geometry loads.
        self._proj_x = None
        self._proj_y = None
        self._proj_frac = None

        self._setup_ui()

        # Geometry signal (emitted from background thread → applied on main thread)
        self._geometry_ready.connect(self._on_geometry_ready)

        # OPW WebSocket client
        self._client = OPWClient(host, port)
        self._client.positions_updated.connect(self._on_positions_updated)
        self._client.status_changed.connect(self._on_status_changed)
        self._client.welcome_received.connect(self._on_welcome)
        self._client.announcements_updated.connect(self._on_announcements)
        self._client.start()

        # Load circuit geometry in background
        self._status_label.setText("Loading circuit geometry…")
        load_circuit_geometry(
            year, round_name, session_type, rotation_deg,
            lambda *args: self._geometry_ready.emit(*args),
        )

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)

        # Status bar row
        bar = QHBoxLayout()
        self._lap_label = self._make_label("Lap: —")
        self._status_label = self._make_label("Connecting…")
        self._circuit_len_label = self._make_label("")

        # View toggle buttons
        _active = ("QPushButton { background:#555; color:#fff; border:1px solid #777; "
                   "padding:3px 10px; font-size:10px; }")
        _inactive = ("QPushButton { background:#2a2a2a; color:#888; border:1px solid #555; "
                     "padding:3px 10px; font-size:10px; }")
        self._btn_real = QPushButton("Real Track")
        self._btn_circle = QPushButton("Circular")
        self._btn_real.setFixedHeight(24)
        self._btn_circle.setFixedHeight(24)
        self._btn_real.setStyleSheet(_inactive)
        self._btn_circle.setStyleSheet(_active)
        self._btn_real.clicked.connect(lambda: self._set_view("real", _active, _inactive))
        self._btn_circle.clicked.connect(lambda: self._set_view("circle", _active, _inactive))
        self._active_style = _active
        self._inactive_style = _inactive

        toggle = QHBoxLayout()
        toggle.setSpacing(0)
        toggle.addWidget(self._btn_circle)
        toggle.addWidget(self._btn_real)

        bar.addWidget(self._lap_label)
        bar.addStretch()
        bar.addWidget(self._status_label)
        bar.addStretch()
        bar.addLayout(toggle)
        bar.addStretch()
        bar.addWidget(self._circuit_len_label)
        root.addLayout(bar)

        self._map = _TrackMapWidget()
        root.addWidget(self._map, stretch=1)

        # Race control / penalty feed
        rc_label = self._make_label("Race Control")
        rc_label.setStyleSheet("color:#888; font-weight:bold; padding-top:4px;")
        root.addWidget(rc_label)

        self._rc_list = QListWidget()
        self._rc_list.setFixedHeight(132)
        self._rc_list.setStyleSheet(
            "QListWidget { background:#141414; border:1px solid #333; "
            "font-family:Menlo,Monaco,monospace; font-size:11px; color:#ddd; }"
        )
        root.addWidget(self._rc_list)
        self._ann_count = 0  # how many announcements we've already rendered

        status_bar = QStatusBar()
        self.setStatusBar(status_bar)
        self._conn_label = QLabel("Disconnected")
        self._msg_label = QLabel("Frames: 0")
        status_bar.addPermanentWidget(self._conn_label)
        status_bar.addPermanentWidget(self._msg_label)
        self._frame_count = 0

    # Colour each announcement by its leading marker.
    _ANN_COLORS = {
        "🚨": "#ff5a5f",  # penalty
        "🔍": "#ffd166",  # under investigation
        "📝": "#aaaaaa",  # noted
        "⏱": "#4cc9f0",  # lap time deleted
        "✅": "#52c41a",  # no further action
    }

    def _on_announcements(self, anns: list):
        # The bridge re-sends the full list every cycle; only render new ones.
        if len(anns) <= self._ann_count:
            return
        for ann in anns[self._ann_count:]:
            text = ann.get("text", "")
            item = QListWidgetItem(text)
            item.setForeground(QColor(self._ANN_COLORS.get(text[:1], "#dddddd")))
            self._rc_list.insertItem(0, item)  # newest on top
        self._ann_count = len(anns)
        # Keep the list bounded in the UI too
        while self._rc_list.count() > 60:
            self._rc_list.takeItem(self._rc_list.count() - 1)

    def _set_view(self, mode: str, active: str, inactive: str):
        is_circle = mode == "circle"
        self._map.force_circle = is_circle
        self._btn_circle.setStyleSheet(active if is_circle else inactive)
        self._btn_real.setStyleSheet(inactive if is_circle else active)
        self._map.update()

    @staticmethod
    def _make_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(QFont("Arial", 10))
        lbl.setStyleSheet("color: #cccccc;")
        return lbl

    def _on_geometry_ready(self, x_c, y_c, x_i, y_i, x_o, y_o,
                           rotation_deg, circuit_length_m):
        self._circuit_length_m = circuit_length_m
        self._circuit_len_label.setText(f"Circuit: {circuit_length_m:.0f} m")
        self._map.set_track_geometry(x_c, y_c, x_i, y_i, x_o, y_o, rotation_deg)

        # Build the projection table in metre-space. The centerline X/Y are in
        # 1/10-metre units (FastF1 telemetry); the live feed gives metres, so
        # scale the centerline to match.
        self._proj_x = np.array(x_c, dtype=float) * 0.1
        self._proj_y = np.array(y_c, dtype=float) * 0.1
        seg = np.hypot(np.diff(self._proj_x), np.diff(self._proj_y))
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        total = cum[-1] if cum[-1] > 0 else 1.0
        self._proj_frac = cum / total

        self._status_label.setText("Circuit loaded ✓")
        # Switch to real track view automatically
        self._set_view("real", self._active_style, self._inactive_style)

    def _project(self, x: float, y: float) -> float:
        """Nearest-point projection of a live (x, y) onto the lap fraction."""
        d2 = (self._proj_x - x) ** 2 + (self._proj_y - y) ** 2
        return float(self._proj_frac[int(d2.argmin())])

    def _on_welcome(self, welcome: dict):
        session = welcome.get("session", {})
        total_laps = session.get("total_laps", "?")
        drivers = session.get("driver_count", "?")
        self._status_label.setText(f"Connected — {drivers} drivers, {total_laps} laps")

    def _on_positions_updated(self, xy: dict, colors: dict, leader: str):
        self._frame_count += 1
        self._msg_label.setText(f"Frames: {self._frame_count}")
        if self._proj_x is None:
            return  # circuit geometry not loaded yet — can't project

        # F1TV Access streams the Position feed with all coordinates zeroed
        # (real GPS is gated to higher tiers). Detect that and show a note
        # instead of collapsing every dot onto the origin.
        vals = list(xy.values())
        all_zero = bool(vals) and all(
            abs(x) < 1e-6 and abs(y) < 1e-6 for x, y in vals
        )
        if all_zero:
            self._status_label.setText(
                "⚠ Position feed returning zeros — live track positions "
                "unavailable on this subscription (telemetry still live)"
            )
            return  # don't move dots to (0,0)

        self._status_label.setText("Live positions ✓")
        fracs = {code: self._project(x, y) for code, (x, y) in xy.items()}
        self._map.update_positions(fracs, colors, leader or None,
                                   self._circuit_length_m)

    def _on_status_changed(self, status: str):
        self._conn_label.setText(f"Status: {status}")
        if "Connected" in status:
            self._conn_label.setStyleSheet("color: green; font-weight: bold;")
        elif "Error" in status or "Disconnected" in status:
            self._conn_label.setStyleSheet("color: red; font-weight: bold;")
        else:
            self._conn_label.setStyleSheet("color: orange; font-weight: bold;")

    def closeEvent(self, event):
        self._client.stop()
        event.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="OPW Track Map")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--round", dest="round_name", default="Canadian Grand Prix")
    parser.add_argument("--session", default="R")
    parser.add_argument("--rotation", type=float, default=0.0,
                        help="Degrees to rotate the circuit layout")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("OPW Track Map")
    app.setStyle("Fusion")

    window = OPWTrackMapWindow(
        host=args.host,
        port=args.port,
        year=args.year,
        round_name=args.round_name,
        session_type=args.session,
        rotation_deg=args.rotation,
    )
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
