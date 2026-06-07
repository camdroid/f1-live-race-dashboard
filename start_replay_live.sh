#!/usr/bin/env bash
# start_replay_live.sh — replay a recorded live session through the dashboards
#
# Runs the bridge in --replay mode (no F1TV auth, no network) against a
# recording made by start_live.sh, then launches the same telemetry trace
# and track map. Perfect for offline development and debugging.
#
# Usage: ./start_replay_live.sh <recording.txt> [DRIVER] [ROUND] [YEAR] [SPEED]
#
#   recording.txt  Path to a recording from recordings/ (required)
#   DRIVER         Driver code for telemetry trace  (default: VER)
#   ROUND          Round name for track geometry    (default: Monaco)
#   YEAR           Season year                      (default: 2026)
#   SPEED          Replay speed multiplier          (default: 1)
#
# Example: ./start_replay_live.sh recordings/session_20260607_083000.txt HAM Monaco 2026 2

set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$DIR/.venv/bin/python"
OPW="$DIR/vendor/open-pit-wall"

if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: venv not found. Run ./setup.sh first." >&2
  exit 1
fi

RECORDING="${1:-}"
if [[ -z "$RECORDING" || ! -f "$RECORDING" ]]; then
  echo "ERROR: pass a recording file as the first argument." >&2
  echo "       Available recordings:" >&2
  ls -1 "$DIR/recordings/" 2>/dev/null | sed 's/^/         /' >&2 || echo "         (none yet)" >&2
  exit 1
fi

DRIVER="${2:-VER}"
ROUND="${3:-Monaco}"
YEAR="${4:-2026}"
SPEED="${5:-1}"

screen -S f1replay -X quit 2>/dev/null || true
sleep 0.3

if lsof -iTCP:8765 -sTCP:LISTEN -n >/dev/null 2>&1; then
  echo "ERROR: port 8765 is already in use — stop the other bridge first." >&2
  exit 1
fi

# 1. Bridge in replay mode
echo "Replaying $RECORDING at ${SPEED}x..."
screen -dmS f1replay bash -c "
  cd '$DIR'
  '$PYTHON' live_opw_bridge.py --replay '$RECORDING' --speed '$SPEED' --loop
  echo '--- replay exited (press enter) ---'
  read -r _
"

# 2. Telemetry trace
echo "Starting telemetry trace for $DRIVER..."
"$PYTHON" "$OPW/examples/driver-telemetry-trace/main.py" --driver "$DRIVER" &
TRACE_PID=$!

# 3. Track map
echo "Starting track map ($ROUND $YEAR)..."
"$PYTHON" "$DIR/opw_track_map.py" --round "$ROUND" --year "$YEAR" &
MAP_PID=$!

cat <<EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  F1 Replay Dashboard (offline, from recording)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Recording   : $RECORDING  (${SPEED}x, looping)
  Telemetry   : $DRIVER  (PID $TRACE_PID)
  Track map   : $ROUND $YEAR  (PID $MAP_PID)
  Replay      : screen session 'f1replay'

  Detach from screen : Ctrl-A D
  Reattach           : screen -r f1replay
  Kill everything    : screen -S f1replay -X quit && kill $TRACE_PID $MAP_PID
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EOF

exec screen -r f1replay
