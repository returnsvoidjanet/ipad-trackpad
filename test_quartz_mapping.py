#!/usr/bin/env python3
"""
Linux/CI-testable check for QuartzInjector's actual CGEvent selection.

test_protocol.py can only prove the HTTP/WebSocket/dispatch layer talks
to MockInjector correctly - QuartzInjector itself is unconstructable on
non-macOS (its __init__ raises unless `import Quartz` already succeeded),
so its real button/event-type/modifier-flag choices have never actually
run anywhere except on someone's Mac by hand.

This closes that gap by installing a fake `Quartz` module (distinct
string sentinels per constant, so any accidental reuse/typo is instantly
visible) into sys.modules *before* importing helper, which lets
QuartzInjector construct normally on Linux. Every CGEventCreateMouseEvent/
CGEventCreateKeyboardEvent + CGEventPost call is recorded, so this proves
- by actually running QuartzInjector's real code, not by reading it -
that:
  - click('left'/'right'), mouse_down/up('left'/'right'), and
    move()-while-dragging always pair the correct CGEvent TYPE with the
    correct CGMouseButton (no left/right swap at the Quartz-call level).
  - swipe(fingers, direction) posts a down+up keyboard event pair with
    the Control modifier flag set, matching SWIPE_KEY_MAP.

Run: python3 test_quartz_mapping.py
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def install_fake_quartz() -> tuple[types.ModuleType, list[dict]]:
    fake = types.ModuleType("Quartz")

    fake.kCGEventLeftMouseDown = "L_DOWN"
    fake.kCGEventLeftMouseUp = "L_UP"
    fake.kCGEventLeftMouseDragged = "L_DRAG"
    fake.kCGEventRightMouseDown = "R_DOWN"
    fake.kCGEventRightMouseUp = "R_UP"
    fake.kCGEventRightMouseDragged = "R_DRAG"
    fake.kCGEventMouseMoved = "MOVE"
    fake.kCGEventOtherMouseDown = "O_DOWN"
    fake.kCGMouseButtonLeft = "BTN_LEFT"
    fake.kCGMouseButtonRight = "BTN_RIGHT"
    fake.kCGMouseButtonCenter = "BTN_CENTER"
    fake.kCGMouseEventClickState = "CLICKSTATE"
    fake.kCGHIDEventTap = "HID_TAP"
    fake.kCGEventFlagMaskCommand = 1 << 20
    fake.kCGEventFlagMaskAlternate = 1 << 19
    fake.kCGEventFlagMaskControl = 1 << 18
    fake.kCGEventFlagMaskShift = 1 << 17
    fake.kCGEventFlagMaskSecondaryFn = 1 << 16

    posted: list[dict] = []

    class FakeEvent:
        def __init__(self, event_type, button=None):
            self.event_type = event_type
            self.button = button
            self.fields: dict = {}

    _MOUSE_TYPES = (
        fake.kCGEventLeftMouseDown, fake.kCGEventLeftMouseUp, fake.kCGEventLeftMouseDragged,
        fake.kCGEventRightMouseDown, fake.kCGEventRightMouseUp, fake.kCGEventRightMouseDragged,
        fake.kCGEventMouseMoved,
    )

    def CGEventCreate(source):
        return FakeEvent("QUERY")

    def CGEventGetLocation(ev):
        class P:
            x = 0.0
            y = 0.0
        return P()

    def CGEventCreateMouseEvent(source, mouse_type, point, mouse_button):
        return FakeEvent(mouse_type, mouse_button)

    def CGEventCreateKeyboardEvent(source, keycode, down):
        ev = FakeEvent("KEY_DOWN" if down else "KEY_UP")
        ev.keycode = keycode
        return ev

    def CGEventSetIntegerValueField(ev, field, value):
        ev.fields[field] = value

    def CGEventSetFlags(ev, flags):
        ev.fields["flags"] = flags

    def CGEventPost(tap, ev):
        posted.append({
            "kind": "mouse" if ev.event_type in _MOUSE_TYPES else "key",
            "event_type": ev.event_type,
            "button": ev.button,
            "flags": ev.fields.get("flags"),
            "keycode": getattr(ev, "keycode", None),
        })

    fake.CGEventCreate = CGEventCreate
    fake.CGEventGetLocation = CGEventGetLocation
    fake.CGEventCreateMouseEvent = CGEventCreateMouseEvent
    fake.CGEventCreateKeyboardEvent = CGEventCreateKeyboardEvent
    fake.CGEventSetIntegerValueField = CGEventSetIntegerValueField
    fake.CGEventSetFlags = CGEventSetFlags
    fake.CGEventPost = CGEventPost
    fake.CGEventKeyboardSetUnicodeString = lambda *a, **k: None

    return fake, posted


def main() -> None:
    fake, posted = install_fake_quartz()
    sys.modules["Quartz"] = fake

    import helper  # noqa: E402  (must import after the fake Quartz is installed)

    assert helper.QUARTZ_AVAILABLE is True, "fake Quartz shim did not register as available"
    inj = helper.QuartzInjector()

    def check_mouse_pair(name, entries, expect_down, expect_up, expect_btn):
        assert len(entries) == 2, f"{name}: expected down+up pair, got {entries}"
        down, up = entries
        assert down["event_type"] == expect_down and down["button"] == expect_btn, f"{name} down: {down}"
        assert up["event_type"] == expect_up and up["button"] == expect_btn, f"{name} up: {up}"

    # -- click('left') / click('right') --
    posted.clear()
    inj.click("left", 1)
    check_mouse_pair("click_left", list(posted), "L_DOWN", "L_UP", "BTN_LEFT")

    posted.clear()
    inj.click("right", 1)
    check_mouse_pair("click_right", list(posted), "R_DOWN", "R_UP", "BTN_RIGHT")

    # -- mouse_down/up('left') / ('right') --
    posted.clear()
    inj.mouse_down("left")
    inj.mouse_up("left")
    check_mouse_pair("down_up_left", list(posted), "L_DOWN", "L_UP", "BTN_LEFT")

    posted.clear()
    inj.mouse_down("right")
    inj.mouse_up("right")
    check_mouse_pair("down_up_right", list(posted), "R_DOWN", "R_UP", "BTN_RIGHT")

    # -- move() while a button is held down: must drag with THAT button --
    inj.mouse_down("left")
    posted.clear()
    inj.move(5, 5)
    move_entries = list(posted)
    assert len(move_entries) == 1, move_entries
    assert move_entries[0]["event_type"] == "L_DRAG" and move_entries[0]["button"] == "BTN_LEFT", move_entries
    posted.clear()
    inj.mouse_up("left")

    inj.mouse_down("right")
    posted.clear()
    inj.move(5, 5)
    move_entries = list(posted)
    assert len(move_entries) == 1, move_entries
    assert move_entries[0]["event_type"] == "R_DRAG" and move_entries[0]["button"] == "BTN_RIGHT", move_entries
    posted.clear()
    inj.mouse_up("right")

    # -- move() with no button held: plain cursor move, not a drag --
    posted.clear()
    inj.move(1, 1)
    move_entries = list(posted)
    assert len(move_entries) == 1, move_entries
    assert move_entries[0]["event_type"] == "MOVE", move_entries

    # -- swipe(): Ctrl+Arrow keystroke matching helper.SWIPE_KEY_MAP --
    CTRL = fake.kCGEventFlagMaskControl
    for direction, (expect_key, expect_mod) in helper.SWIPE_KEY_MAP.items():
        assert expect_mod == "ctrl", f"test assumes ctrl, SWIPE_KEY_MAP now has {expect_mod!r}"
        expect_keycode = helper.KEYCODES[expect_key]
        posted.clear()
        inj.swipe(3, direction)
        entries = list(posted)
        assert len(entries) == 2, f"swipe({direction!r}): expected key down+up, got {entries}"
        down, up = entries
        assert down["kind"] == "key" and down["event_type"] == "KEY_DOWN", down
        assert up["kind"] == "key" and up["event_type"] == "KEY_UP", up
        assert down["keycode"] == expect_keycode, f"swipe({direction!r}) down keycode: {down}"
        assert up["keycode"] == expect_keycode, f"swipe({direction!r}) up keycode: {up}"
        assert down["flags"] == CTRL, f"swipe({direction!r}) down flags (expected Control): {down}"
        assert up["flags"] == CTRL, f"swipe({direction!r}) up flags (expected Control): {up}"

    print("ALL QUARTZ MAPPING CHECKS PASSED")
    print("  - click/mouse_down/mouse_up/move: left always -> Left*/BTN_LEFT,")
    print("    right always -> Right*/BTN_RIGHT, no swap at the Quartz-call level.")
    print("  - swipe(): every SWIPE_KEY_MAP direction posts the correct")
    print("    keycode with the Control modifier flag set.")


if __name__ == "__main__":
    main()
