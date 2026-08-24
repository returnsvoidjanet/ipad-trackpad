# iPad Trackpad + Keyboard (v1.1)

Turns an iPad into a wireless trackpad + keyboard for a Mac. Architecture:
a small Python server (`helper.py`) runs **on your Mac**, serves a PWA to
the iPad over your LAN, and injects real macOS input events (mouse moves,
clicks, scrolls, zoom, swipes, keystrokes) via Quartz when messages arrive
over a WebSocket. No cloud, no App Store, nothing leaves your LAN.

v1 scope: trackpad + keyboard only. No macros, no notes, no lab-specific
features.

**v1.1**: latency pass (TCP_NODELAY + client-side move/scroll
coalescing — see "Performance" below), plus a regression-guard pass on
click button mapping and 3/4-finger swipe → keystroke mapping after a v1
report that clicks felt swapped and swipes weren't registering.

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
- `debug_log_messages` — off by default. When `true`, logs every dispatched
  WS message on the Mac side. Useful for debugging the protocol, but real
  per-message overhead, so leave it off unless you're actively debugging.

## Performance

v1 feedback was "not fast enough" — here's what changed and why:

- **TCP_NODELAY on the WebSocket.** Nagle's algorithm batches small
  outbound TCP segments waiting for an ACK or a full segment before
  sending. Move packets are tiny and frequent — exactly what Nagle delays
  most — so `helper.py` disables it (`socket.TCP_NODELAY`) on every WS
  connection's underlying socket and confirms (via `getsockopt`, logged)
  that it actually stuck.
- **Client-side move/scroll coalescing.** `touchmove` can fire faster than
  the display refresh rate, so the PWA used to send one WS message per
  event. It now accumulates the relative dx/dy across all touchmoves in a
  frame and flushes ONE `move` (or `scroll`) message per
  `requestAnimationFrame` tick — same visual responsiveness (~60–120Hz),
  far fewer packets. No motion is dropped (every delta is summed in), and
  a frame with nothing new never sends an empty message. Any move still
  pending is flushed immediately on touch-end so lifting a finger doesn't
  strand up to a frame's worth of motion.
- **Tightened dispatch path.** `dispatch()` is fully synchronous (no
  awaits/sleeps between a WS message arriving and the corresponding
  Quartz event being posted), and per-message logging is now gated behind
  `debug_log_messages` so it isn't a hot-path cost by default.
- **Not changed, on purpose**: message shape (still plain JSON, same
  keys) and keyboard keystrokes (still sent immediately, uncoalesced —
  batching would make typing feel laggy). Compact/binary framing was
  considered and skipped as unnecessary — NODELAY + coalescing were the
  two actual wins.

Real end-to-end feel (finger-to-cursor, on the actual Mac) can only be
judged by hand on macOS — this pass fixes the two biggest *transport*
culprits and is proven at that layer (see "What's tested vs. not"). If it
still doesn't feel fast enough after this, the next lever is a
lower-latency transport (WebRTC/UDP data channel instead of WS-over-TCP)
and/or a native iPad client instead of a Safari PWA.

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

  **This depends on two things a code fix can't change:** (1) Mission
  Control's keyboard shortcuts must be enabled in **System Settings →
  Keyboard → Keyboard Shortcuts → Mission Control** on the Mac (Ctrl+←/→/
  ↑/↓ specifically) — if they've been reassigned or turned off, the
  keystroke goes out but nothing visible happens; (2) Ctrl+Left/Right
  switching Spaces requires **more than one Space to exist** — with only
  one Space it's a no-op by design.

  **If a 3-finger swipe seems to do nothing at all** (not even a Space
  switch that's a no-op), check whether iPadOS is intercepting it first:
  Safari/WebKit has its own **system-level 3-finger text-editing gesture**
  (undo/redo/copy) that can swallow 3-finger touches before they ever
  reach this page's JS, independent of `touch-action` or
  `preventDefault()` — that's an OS/WebKit gesture recognizer, not
  something a web page can override. If swipes work with 4 fingers but
  not 3, this is almost certainly why. (4-finger/5-finger gestures have
  their own separate, always-reserved system meaning — App Switcher/Home —
  and are never deliverable to a web page at all.)

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
JSON parsing, dispatch of every message type to the injector, and — as of
v1.1 — that `button:"left"`/`"right"` clicks and every 3/4-finger swipe
direction resolve to the correct button/keystroke) is tested on Linux via
`test_protocol.py`. It can't run on macOS-only hardware, so it forces the
code path through `MockInjector` and asserts the right mock action + args
got called for one sample of every message type. Run it with:

```sh
pip install aiohttp
python3 test_protocol.py
```

**`QuartzInjector`'s actual CGEvent selection is now also tested on
Linux**, via `test_quartz_mapping.py`. `QuartzInjector` is normally
unconstructable off macOS (its `__init__` requires `import Quartz` to
have already succeeded), so this installs a fake `Quartz` module into
`sys.modules` first, letting the real class construct and its real
`click`/`mouse_down`/`mouse_up`/`move`/`swipe` code run — proving (not
just reading) that left/right never swap at the CGEvent-type/
CGMouseButton level, and that every swipe direction posts the correct
keycode with the Control modifier flag set:

```sh
python3 test_quartz_mapping.py
```

**What's still genuinely untested by this repo**: the actual pixels/
clicks/keystrokes landing in a running macOS session — that needs a real
display and a real Mac, which isn't available in this dev environment.
`QuartzInjector` is syntax-checked and its constant-selection logic is
covered as above, but end-to-end behavior (does the cursor actually move,
does the Space actually switch, does typing actually appear) needs to be
verified by hand on a Mac: run `helper.py`, connect from the iPad, and
confirm the cursor moves, clicks land, scrolling works, swipes switch
Spaces, and (after granting Input Monitoring) keystrokes actually type.
