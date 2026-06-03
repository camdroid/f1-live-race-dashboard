#!/usr/bin/env bash
# start_replay.sh — launch the full F1 replay dashboard stack
#
# Usage: ./start_replay.sh [DRIVER] [ROUND] [YEAR]
#
#   DRIVER  Driver code for telemetry trace  (default: VER)
#   ROUND   Round name for track geometry    (default: Canadian Grand Prix)
#   YEAR    Season year                      (default: 2026)
#
# Example: ./start_replay.sh LEC Monaco 2026

set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$DIR/.venv/bin/python"
OPW="$DIR/vendor/open-pit-wall"

if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: venv not found. Run ./setup.sh first." >&2
  exit 1
fi

DRIVER="${1:-VER}"
ROUND="${2:-Canadian Grand Prix}"
YEAR="${3:-2026}"

# Kill any leftover session
screen -S opw -X quit 2>/dev/null || true
sleep 0.3

# 1. OPW broadcaster in screen (interactive — navigate menu, then type 'play')
echo "Starting OPW broadcaster in screen session 'opw'..."
screen -dmS opw bash -c "
  cd '$OPW'
  '$PYTHON' -m open_pit_wall
  echo '--- broadcaster exited (press enter) ---'
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
  F1 Replay Dashboard
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Telemetry trace : $DRIVER  (PID $TRACE_PID)
  Track map       : $ROUND $YEAR  (PID $MAP_PID)
  Broadcaster     : screen session 'opw'

  Navigate the OPW menu to select a session, then
  choose "Play saved data" — dashboards connect automatically.

  Broadcaster controls: play / pause / speed <n> / quit
  Detach from screen  : Ctrl-A D
  Reattach            : screen -r opw
  Kill everything     : screen -S opw -X quit && kill $TRACE_PID $MAP_PID
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EOF

exec screen -r opw
