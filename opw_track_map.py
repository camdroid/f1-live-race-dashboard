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
import re
import sys
import threading
import time

import numpy as np
from PySide6.QtCore import QObject, QRect, QThread, Signal, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QStatusBar, QListWidget, QListWidgetItem,
)
from PySide6.QtGui import QFont, QColor, QPainter
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

_HEX_COLOUR_RE = re.compile(r"[0-9A-Fa-f]{6}")

_TYRE_COLOURS = {
    "SOFT": "#e10600", "MEDIUM": "#ffd12e", "HARD": "#d8d8d8",
    "INTER": "#43b02a", "WET": "#0067ad", "UNKNOWN": "#666666",
}


def _tint(hex_colour: str, factor: float) -> str:
    """Lighten a #RRGGBB colour toward white by `factor` (0..1)."""
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    r = round(r + (255 - r) * factor)
    g = round(g + (255 - g) * factor)
    b = round(b + (255 - b) * factor)
    return f"#{r:02X}{g:02X}{b:02X}"


# ---------------------------------------------------------------------------
# Driver tower — F1 TV-style standings column
# ---------------------------------------------------------------------------
class DriverTowerWidget(QWidget):
    """Position, team colour, TLA, tyre + age, and gap for every driver.

    Re-sorts at most every RESORT_MS so side-by-side battles don't strobe
    the tower; position changes flash green/red and fade over FLASH_SECS.
    Retired drivers dim and sink below the classified runners.
    """

    ROW_H = 22
    WIDTH = 224
    FLASH_SECS = 4.0
    RESORT_MS = 500

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(self.WIDTH)
        self._rows: list[dict] = []            # current display order
        self._colors: dict[str, str] = {}      # code -> resolved "#RRGGBB"
        self._pending: list[dict] | None = None
        self._mode = "interval"                # "interval" | "leader"
        self._last_pos: dict[str, int] = {}
        self._flash: dict[str, tuple[float, int]] = {}  # code -> (t0, ±1)

        self._resort = QTimer(self)
        self._resort.setInterval(self.RESORT_MS)
        self._resort.timeout.connect(self._apply_pending)
        self._resort.start()

        self._anim = QTimer(self)              # repaints while flashes fade
        self._anim.setInterval(100)
        self._anim.timeout.connect(self.update)

    def set_mode(self, mode: str):
        self._mode = mode
        self.update()

    def update_data(self, drivers: list[dict], colors: dict[str, str]):
        self._colors = colors
        self._pending = drivers
        if not self._rows:
            self._apply_pending()  # first data: show immediately

    def _apply_pending(self):
        if self._pending is None:
            return
        drivers = self._pending
        self._pending = None

        running = [d for d in drivers if not d.get("retired")]
        retired = [d for d in drivers if d.get("retired")]
        by_pos = lambda d: d.get("position", 99)
        rows = sorted(running, key=by_pos) + sorted(retired, key=by_pos)

        now = time.monotonic()
        for d in running:
            code = d.get("driver_code", "")
            pos = d.get("position", 0)
            old = self._last_pos.get(code)
            if old is not None and pos != old:
                self._flash[code] = (now, 1 if pos < old else -1)
            self._last_pos[code] = pos

        self._flash = {c: f for c, f in self._flash.items()
                       if now - f[0] < self.FLASH_SECS}
        if self._flash and not self._anim.isActive():
            self._anim.start()
        elif not self._flash and self._anim.isActive():
            self._anim.stop()

        self._rows = rows
        self.update()

    # ------------------------------------------------------------------
    def _gap_for(self, d: dict) -> tuple[str, QColor, bool]:
        """Return (text, colour, is_status) for the gap column."""
        if d.get("retired"):
            return "OUT", QColor("#999999"), True
        if d.get("in_pit"):
            return "IN PIT", QColor("#ffb84d"), True
        if d.get("pit_out"):
            return "OUT LAP", QColor("#4cc9f0"), True
        if d.get("position") == 1:
            return "LEADER", QColor("#ffffff"), True
        val = d.get("interval") if self._mode == "interval" else d.get("gap_to_leader")
        val = str(val or "").strip()
        if not val:
            return "—", QColor("#777777"), False
        if self._mode == "interval" and d.get("catching"):
            return val, QColor("#3fdc84"), False
        return val, QColor("#dddddd"), False

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        p.fillRect(self.rect(), QColor("#141414"))
        p.setPen(QColor("#333333"))
        p.drawRect(0, 0, w - 1, self.height() - 1)

        now = time.monotonic()
        y = 1
        for d in self._rows:
            if y + self.ROW_H > self.height():
                break
            self._paint_row(p, d, y, w, now)
            y += self.ROW_H
        p.end()

    def _paint_row(self, p: QPainter, d: dict, y: int, w: int, now: float):
        code = d.get("driver_code", "")
        retired = bool(d.get("retired"))

        # Position-change flash background + arrow
        arrow = ""
        pos_colour = QColor("#8a8a8a")
        flash = self._flash.get(code)
        if flash and not retired:
            t0, direction = flash
            frac = max(0.0, 1.0 - (now - t0) / self.FLASH_SECS)
            base = QColor("#3fdc84") if direction > 0 else QColor("#ff6b6b")
            bg = QColor(base)
            bg.setAlphaF(0.14 * frac)
            p.fillRect(1, y, w - 2, self.ROW_H, bg)
            arrow = "▲" if direction > 0 else "▼"
            pos_colour = base

        p.setPen(QColor("#202020"))
        p.drawLine(1, y + self.ROW_H - 1, w - 2, y + self.ROW_H - 1)

        if retired:
            p.setOpacity(0.42)

        mono = QFont("Menlo", 9)

        # Position (right-aligned, with change arrow while flashing)
        p.setFont(mono)
        p.setPen(pos_colour)
        pos_text = "—" if retired else str(d.get("position", ""))
        if arrow:
            pos_text += arrow
        p.drawText(QRect(0, y, 32, self.ROW_H),
                   Qt.AlignRight | Qt.AlignVCenter, pos_text)

        # Team colour bar
        bar = QColor(self._colors.get(code, "#555555"))
        p.fillRect(36, y + 4, 4, self.ROW_H - 8, bar)

        # Driver code
        tla_font = QFont("Menlo", 11)
        tla_font.setBold(True)
        p.setFont(tla_font)
        p.setPen(QColor("#eeeeee"))
        p.drawText(QRect(46, y, 42, self.ROW_H),
                   Qt.AlignLeft | Qt.AlignVCenter, code)

        # Tyre: compound ring with letter, laps on set beside it
        compound = str(d.get("tyre", "UNKNOWN"))
        ring = QColor(_TYRE_COLOURS.get(compound, _TYRE_COLOURS["UNKNOWN"]))
        cx, cy = 99, y + self.ROW_H // 2
        p.setPen(ring)
        pen = p.pen()
        pen.setWidthF(2.0)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(cx - 6, cy - 6, 12, 12)
        letter_font = QFont("Menlo", 7)
        letter_font.setBold(True)
        p.setFont(letter_font)
        p.setPen(QColor("#dddddd"))
        letter = "?" if compound == "UNKNOWN" else compound[0]
        p.drawText(QRect(cx - 6, y, 12, self.ROW_H),
                   Qt.AlignCenter, letter)

        p.setFont(mono)
        p.setPen(QColor("#9a9a9a"))
        age = d.get("tyre_age", 0)
        p.drawText(QRect(108, y, 18, self.ROW_H),
                   Qt.AlignLeft | Qt.AlignVCenter, str(age) if age else "")

        # Fastest-lap chip
        if d.get("fastest_lap"):
            p.setPen(QColor("#8f5bd4"))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(127, y + 5, 16, self.ROW_H - 10, 2, 2)
            fl_font = QFont("Menlo", 6)
            fl_font.setBold(True)
            p.setFont(fl_font)
            p.setPen(QColor("#c9a0ff"))
            p.drawText(QRect(127, y, 16, self.ROW_H), Qt.AlignCenter, "FL")

        # Gap / status column
        text, colour, is_status = self._gap_for(d)
        gap_font = QFont("Menlo", 9 if is_status else 10)
        gap_font.setBold(is_status)
        p.setFont(gap_font)
        p.setPen(colour)
        p.drawText(QRect(140, y, w - 140 - 8, self.ROW_H),
                   Qt.AlignRight | Qt.AlignVCenter, text)

        if retired:
            p.setOpacity(1.0)


# ---------------------------------------------------------------------------
# WebSocket client thread
# ---------------------------------------------------------------------------
class OPWClient(QObject):
    """Connects to the OPW WebSocket and emits signals with parsed data."""

    positions_updated = Signal(dict, dict, str)   # {code: (x, y)}, colors, leader
    status_changed = Signal(str)
    welcome_received = Signal(dict)
    announcements_updated = Signal(list)          # [{"text", "lap"}, ...]
    leaderboard_updated = Signal(list, dict)      # driver dicts, resolved colors
    lap_updated = Signal(int)                     # current lap

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
                        leader_code = next(
                            (d.get("driver_code") for d in drivers
                             if not d.get("retired")),
                            drivers[0].get("driver_code"),
                        )
                        self._apply_team_colours(drivers)
                        self.leaderboard_updated.emit(
                            list(drivers), dict(self._colors)
                        )

                elif event == "telemetry.lap":
                    lap = msg.get("payload", {}).get("current_lap")
                    if lap:
                        self.lap_updated.emit(int(lap))

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

    def _apply_team_colours(self, drivers: list[dict]):
        """Resolve driver colours from the real team colours in the feed.

        Teammates share a hex, so the second driver of each team (by sorted
        code, for stability across frames) gets lightened toward white to
        stay distinguishable on the map and in the tower.
        """
        by_team: dict[str, list[str]] = {}
        for d in drivers:
            code = d.get("driver_code", "")
            colour = str(d.get("team_colour", "")).strip().lstrip("#").upper()
            if code and _HEX_COLOUR_RE.fullmatch(colour):
                by_team.setdefault(colour, []).append(code)
        for colour, codes in by_team.items():
            for i, code in enumerate(sorted(codes)):
                base = f"#{colour}"
                self._colors[code] = _tint(base, min(0.35 * i, 0.7)) if i else base


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
        self.setMinimumSize(780, 580)
        self.setStyleSheet("background: #1a1a1a; color: #cccccc;")

        self._circuit_length_m: float | None = None
        self._total_laps = None
        self._current_lap = None

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
        self._client.leaderboard_updated.connect(self._on_leaderboard)
        self._client.lap_updated.connect(self._on_lap)
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
        outer = QHBoxLayout(central)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(10)

        # Shared toggle-button styles
        _active = ("QPushButton { background:#555; color:#fff; border:1px solid #777; "
                   "padding:3px 10px; font-size:10px; }")
        _inactive = ("QPushButton { background:#2a2a2a; color:#888; border:1px solid #555; "
                     "padding:3px 10px; font-size:10px; }")
        self._active_style = _active
        self._inactive_style = _inactive

        # ---- Left column: driver tower ----
        left = QVBoxLayout()
        left.setSpacing(6)

        tower_head = QHBoxLayout()
        self._lap_label = self._make_label("LAP —")
        tower_head.addWidget(self._lap_label)
        tower_head.addStretch()

        self._btn_interval = QPushButton("Interval")
        self._btn_leader = QPushButton("Leader")
        for btn in (self._btn_interval, self._btn_leader):
            btn.setFixedHeight(24)
        self._btn_interval.setStyleSheet(_active)
        self._btn_leader.setStyleSheet(_inactive)
        self._btn_interval.clicked.connect(lambda: self._set_gap_mode("interval"))
        self._btn_leader.clicked.connect(lambda: self._set_gap_mode("leader"))

        gap_toggle = QHBoxLayout()
        gap_toggle.setSpacing(0)
        gap_toggle.addWidget(self._btn_interval)
        gap_toggle.addWidget(self._btn_leader)
        tower_head.addLayout(gap_toggle)
        left.addLayout(tower_head)

        self._tower = DriverTowerWidget()
        left.addWidget(self._tower, stretch=1)
        outer.addLayout(left)

        # ---- Right column: map bar, map, race control ----
        root = QVBoxLayout()
        root.setSpacing(6)
        outer.addLayout(root, stretch=1)

        # Status bar row
        bar = QHBoxLayout()
        self._status_label = self._make_label("Connecting…")
        self._circuit_len_label = self._make_label("")

        # View toggle buttons
        self._btn_real = QPushButton("Real Track")
        self._btn_circle = QPushButton("Circular")
        self._btn_real.setFixedHeight(24)
        self._btn_circle.setFixedHeight(24)
        self._btn_real.setStyleSheet(_inactive)
        self._btn_circle.setStyleSheet(_active)
        self._btn_real.clicked.connect(lambda: self._set_view("real", _active, _inactive))
        self._btn_circle.clicked.connect(lambda: self._set_view("circle", _active, _inactive))

        toggle = QHBoxLayout()
        toggle.setSpacing(0)
        toggle.addWidget(self._btn_circle)
        toggle.addWidget(self._btn_real)

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
        "💥": "#ff3b3b",  # incident — car stopped/retired
        "🟥": "#ff3b3b",  # red flag
        "🚨": "#ff5a5f",  # penalty
        "🟨": "#ffcc00",  # safety car
        "⚠": "#ff9f1c",  # double yellow
        "🟧": "#ff9f1c",  # virtual safety car
        "🟡": "#ffd166",  # yellow flag
        "🔍": "#ffd166",  # under investigation
        "📝": "#aaaaaa",  # noted
        "⏱": "#4cc9f0",  # lap time deleted
        "✅": "#52c41a",  # no further action / green
        "🟢": "#52c41a",  # track clear
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

    def _set_gap_mode(self, mode: str):
        self._tower.set_mode(mode)
        is_interval = mode == "interval"
        self._btn_interval.setStyleSheet(
            self._active_style if is_interval else self._inactive_style)
        self._btn_leader.setStyleSheet(
            self._inactive_style if is_interval else self._active_style)

    def _on_leaderboard(self, drivers: list, colors: dict):
        self._tower.update_data(drivers, colors)

    def _on_lap(self, lap: int):
        self._current_lap = lap
        self._refresh_lap_label()

    def _refresh_lap_label(self):
        if self._current_lap is None:
            return
        if self._total_laps:
            self._lap_label.setText(f"LAP {self._current_lap}/{self._total_laps}")
        else:
            self._lap_label.setText(f"LAP {self._current_lap}")

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
        if isinstance(total_laps, int) and total_laps > 0:
            self._total_laps = total_laps
            self._refresh_lap_label()
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
