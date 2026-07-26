"""
Tests for the F1TV auth-visibility feature in live_opw_bridge.py.

Verifies that:
  1. LiveState tracks/clears the auth error and exposes it in snapshots.
  2. check_f1_auth() runs non-interactively (never blocks on browser sign-in).
  3. A running OPWServer tells connected dashboards about auth failures:
     - the welcome message carries auth_error,
     - a status frame is broadcast while unauthenticated,
     - a cleared status frame is broadcast on recovery.

Usage:
    .venv/bin/python test_auth_status.py
"""

import asyncio
import json
import socket

import websockets

from live_opw_bridge import LiveState, OPWServer, check_f1_auth

AUTH_ERROR = "Your F1TV login has EXPIRED — please sign in again."


def free_port() -> int:
    """Ask the OS for a free TCP port so tests never collide with running bridges."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_state_tracks_auth_error():
    state = LiveState()
    assert state.snapshot()["auth_error"] is None

    state.set_auth_error(AUTH_ERROR)
    assert state.snapshot()["auth_error"] == AUTH_ERROR

    state.set_auth_error(None)
    assert state.snapshot()["auth_error"] is None
    print("state tracks auth error: OK")


def test_check_f1_auth_is_non_interactive():
    # Whatever the token situation on this machine, the check must return
    # promptly with None or a reason string — never hang waiting for a browser.
    result = check_f1_auth()
    assert result is None or isinstance(result, str), result
    print(f"check_f1_auth() -> {result!r}: OK")


async def test_server_broadcasts_auth_status():
    state = LiveState()
    state.set_auth_error(AUTH_ERROR)
    port = free_port()
    server = OPWServer(state, host="127.0.0.1", port=port)
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.3)

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            welcome = json.loads(await ws.recv())
            assert welcome["type"] == "welcome", welcome
            assert welcome["auth_error"] == AUTH_ERROR, welcome
            print("welcome carries auth_error: OK")

            msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert msg["type"] == "status", msg
            assert msg["auth_error"] == AUTH_ERROR, msg
            print("status frame broadcast while unauthenticated: OK")

            state.set_auth_error(None)
            msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert msg["type"] == "status", msg
            assert msg["auth_error"] == "", msg
            print("recovery status frame on clear: OK")
    finally:
        task.cancel()


def main():
    test_state_tracks_auth_error()
    test_check_f1_auth_is_non_interactive()
    asyncio.run(test_server_broadcasts_auth_status())
    print("\nAll auth status tests passed.")


if __name__ == "__main__":
    main()
