#!/usr/bin/env bash
# start_live.sh — launch the full F1 live timing dashboard stack
#
# Usage: ./start_live.sh [DRIVER] [ROUND] [YEAR] [DELAY]
#
#   DRIVER  Driver code for telemetry trace  (default: VER)
#   ROUND   Round name for track geometry    (default: Monaco)
#   YEAR    Season year                      (default: 2026)
#   DELAY   Run dashboards this far behind the live feed (default: 0)
#           e.g. 90, 90s, 30m, 1.5h — for when your broadcast lags real-time
#
# Example: ./start_live.sh LEC Monaco 2026
# Example: ./start_live.sh VER Monaco 2026 30m   # started watching 30 min late

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
DELAY="${4:-0}"

screen -S f1live -X quit 2>/dev/null || true
sleep 0.3

# Guard: if something is still bound to 8765 (e.g. a manually-run bridge),
# abort rather than launch a second bridge — two bridges sharing a record
# file produces a corrupted, half-NUL recording.
if lsof -iTCP:8765 -sTCP:LISTEN -n >/dev/null 2>&1; then
  echo "ERROR: port 8765 is already in use — another bridge is running." >&2
  echo "       Stop it first (Ctrl-C in its terminal, or: lsof -iTCP:8765)." >&2
  exit 1
fi

# Each run records to its own timestamped file so concurrent/old runs can
# never clobber the same recording.
mkdir -p "$DIR/recordings"
RECORDING="$DIR/recordings/session_$(date +%Y%m%d_%H%M%S).txt"

# 1. Live OPW bridge in screen
echo "Starting live OPW bridge in screen session 'f1live'..."
echo "Recording to: $RECORDING"
DELAY_ARGS=()
if [[ "$DELAY" != "0" ]]; then
  DELAY_ARGS=(--delay "$DELAY")
  echo "Dashboards will run ${DELAY} behind the live feed"
fi

screen -dmS f1live bash -c "
  cd '$DIR'
  '$PYTHON' live_opw_bridge.py --record '$RECORDING' ${DELAY_ARGS[*]}
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
  Recording to    : $RECORDING
  Feed delay      : ${DELAY}

  Detach from screen : Ctrl-A D
  Reattach           : screen -r f1live
  Kill everything    : screen -S f1live -X quit && kill $TRACE_PID $MAP_PID
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EOF

exec screen -r f1live
