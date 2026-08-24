# iPad Trackpad + Keyboard (v1)

Turns an iPad into a wireless trackpad + keyboard for a Mac. Architecture:
a small Python server (`helper.py`) runs **on your Mac**, serves a PWA to
the iPad over your LAN, and injects real macOS input events (mouse moves,
clicks, scrolls, zoom, swipes, keystrokes) via Quartz when messages arrive
over a WebSocket. No cloud, no App Store, nothing leaves your LAN.

v1 scope: trackpad + keyboard only. No macros, no notes, no lab-specific
features.

## Setup (on the Mac)

```sh
cd ipad-trackpad
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 helper.py
```

On first run it'll print something like:

```
============================================================
iPad Trackpad helper
Injector mode: Quartz (macOS)
sensitivity=1.6  natural_scroll=True

Open one of these on your iPad (same Wi-Fi as this Mac):
  http://10.0.1.42:8777
============================================================
```

### Grant permissions (do this BEFORE it'll actually work)

Go to **System Settings → Privacy & Security** and grant these to
**Terminal** (or whatever runs `python3` — if you run it from a different
app/IDE, grant that app instead):

1. **Accessibility** — required for Quartz to post mouse/keyboard events.
2. **Input Monitoring** — ⚠️ **this is the one people miss.** Without it,
   clicks and scrolling may appear to work but **typing does nothing** —
   keystrokes get silently dropped with no error printed. If key presses
   aren't landing, this permission is almost always why.

After granting either permission for the first time, macOS usually
requires you to quit and re-run `python3 helper.py` (sometimes it asks you
to restart Terminal itself) for it to take effect.

## Use it (on the iPad)

1. Make sure the iPad is on the **same Wi-Fi network** as the Mac (this is
   LAN-only; it will not work over cellular or a different network).
2. Open the printed `http://<mac-lan-ip>:8777` URL in Safari.
3. Tap the Share icon → **Add to Home Screen** to install it as a
   fullscreen app (recommended — avoids Safari's UI chrome eating screen
   space and gesture area).
4. Open it from the home screen. The status dot at top turns green when
   connected to the helper.

If the Mac sleeps or you leave Wi-Fi range, the dot turns red and the app
auto-reconnects (with backoff) once the helper is reachable again — no
need to reload the page, though reloading also works.

## Config

Edit `config.json` next to `helper.py` (restart the helper after changing
it):

```json
{
  "port": 8777,
  "sensitivity": 1.6,
  "natural_scroll": true
}
```

- `sensitivity` — multiplier applied to every cursor-move delta before
  injection. Raise for a faster cursor, lower for more precision.
- `natural_scroll` — matches macOS's "natural" (content-follows-finger)
  scroll direction. Flip if scrolling feels backwards.

## Gestures (trackpad tab)

- **1 finger, move** — move the cursor (with acceleration — small/slow
  movements are precise, fast flicks travel further, tuned to feel like a
  Magic Trackpad).
- **1 finger, quick tap** — left click.
- **1 finger, tap-and-hold (~320ms) then drag** — press-and-drag (e.g. to
  select text or drag a window/file); releases on lift.
- **2 fingers, drag** — scroll, with inertial momentum after release.
- **2 fingers, quick tap** — right click.
- **Pinch** — zoom. (See "Zoom, honestly" below.)
- **3 or 4 finger swipe** (left/right/up/down) — mapped to macOS's default
  Mission Control keyboard shortcuts: Left/Right = Ctrl+Left/Right
  (switch Spaces), Up = Ctrl+Up (Mission Control), Down = Ctrl+Down (App
  Exposé). Chosen over synthesizing a gesture event because these keyboard
  bindings are stable regardless of the user's own trackpad gesture
  settings in System Settings — a synthesized gesture event would depend
  on the user having matching settings enabled.

## Keyboard tab

Full on-screen keyboard: letters, numbers, symbols, arrows, tab/esc/
return/delete. The modifier keys (⌘ ⌥ ⌃ ⇧ fn) are **sticky**: tap one (or
several) to arm them — they highlight — then tap a regular key to send
that key combined with every armed modifier; the modifiers then clear
automatically. This is how you send shortcuts like ⌘C or ⌘⇧4.

There's also a plain text field below the keyboard grid for fast literal
typing (handles autocorrect/paste reasonably — appended characters are
sent as `text`, deletions as `delete` keypresses).

**Known v1 limitation**: letter/symbol keys assume a **US ANSI physical
keyboard layout** on the Mac (standard `kVK_ANSI_*` virtual keycodes).
Literal text typed into the text field bypasses this (it's injected via
unicode string, not keycode, so it works regardless of layout) — only the
keyboard *grid* buttons and shortcut combos are layout-assumed.

## Zoom, honestly

There's no public, reliably synthesizable Quartz API for the actual pinch
magnify gesture event (`CGEventCreateMagnificationEvent`-style APIs
aren't exposed in a way pyobjc can drive dependably). So `zoom` falls
back to the documented keyboard equivalent: accumulated pinch travel past
a threshold sends ⌘= (zoom in) or ⌘- (zoom out). This works everywhere
those shortcuts work (Finder, Preview, browsers, System Settings
accessibility zoom if enabled) but isn't a true continuous pinch in apps
that only respond to the real gesture.

## Protocol (WebSocket, JSON, client → server)

```
{"type":"move",  "dx":Number, "dy":Number}
{"type":"click", "button":"left"|"right", "count":1|2}
{"type":"down",  "button":"left"|"right"}
{"type":"up",    "button":"left"|"right"}
{"type":"scroll","dx":Number, "dy":Number, "momentum":Boolean}
{"type":"zoom",  "magnification":Number}   // signed delta, e.g. distDelta/prevDist per event
{"type":"swipe", "fingers":3|4, "dir":"left"|"right"|"up"|"down"}
{"type":"key",   "key":String, "modifiers":["cmd"|"opt"|"ctrl"|"shift"|"fn", ...], "action":"press"|"down"|"up"}
{"type":"text",  "string":String}
```

`key` names match `helper.py`'s `KEYCODES` table: letters `a`-`z`, digits
`0`-`9`, symbols (`-`, `=`, `[`, `]`, `\`, `;`, `'`, `,`, `.`, `/`, `` ` ``),
and named keys (`tab`, `space`, `return`/`enter`, `delete`/`backspace`,
`escape`/`esc`, `command`/`cmd`, `shift`, `option`/`opt`, `control`/`ctrl`,
`fn`, `leftarrow`/`left`, `rightarrow`/`right`, `uparrow`/`up`,
`downarrow`/`down`, `f1`-`f12`, `home`, `end`, `pageup`, `pagedown`,
`forwarddelete`).

## What's tested vs. not

The web/transport layer (HTTP serving of the PWA, WebSocket handshake,
JSON parsing, dispatch of every message type to the injector) is tested
on Linux via `test_protocol.py` — it can't run on macOS-only hardware, so
that file forces the code path through `MockInjector` and asserts the
right mock action + args got called for one sample of every message
type. Run it with:

```sh
pip install aiohttp
python3 test_protocol.py
```

**Real Quartz CGEvent injection (`QuartzInjector`) is untested by this
repo** — it only runs on macOS and needs a real display/session, which
isn't available in this dev environment. It's syntax-checked (imports are
guarded so the module at least parses) but the actual mouse/keyboard
injection needs to be verified by hand on a Mac: run `helper.py`, connect
from the iPad, and confirm the cursor moves, clicks land, scrolling
works, and (after granting Input Monitoring) keystrokes actually type.
