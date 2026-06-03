#!/usr/bin/env bash
# start_live.sh — launch the full F1 live timing dashboard stack
#
# Usage: ./start_live.sh [DRIVER] [ROUND] [YEAR]
#
#   DRIVER  Driver code for telemetry trace  (default: VER)
#   ROUND   Round name for track geometry    (default: Monaco)
#   YEAR    Season year                      (default: 2026)
#
# Example: ./start_live.sh LEC Monaco 2026

set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$DIR/.venv/bin/python"
OPW="$DIR/vendor/open-pit-wall"

if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: venv not found. Run ./setup.sh first." >&2
  exit 1
fi

DRIVER="${1:-VER}"
ROUND="${2:-Monaco}"
YEAR="${3:-2026}"

screen -S f1live -X quit 2>/dev/null || true
sleep 0.3

# 1. Live OPW bridge in screen
echo "Starting live OPW bridge in screen session 'f1live'..."
screen -dmS f1live bash -c "
  cd '$DIR'
  '$PYTHON' live_opw_bridge.py --record live_session.txt
  echo '--- bridge exited (press enter) ---'
  read -r _
"

# 2. Telemetry trace (OPW example, from submodule) — retries automatically
echo "Starting telemetry trace for $DRIVER..."
"$PYTHON" "$OPW/examples/driver-telemetry-trace/main.py" --driver "$DRIVER" &
TRACE_PID=$!

# 3. Track map — retries connection automatically
echo "Starting track map ($ROUND $YEAR)..."
"$PYTHON" "$DIR/opw_track_map.py" --round "$ROUND" --year "$YEAR" &
MAP_PID=$!

cat <<EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  F1 Live Dashboard
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Telemetry trace : $DRIVER  (PID $TRACE_PID)
  Track map       : $ROUND $YEAR  (PID $MAP_PID)
  Live bridge     : screen session 'f1live'
  Recording to    : $DIR/live_session.txt

  Detach from screen : Ctrl-A D
  Reattach           : screen -r f1live
  Kill everything    : screen -S f1live -X quit && kill $TRACE_PID $MAP_PID
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EOF

exec screen -r f1live
