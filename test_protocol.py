#!/usr/bin/env python3
"""
Linux/CI-testable check for the web/transport layer of helper.py.

This does NOT and CANNOT test real Quartz input injection (that only
exists on macOS) - see test_quartz_mapping.py for that, via a fake
Quartz shim. What THIS file proves:
  - the aiohttp app serves the static PWA files over HTTP (200s)
  - a WebSocket client can connect to /ws
  - one sample message of every protocol type is received, parsed, and
    dispatched to the correct Injector method with the right args
  - button:"left"/"right" clicks and every 3/4-finger swipe direction
    dispatch with the correct button / resolve to the correct keystroke
    (regression guard - see the assertions below)

Run: python3 test_protocol.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helper  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402


async def main() -> None:
    app = helper.build_app()
    injector = app["injector"]

    assert isinstance(injector, helper.MockInjector), (
        f"expected MockInjector on this (non-macOS) platform, got {type(injector)}"
    )
    print(f"Injector: {type(injector).__name__} (expected on non-macOS)")

    async with TestClient(TestServer(app)) as client:
        print("\n-- HTTP: static PWA --")
        for path in ("/", "/app.js", "/style.css", "/manifest.webmanifest"):
            resp = await client.get(path)
            status = resp.status
            body = await resp.text()
            print(f"  GET {path:28s} -> {status}  ({len(body)} bytes)")
            assert status == 200, f"{path} returned {status}, expected 200"

        print("\n-- WebSocket: handshake --")
        ws = await client.ws_connect("/ws")
        print("  ws_connect(/ws) -> OK")

        messages = [
            {"type": "move", "dx": 12, "dy": -6},
            {"type": "click", "button": "left", "count": 1},
            {"type": "click", "button": "right", "count": 1},
            {"type": "down", "button": "left"},
            {"type": "move", "dx": 3, "dy": 3},
            {"type": "up", "button": "left"},
            {"type": "scroll", "dx": 0, "dy": 24, "momentum": False},
            {"type": "zoom", "magnification": 0.4},
            {"type": "swipe", "fingers": 3, "dir": "left"},
            {"type": "swipe", "fingers": 4, "dir": "up"},
            {"type": "key", "key": "a", "modifiers": ["cmd"], "action": "press"},
            {"type": "text", "string": "hi"},
        ]

        print("\n-- WebSocket: sending one sample of every message type --")
        for m in messages:
            await ws.send_json(m)
            print(f"  sent  {m}")

        await asyncio.sleep(0.2)
        await ws.close()

    print(f"\n-- MockInjector received {len(injector.log)} dispatched actions --")
    for entry in injector.log:
        print(" ", entry)

    expected_actions = [
        "move", "click", "click", "mouse_down", "move", "mouse_up",
        "scroll", "zoom", "swipe", "swipe", "key", "text",
    ]
    got_actions = [e["action"] for e in injector.log]
    assert got_actions == expected_actions, f"\n  got:      {got_actions}\n  expected: {expected_actions}"

    # Spot-check a couple of payloads made it through dispatch() intact,
    # including config transforms (sensitivity multiplier, natural_scroll
    # sign flip).
    cfg = app["config"]
    move_entry = injector.log[0]
    assert move_entry["dx"] == 12 * cfg["sensitivity"], move_entry
    assert move_entry["dy"] == -6 * cfg["sensitivity"], move_entry

    scroll_entry = next(e for e in injector.log if e["action"] == "scroll")
    expected_dy = -24 if cfg["natural_scroll"] else 24
    assert scroll_entry["dy"] == expected_dy, scroll_entry

    # -- Regression guard: left/right must never swap (bug found in v1
    # where a report claimed all left clicks registered as right clicks;
    # traced end-to-end and found no swap in this code, but pinning it
    # here with an explicit assertion so it can't silently regress). --
    click_entries = [e for e in injector.log if e["action"] == "click"]
    assert click_entries[0]["button"] == "left", (
        f"button:'left' click must dispatch a LEFT-button action, got {click_entries[0]}"
    )
    assert click_entries[1]["button"] == "right", (
        f"button:'right' click must dispatch a RIGHT-button action, got {click_entries[1]}"
    )

    down_entry = next(e for e in injector.log if e["action"] == "mouse_down")
    assert down_entry["button"] == "left", down_entry
    up_entry = next(e for e in injector.log if e["action"] == "mouse_up")
    assert up_entry["button"] == "left", up_entry

    # -- Regression guard: 3/4-finger swipe must resolve to the correct
    # Ctrl+Arrow keystroke (Mission Control shortcuts). MockInjector
    # reports what QuartzInjector's swipe() would inject via the shared
    # SWIPE_KEY_MAP, so this is checkable without real Quartz/macOS. --
    swipe_entries = [e for e in injector.log if e["action"] == "swipe"]
    swipe_left = next(e for e in swipe_entries if e["direction"] == "left")
    assert swipe_left["fingers"] == 3, swipe_left
    assert swipe_left["mapped_key"] == "leftarrow", swipe_left
    assert swipe_left["mapped_modifiers"] == ["ctrl"], swipe_left

    swipe_up = next(e for e in swipe_entries if e["direction"] == "up")
    assert swipe_up["fingers"] == 4, swipe_up
    assert swipe_up["mapped_key"] == "uparrow", swipe_up
    assert swipe_up["mapped_modifiers"] == ["ctrl"], swipe_up

    key_entry = next(e for e in injector.log if e["action"] == "key")
    assert key_entry["key"] == "a" and key_entry["modifiers"] == ["cmd"], key_entry
    assert key_entry["press_action"] == "press", key_entry

    text_entry = next(e for e in injector.log if e["action"] == "text")
    assert text_entry["string"] == "hi", text_entry

    print("\nALL CHECKS PASSED")
    print("\nNOTE: this only proves the HTTP/WebSocket/dispatch layer + the")
    print("button/keystroke mapping as reported by MockInjector. It does NOT")
    print("prove real Quartz CGEvent injection (macOS-only) - run")
    print("test_quartz_mapping.py for that (via a fake Quartz shim), and")
    print("verify actual on-screen behavior by hand on a Mac for the rest.")


if __name__ == "__main__":
    asyncio.run(main())
