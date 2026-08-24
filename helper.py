#!/usr/bin/env python3
"""
iPad Trackpad+Keyboard helper.

Runs on the Mac. Serves the PWA (static/) over HTTP and accepts a
WebSocket connection from the iPad at /ws. JSON input messages arriving
on the WebSocket are translated into real macOS input events via Quartz
CGEvent (pyobjc). On non-macOS platforms (e.g. this file is imported on
Linux for testing) the Quartz import fails and a MockInjector is used
instead, which just logs what it would have done. This keeps the
web/transport layer testable off a Mac.

Usage:
    python3 helper.py

Config is read from config.json next to this file (port, sensitivity,
natural_scroll). See README.md for macOS setup (Accessibility + Input
Monitoring permissions are required for real injection to do anything).
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ipad-trackpad")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "port": 8777,
    # Multiplier applied to every incoming move {dx,dy} before injection.
    # The PWA already applies its own touch acceleration curve; this is
    # the "master" sensitivity knob on the Mac side.
    "sensitivity": 1.6,
    # If true, flips scroll direction to match macOS "natural" scrolling.
    "natural_scroll": True,
}


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except (OSError, json.JSONDecodeError):
            logger.exception("failed to read/parse config.json, using defaults")
    return cfg


def get_lan_ips() -> list[str]:
    """Best-effort enumeration of this machine's LAN IPv4 addresses."""
    ips: set[str] = set()

    # Primary outbound-route IP (works even with multiple interfaces).
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass

    # Anything else resolvable via the hostname (picks up extra NICs).
    try:
        hostname = socket.gethostname()
        _, _, addrs = socket.gethostbyname_ex(hostname)
        for ip in addrs:
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass

    return sorted(ips) or ["127.0.0.1"]


# ---------------------------------------------------------------------------
# Injector interface. QuartzInjector (macOS, real) and MockInjector
# (any platform, logs only) implement the same surface so the transport
# layer above never needs to know which one it's talking to.
# ---------------------------------------------------------------------------


class Injector:
    def move(self, dx: float, dy: float) -> None:
        raise NotImplementedError

    def mouse_down(self, button: str) -> None:
        raise NotImplementedError

    def mouse_up(self, button: str) -> None:
        raise NotImplementedError

    def click(self, button: str, count: int = 1) -> None:
        raise NotImplementedError

    def scroll(self, dx: float, dy: float, momentum: bool = False) -> None:
        raise NotImplementedError

    def zoom(self, magnification: float) -> None:
        raise NotImplementedError

    def swipe(self, fingers: int, direction: str) -> None:
        raise NotImplementedError

    def key(self, key: str, modifiers: list[str] | None, action: str = "press") -> None:
        raise NotImplementedError

    def text(self, string: str) -> None:
        raise NotImplementedError


class MockInjector(Injector):
    """Used on any non-macOS platform (and in tests). Logs every action
    instead of touching real input, so the WS/HTTP layer is testable on
    Linux/CI without Quartz."""

    def __init__(self) -> None:
        self.log: list[dict[str, Any]] = []

    def _record(self, action_name: str, **kwargs: Any) -> None:
        entry = {"action": action_name, **kwargs}
        self.log.append(entry)
        logger.info("[MOCK] %s %s", action_name, kwargs)

    def move(self, dx: float, dy: float) -> None:
        self._record("move", dx=dx, dy=dy)

    def mouse_down(self, button: str) -> None:
        self._record("mouse_down", button=button)

    def mouse_up(self, button: str) -> None:
        self._record("mouse_up", button=button)

    def click(self, button: str, count: int = 1) -> None:
        self._record("click", button=button, count=count)

    def scroll(self, dx: float, dy: float, momentum: bool = False) -> None:
        self._record("scroll", dx=dx, dy=dy, momentum=momentum)

    def zoom(self, magnification: float) -> None:
        self._record("zoom", magnification=magnification)

    def swipe(self, fingers: int, direction: str) -> None:
        self._record("swipe", fingers=fingers, direction=direction)

    def key(self, key: str, modifiers: list[str] | None, action: str = "press") -> None:
        # NB: kwarg is named press_action, not action - the log entry's
        # own bookkeeping field is called "action" (== "key" here), and
        # that would silently collide with (and get overwritten by) a
        # same-named "action" kwarg when merged into the entry dict.
        self._record("key", key=key, modifiers=modifiers or [], press_action=action)

    def text(self, string: str) -> None:
        self._record("text", string=string)


# Standard macOS ANSI-US virtual keycodes (Carbon HIToolbox table).
# Letters/digits assume a US physical layout; this is a known v1
# limitation (documented in README).
KEYCODES: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29,
    "equal": 24, "=": 24, "minus": 27, "-": 27,
    "rightbracket": 30, "]": 30, "leftbracket": 33, "[": 33,
    "o": 31, "u": 32, "i": 34, "p": 35,
    "return": 36, "enter": 36, "l": 37, "j": 38, "k": 40,
    "quote": 39, "'": 39, "semicolon": 41, ";": 41, "backslash": 42, "\\": 42,
    "comma": 43, ",": 43, "n": 45, "m": 46, "period": 47, ".": 47, "slash": 44, "/": 44,
    "tab": 48, "space": 49, "grave": 50, "`": 50,
    "delete": 51, "backspace": 51, "escape": 53, "esc": 53,
    "command": 55, "cmd": 55, "shift": 56, "capslock": 57, "option": 58, "opt": 58,
    "control": 59, "ctrl": 59, "rightshift": 60, "rightoption": 61, "rightcontrol": 62,
    "function": 63, "fn": 63,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "home": 115, "pageup": 116, "forwarddelete": 117, "end": 119, "pagedown": 121,
    "leftarrow": 123, "left": 123, "rightarrow": 124, "right": 124,
    "downarrow": 125, "down": 125, "uparrow": 126, "up": 126,
}

try:
    import Quartz  # type: ignore

    QUARTZ_AVAILABLE = True
except ImportError:
    QUARTZ_AVAILABLE = False


class QuartzInjector(Injector):
    """Real macOS input injection via Quartz CGEvent. Only constructed
    when `import Quartz` succeeds (i.e. we're actually on macOS with
    pyobjc-framework-Quartz installed)."""

    MODIFIER_FLAGS: dict[str, int]

    def __init__(self) -> None:
        if not QUARTZ_AVAILABLE:
            raise RuntimeError("Quartz is not available on this platform")
        self.MODIFIER_FLAGS = {
            "cmd": Quartz.kCGEventFlagMaskCommand,
            "command": Quartz.kCGEventFlagMaskCommand,
            "opt": Quartz.kCGEventFlagMaskAlternate,
            "option": Quartz.kCGEventFlagMaskAlternate,
            "alt": Quartz.kCGEventFlagMaskAlternate,
            "ctrl": Quartz.kCGEventFlagMaskControl,
            "control": Quartz.kCGEventFlagMaskControl,
            "shift": Quartz.kCGEventFlagMaskShift,
            "fn": Quartz.kCGEventFlagMaskSecondaryFn,
        }
        self._button_down: str | None = None  # "left" | "right" | None
        self._zoom_accum = 0.0

    # -- helpers --

    def _current_location(self):
        ev = Quartz.CGEventCreate(None)
        return Quartz.CGEventGetLocation(ev)

    def _post_mouse(self, event_type, point, button) -> None:
        ev = Quartz.CGEventCreateMouseEvent(None, event_type, point, button)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    # -- mouse / trackpad --

    def move(self, dx: float, dy: float) -> None:
        loc = self._current_location()
        new_point = (loc.x + dx, loc.y + dy)
        if self._button_down == "left":
            self._post_mouse(Quartz.kCGEventLeftMouseDragged, new_point, Quartz.kCGMouseButtonLeft)
        elif self._button_down == "right":
            self._post_mouse(Quartz.kCGEventRightMouseDragged, new_point, Quartz.kCGMouseButtonRight)
        else:
            self._post_mouse(Quartz.kCGEventMouseMoved, new_point, Quartz.kCGMouseButtonLeft)

    def mouse_down(self, button: str) -> None:
        self._button_down = button
        loc = self._current_location()
        if button == "right":
            self._post_mouse(Quartz.kCGEventRightMouseDown, loc, Quartz.kCGMouseButtonRight)
        else:
            self._post_mouse(Quartz.kCGEventLeftMouseDown, loc, Quartz.kCGMouseButtonLeft)

    def mouse_up(self, button: str) -> None:
        loc = self._current_location()
        if button == "right":
            self._post_mouse(Quartz.kCGEventRightMouseUp, loc, Quartz.kCGMouseButtonRight)
        else:
            self._post_mouse(Quartz.kCGEventLeftMouseUp, loc, Quartz.kCGMouseButtonLeft)
        self._button_down = None

    def click(self, button: str, count: int = 1) -> None:
        loc = self._current_location()
        right = button == "right"
        down_type = Quartz.kCGEventRightMouseDown if right else Quartz.kCGEventLeftMouseDown
        up_type = Quartz.kCGEventRightMouseUp if right else Quartz.kCGEventLeftMouseUp
        btn = Quartz.kCGMouseButtonRight if right else Quartz.kCGMouseButtonLeft

        down = Quartz.CGEventCreateMouseEvent(None, down_type, loc, btn)
        Quartz.CGEventSetIntegerValueField(down, Quartz.kCGMouseEventClickState, max(1, count))
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)

        up = Quartz.CGEventCreateMouseEvent(None, up_type, loc, btn)
        Quartz.CGEventSetIntegerValueField(up, Quartz.kCGMouseEventClickState, max(1, count))
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

    def scroll(self, dx: float, dy: float, momentum: bool = False) -> None:
        # wheel1 = vertical, wheel2 = horizontal. Pixel units so the PWA's
        # own momentum/easing translates 1:1 into scroll distance.
        ev = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitPixel, 2, int(dy), int(dx)
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    def zoom(self, magnification: float) -> None:
        # There is no public, reliably-synthesizable CGEvent constructor
        # for the pinch-to-zoom gesture event in Quartz/pyobjc. Fall back
        # to the documented keyboard equivalent (Cmd+= / Cmd+-), firing
        # once per accumulated threshold of pinch travel so a continuous
        # pinch doesn't spam keystrokes.
        self._zoom_accum += magnification
        threshold = 0.15
        while self._zoom_accum >= threshold:
            self.key("=", ["cmd"], "press")
            self._zoom_accum -= threshold
        while self._zoom_accum <= -threshold:
            self.key("-", ["cmd"], "press")
            self._zoom_accum += threshold

    def swipe(self, fingers: int, direction: str) -> None:
        # Mapped to macOS Mission Control's default keyboard shortcuts
        # (Ctrl+Left/Right = Spaces, Ctrl+Up = Mission Control,
        # Ctrl+Down = App Exposé) rather than synthesizing a gesture
        # event, since those bindings are stable regardless of the
        # user's trackpad gesture settings. Used for both 3- and
        # 4-finger swipes.
        mapping = {
            "left": ("leftarrow", "ctrl"),
            "right": ("rightarrow", "ctrl"),
            "up": ("uparrow", "ctrl"),
            "down": ("downarrow", "ctrl"),
        }
        target = mapping.get(direction)
        if target is None:
            logger.warning("unknown swipe direction: %r", direction)
            return
        key, mod = target
        self.key(key, [mod], "press")

    # -- keyboard --

    def key(self, key: str, modifiers: list[str] | None, action: str = "press") -> None:
        modifiers = modifiers or []
        keycode = KEYCODES.get(str(key).lower())
        if keycode is None:
            logger.warning("unknown key: %r", key)
            return
        flags = 0
        for m in modifiers:
            flags |= self.MODIFIER_FLAGS.get(m, 0)

        def _post(down: bool) -> None:
            ev = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
            if flags:
                Quartz.CGEventSetFlags(ev, flags)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

        if action in ("press", "down"):
            _post(True)
        if action in ("press", "up"):
            _post(False)

    def text(self, string: str) -> None:
        # Bypasses the keycode/layout table entirely by setting the
        # unicode string directly on a keycode-0 keyboard event, so
        # literal typing works regardless of physical layout.
        for ch in string:
            down = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
            Quartz.CGEventKeyboardSetUnicodeString(down, len(ch), ch)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)

            up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
            Quartz.CGEventKeyboardSetUnicodeString(up, len(ch), ch)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def make_injector() -> Injector:
    if QUARTZ_AVAILABLE:
        logger.info("Quartz available - using QuartzInjector (real macOS input injection)")
        return QuartzInjector()
    logger.warning(
        "Quartz not available - running in MOCK mode (not macOS). "
        "Input messages will be logged, not injected."
    )
    return MockInjector()


# ---------------------------------------------------------------------------
# Protocol dispatch: JSON message -> Injector call.
# ---------------------------------------------------------------------------


def dispatch(injector: Injector, msg: dict[str, Any], cfg: dict[str, Any]) -> None:
    t = msg.get("type")
    if t == "move":
        dx = float(msg.get("dx", 0)) * cfg["sensitivity"]
        dy = float(msg.get("dy", 0)) * cfg["sensitivity"]
        injector.move(dx, dy)
    elif t == "down":
        injector.mouse_down(msg.get("button", "left"))
    elif t == "up":
        injector.mouse_up(msg.get("button", "left"))
    elif t == "click":
        injector.click(msg.get("button", "left"), int(msg.get("count", 1)))
    elif t == "scroll":
        dx = float(msg.get("dx", 0))
        dy = float(msg.get("dy", 0))
        if cfg.get("natural_scroll", True):
            dx, dy = -dx, -dy
        injector.scroll(dx, dy, bool(msg.get("momentum", False)))
    elif t == "zoom":
        injector.zoom(float(msg.get("magnification", 0)))
    elif t == "swipe":
        injector.swipe(int(msg.get("fingers", 3)), msg.get("dir", ""))
    elif t == "key":
        injector.key(msg.get("key", ""), msg.get("modifiers", []), msg.get("action", "press"))
    elif t == "text":
        injector.text(msg.get("string", ""))
    else:
        logger.warning("unknown message type: %r", t)


# ---------------------------------------------------------------------------
# aiohttp app: static PWA + single WebSocket endpoint, one port.
# ---------------------------------------------------------------------------


async def index_handler(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    injector: Injector = request.app["injector"]
    cfg: dict[str, Any] = request.app["config"]

    peer = request.remote
    logger.info("client connected: %s", peer)

    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                logger.warning("bad JSON from client: %r", msg.data[:200])
                continue
            try:
                dispatch(injector, data, cfg)
            except Exception:
                logger.exception("error dispatching message: %r", data)
        elif msg.type == WSMsgType.ERROR:
            logger.warning("ws connection error: %s", ws.exception())

    logger.info("client disconnected: %s", peer)
    return ws


def build_app() -> web.Application:
    cfg = load_config()
    injector = make_injector()

    app = web.Application()
    app["config"] = cfg
    app["injector"] = injector

    app.router.add_get("/", index_handler)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/", path=STATIC_DIR, show_index=False)

    return app


def main() -> None:
    app = build_app()
    cfg = app["config"]
    port = int(cfg["port"])

    mode = "MOCK (not macOS)" if isinstance(app["injector"], MockInjector) else "Quartz (macOS)"
    print("=" * 60, flush=True)
    print("iPad Trackpad helper", flush=True)
    print(f"Injector mode: {mode}", flush=True)
    print(f"sensitivity={cfg['sensitivity']}  natural_scroll={cfg['natural_scroll']}", flush=True)
    print(flush=True)
    print("Open one of these on your iPad (same Wi-Fi as this Mac):", flush=True)
    for ip in get_lan_ips():
        print(f"  http://{ip}:{port}", flush=True)
    print("=" * 60, flush=True)

    web.run_app(app, port=port, print=None)


if __name__ == "__main__":
    main()
