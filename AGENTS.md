# AGENTS.md

Guidance for AI agents (and new humans) working in this repo.

## What this is

A set of F1 live-timing dashboards built on [FastF1](https://docs.fastf1.dev) and
Tom Shaw's Open Pit Wall (OPW). The core piece is a bridge that connects to the
official F1 SignalR live-timing feed, decodes the compressed `CarData.z` /
`Position.z` channels, and re-broadcasts everything in OPW's WebSocket protocol
on **port 8765** so OPW dashboards work during a live race. Around it: a Rich
terminal timing board, a PySide6 (Qt) track map, and record/replay tooling so
everything can be developed offline.

This is a personal hobby project: no CI, no linter, and (historically) no
tests — but see **Testing** below: new work should add them. Season defaults
are 2026 / Monaco.

## Layout

| Path | What it is |
|---|---|
| `live_opw_bridge.py` | The heart of the repo. SignalR → OPW WebSocket bridge (port 8765). Also handles `--record FILE`, `--replay FILE --speed N --loop`, `--no-auth`. |
| `pit_wall.py` | Rich TUI live timing board. Also imported as a library by `replay_past_race.py` (`TimingState`, `build_timing_table`, `build_race_control_panel`). |
| `opw_track_map.py` | Qt track map fed from the bridge's WebSocket. Circuit geometry from FastF1; imports `_TrackMapWidget` from `vendor/f1-race-replay` by appending it to `sys.path`. |
| `replay_past_race.py` | Animates a completed session from the FastF1 API through the pit-wall display. |
| `live_timing.py`, `replay_session.py` | Small standalone FastF1 scripts (raw feed recorder / recording analyzer). Mostly superseded by the bridge but kept as simple examples. |
| `start_live.sh` / `start_replay_live.sh` / `start_replay.sh` | Launchers (see below). |
| `vendor/open-pit-wall`, `vendor/f1-race-replay` | **Git submodules of upstream repos — do not edit.** OPW is `pip install -e`'d; f1-race-replay is imported via `sys.path`. If a change seems needed there, wrap or monkey-patch from this repo's code instead, and log it (see **Suspected upstream bugs**). |
| `vendor-issues.md` | Running log of suspected bugs/flakiness in the vendored submodules, kept bug-report-ready (created on first entry — see **Suspected upstream bugs**). |
| `recordings/` | Gitignored, but contains real captured sessions — the test fixtures for offline work. |
| `cache/`, `.fastf1-cache/` | FastF1 caches, gitignored. Safe to delete; slow to rebuild. |

## Setup and environment

```bash
./setup.sh        # once: inits submodules, creates .venv, installs deps + OPW
```

- Always use **`.venv/bin/python`** — scripts hardcode it and there is no
  activation step. The `env/` directory is a stale second venv; ignore it.
- Dependencies live in `requirements.txt` (fastf1, rich, websockets,
  signalrcore, PySide6, matplotlib, pandas, numpy, scipy, questionary,
  requests). No lockfile.
- Python 3.14 in Docker; whatever `python3` is locally.

## Running things

```bash
./start_live.sh [DRIVER] [ROUND] [YEAR]                    # live race weekend
./start_replay_live.sh recordings/<file> [DRIVER] [ROUND] [YEAR] [SPEED]   # offline dev
./start_replay.sh [DRIVER] [ROUND] [YEAR]                  # OPW's own saved-data replay
```

Conventions the launchers rely on:

- The bridge (or OPW broadcaster) runs inside a **`screen`** session
  (`f1live` / `f1replay` / `opw`); the telemetry trace and track map run as
  background processes of the script. The script ends with `exec screen -r`,
  so it takes over the terminal — don't run launchers from an agent shell
  expecting them to return. To exercise components yourself, run the Python
  entry points directly instead.
- **Only one bridge may bind port 8765.** `start_live.sh` refuses to start if
  the port is taken — two bridges writing one recording file corrupts it.
  Check with `lsof -iTCP:8765 -sTCP:LISTEN`.
- Live runs record to `recordings/session_<timestamp>.txt` automatically.
- The Qt apps (`opw_track_map.py`, the OPW telemetry trace) need a display;
  they retry their WebSocket connection automatically, so start order doesn't
  matter much.

Docker (`Dockerfile`, `docker-compose.yml`) provides headless variants:
`pitwall` / `bridge` / `replay` services, `web-pitwall` / `web-replay` serving
the TUIs in a browser via ttyd (ports 7681/7682, no auth — meant to sit behind
a reverse proxy), and `trackmap` via X11 forwarding.

## Testing

There is no test suite yet — that's a gap, not a convention. **Any new
feature or bug fix should come with tests covering it as well as is
feasible.** Guidelines:

- Put tests in `tests/` and use `pytest` (add it to `requirements.txt` when
  creating the first test). Run with `.venv/bin/python -m pytest`.
- The most testable seams are the pure decode/transform layers: SignalR
  message decoding (`CarData.z`/`Position.z` inflate + parse), the
  OPW-protocol message building in `live_opw_bridge.py`, and the state/table
  builders in `pit_wall.py` (`TimingState` etc.). Prefer testing those
  directly over driving the TUIs/Qt apps.
- Real captured feeds in `recordings/` make good fixture material — snip a
  few representative lines (including the pathological ones: zeroed
  positions, partial payloads) into small fixture files under
  `tests/fixtures/` rather than referencing the gitignored full recordings.
- A bug fix should include a regression test that fails without the fix,
  whenever the bug can be reproduced at one of the seams above. If it truly
  can't (e.g. live-connection-only behavior), say so in the commit message.

## How to verify changes

Beyond unit tests, verify end-to-end by replaying a real recording through
the stack:

```bash
.venv/bin/python live_opw_bridge.py --replay recordings/<newest>.txt --speed 10
# then, in parallel, whatever component you changed, e.g.:
.venv/bin/python opw_track_map.py --round Monaco --year 2026
```

This is the whole point of the record/replay feature (commit `b47747d`) — use
it rather than waiting for a live session. `pit_wall.py` display changes can
also be exercised with `replay_past_race.py` (needs network for the FastF1 API
on first run; cached afterwards).

## Suspected upstream bugs (vendor flakiness)

If behavior that looks like a bug or flakiness traces into one of the
vendored submodules (`vendor/open-pit-wall`, `vendor/f1-race-replay`) rather
than this repo's code, **don't just work around it silently** — add an entry
to `vendor-issues.md` at the repo root (create the file on first use). The
goal is that each entry can later be reproduced and turned into an upstream
bug report with no archaeology. Capture:

- **Submodule + pinned commit**: `git -C vendor/<name> rev-parse HEAD`, plus
  the upstream URL (from `.gitmodules`).
- **Date observed and context**: live session vs replay, which launcher /
  entry point, and the session (e.g. "Monaco 2026 R").
- **Symptom**: expected vs actual, and whether it's deterministic or flaky
  (roughly how often it reproduces).
- **Repro**: the smallest command that triggers it — ideally a replay
  command against a named file in `recordings/` (note the approximate
  timestamp/offset in the recording where it occurs). If the triggering
  recording is important, note that it must be kept (recordings are
  gitignored and could otherwise be cleaned up).
- **Evidence**: exact stack trace / log lines, verbatim.
- **Environment**: OS, Python version, and versions of the relevant deps
  (`.venv/bin/pip show fastf1 signalrcore websockets PySide6 ...`).
- **Our workaround**, if any: where the wrapper/monkey-patch lives in this
  repo (`file.py:line`) so it can be removed once fixed upstream.
- **Status**: `suspected` → `confirmed` (reliably reproduced) → `reported`
  (link the upstream issue) → `fixed upstream`.

Before logging, sanity-check that the fault isn't actually in this repo's
glue code or in bad live-feed data — the F1 feed itself is flaky (see
Gotchas), and that's not an upstream vendor bug.

## Gotchas

- **Live-feed data is defensive territory.** The real feed sends zeroed
  position frames, partial payloads, and mid-session reconnects; recent
  history is full of fixes for these. Handle missing/zero data gracefully and
  surface a note in the UI rather than crashing (see `f765f65`).
- CarData channel indices and the 1/10-metre position scale are defined at the
  top of `live_opw_bridge.py` — reuse those constants.
- Live auth goes through `fastf1.internals.f1auth.get_auth_token` (an internal
  FastF1 API — may break on FastF1 upgrades). `--no-auth` gives partial data.
- Driver args are three-letter codes (VER, LEC, HAM); rounds are FastF1 event
  names ("Monaco", "Canadian Grand Prix").
- Shell scripts follow `set -euo pipefail` + absolute-`$DIR` style; keep new
  scripts consistent.

## Git conventions

- Commit messages are plain imperative one-liners describing user-visible
  behavior ("Show note when position feed returns zeroed coordinates").
- Work directly on `main`; no PR flow.
- Never commit `recordings/`, caches, or venvs (already gitignored).
