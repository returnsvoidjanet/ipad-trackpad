"use strict";

/* =========================================================================
 * WebSocket transport: connects back to whatever origin served this page,
 * on /ws, with auto-reconnect (handles the Mac sleeping/waking, Wi-Fi
 * drops, helper.py restarting, etc).
 * ======================================================================= */

const Conn = (() => {
  const dot = document.getElementById("conn-dot");
  const text = document.getElementById("conn-text");

  let ws = null;
  let retryDelay = 500;
  const MAX_RETRY_DELAY = 5000;
  let manuallyClosed = false;

  function setState(state) {
    dot.className = "dot " + state;
    if (state === "connected") text.textContent = "Connected";
    else if (state === "connecting") text.textContent = "Connecting…";
    else text.textContent = "Disconnected — retrying…";
  }

  function connect() {
    manuallyClosed = false;
    setState("connecting");
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws`);

    ws.addEventListener("open", () => {
      retryDelay = 500;
      setState("connected");
    });

    ws.addEventListener("close", () => {
      setState("disconnected");
      if (!manuallyClosed) scheduleReconnect();
    });

    ws.addEventListener("error", () => {
      try { ws.close(); } catch (e) { /* ignore */ }
    });
  }

  function scheduleReconnect() {
    setTimeout(connect, retryDelay);
    retryDelay = Math.min(MAX_RETRY_DELAY, retryDelay * 1.6);
  }

  function send(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
    }
  }

  connect();

  // Reconnect promptly when the tab/page becomes visible/foregrounded
  // again (covers iPad Safari suspending the WS in the background).
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && (!ws || ws.readyState > WebSocket.OPEN)) {
      retryDelay = 500;
      connect();
    }
  });

  return { send };
})();

/* =========================================================================
 * Tabs
 * ======================================================================= */

(() => {
  const buttons = document.querySelectorAll(".tab-btn");
  const panels = {
    trackpad: document.getElementById("panel-trackpad"),
    keyboard: document.getElementById("panel-keyboard"),
  };
  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      buttons.forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      Object.values(panels).forEach((p) => p.classList.remove("active"));
      panels[btn.dataset.tab].classList.add("active");
    });
  });
})();

/* =========================================================================
 * Trackpad
 *
 * Gesture model, keyed off simultaneous touch count on the surface:
 *   1 finger : move cursor. Quick tap (short, low travel) -> left click.
 *              Tap-and-hold past TAP_HOLD_MS without much travel -> mouse
 *              "down" (drag), tracks moves, "up" on release.
 *   2 fingers: drag -> scroll (with momentum on release).
 *              Distance-between-touches changing a lot relative to the
 *              midpoint's own travel -> pinch zoom instead of scroll.
 *              Quick tap, both fingers, little travel -> right click.
 *   3/4 fingers: net travel past a threshold in one direction -> one
 *              swipe message (fingers/dir), fired once per touch-down.
 * ======================================================================= */

(() => {
  const el = document.getElementById("trackpad");

  const TAP_MAX_MS = 200;
  const TAP_MAX_TRAVEL = 10; // px
  const HOLD_MS = 320; // 1-finger hold-still -> starts a drag
  const HOLD_MAX_TRAVEL = 6;
  const SWIPE_THRESHOLD = 70; // px of net travel to fire a swipe
  const PINCH_DOMINANCE = 1.4; // distance-delta must exceed translation by this factor to count as pinch

  // Acceleration curve: small/slow movements stay precise, fast flicks
  // travel further, similar to a Magic Trackpad.
  const MIN_FACTOR = 0.9;
  const MAX_FACTOR = 2.6;
  const ACCEL_RANGE = 28; // px/frame at which acceleration saturates
  function accel(deltaMag) {
    const t = Math.min(1, deltaMag / ACCEL_RANGE);
    return MIN_FACTOR + (MAX_FACTOR - MIN_FACTOR) * Math.pow(t, 0.7);
  }

  function touchPoint(t) {
    return { x: t.clientX, y: t.clientY };
  }

  function centroid(touches) {
    let x = 0, y = 0;
    for (const t of touches) { x += t.clientX; y += t.clientY; }
    return { x: x / touches.length, y: y / touches.length };
  }

  function distance(a, b) {
    return Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
  }

  // Gesture session state, reset on every touchstart-from-zero.
  let session = null;

  function newSession(touches) {
    const now = performance.now();
    return {
      startTime: now,
      lastTime: now,
      startCentroid: centroid(touches),
      lastCentroid: centroid(touches),
      maxFingers: touches.length,
      totalTravel: 0,
      holdTimer: null,
      dragging: false, // 1-finger "down" issued
      swipeFired: false,
      pinchDist: touches.length >= 2 ? distance(touches[0], touches[1]) : null,
      momentum: null, // {vx, vy} px/ms sampled just before release, for 2-finger scroll
    };
  }

  function clearHoldTimer(s) {
    if (s.holdTimer) { clearTimeout(s.holdTimer); s.holdTimer = null; }
  }

  function endDragIfActive(s) {
    if (s.dragging) {
      Conn.send({ type: "up", button: "left" });
      s.dragging = false;
    }
  }

  let momentumRaf = null;
  function stopMomentum() {
    if (momentumRaf) { cancelAnimationFrame(momentumRaf); momentumRaf = null; }
  }

  // -- move/scroll coalescing --------------------------------------------
  //
  // touchmove can fire much faster than the display refresh rate (bursts
  // of several events per frame are common, especially on ProMotion
  // iPads). Sending one WS message per touchmove floods the socket with
  // far more small packets than the cursor can visibly move between
  // frames. Instead, accumulate the relative deltas from every touchmove
  // in a frame and flush ONE message per requestAnimationFrame tick -
  // this caps the send rate at the display's refresh rate (~60-120Hz)
  // without losing any motion (every delta is still summed in, none are
  // dropped) and without sending anything when there's nothing new to
  // report.
  function makeCoalescedSender(type, extra) {
    let pendingDx = 0;
    let pendingDy = 0;
    let rafId = null;

    function flush() {
      rafId = null;
      if (pendingDx === 0 && pendingDy === 0) return; // never send a zero-delta frame
      const msg = { type, dx: pendingDx, dy: pendingDy };
      if (extra) Object.assign(msg, extra);
      Conn.send(msg);
      pendingDx = 0;
      pendingDy = 0;
    }

    return {
      queue(dx, dy) {
        pendingDx += dx;
        pendingDy += dy;
        if (rafId === null) rafId = requestAnimationFrame(flush);
      },
      // Send whatever is pending right now instead of waiting for the next
      // frame, and cancel that frame's callback. Used when a gesture ends
      // so the last bit of motion lands immediately instead of up to one
      // frame late.
      flushNow() {
        if (rafId !== null) { cancelAnimationFrame(rafId); rafId = null; }
        flush();
      },
    };
  }

  const moveSender = makeCoalescedSender("move");
  const scrollSender = makeCoalescedSender("scroll", { momentum: false });

  function startMomentum(vx, vy) {
    stopMomentum();
    // vx/vy in px/ms. Convert to a decaying per-frame scroll.
    let vX = vx * 16, vY = vy * 16; // px per ~frame
    const FRICTION = 0.94;
    const CUTOFF = 0.4;
    function tick() {
      vX *= FRICTION;
      vY *= FRICTION;
      if (Math.abs(vX) < CUTOFF && Math.abs(vY) < CUTOFF) { momentumRaf = null; return; }
      Conn.send({ type: "scroll", dx: vX, dy: vY, momentum: true });
      momentumRaf = requestAnimationFrame(tick);
    }
    momentumRaf = requestAnimationFrame(tick);
  }

  el.addEventListener("touchstart", (e) => {
    e.preventDefault();
    stopMomentum();
    // Defensive: a fresh gesture shouldn't inherit any coalesced-but-
    // unsent delta from whatever came before (normally already flushed
    // by onEnd, this just guards against edge cases like a missed
    // touchend/touchcancel).
    moveSender.flushNow();
    scrollSender.flushNow();
    el.classList.add("tapping");
    const touches = Array.from(e.touches);
    session = newSession(touches);

    if (touches.length === 1) {
      const s = session;
      s.holdTimer = setTimeout(() => {
        // Held still long enough with one finger -> start a drag.
        if (s === session && !s.dragging && s.totalTravel < HOLD_MAX_TRAVEL) {
          Conn.send({ type: "down", button: "left" });
          s.dragging = true;
        }
      }, HOLD_MS);
    }
  }, { passive: false });

  el.addEventListener("touchmove", (e) => {
    e.preventDefault();
    if (!session) return;
    const s = session;
    const touches = Array.from(e.touches);
    s.maxFingers = Math.max(s.maxFingers, touches.length);
    const now = performance.now();
    const c = centroid(touches);
    const dt = Math.max(1, now - s.lastTime);
    const dx = c.x - s.lastCentroid.x;
    const dy = c.y - s.lastCentroid.y;
    s.totalTravel += Math.hypot(dx, dy);

    if (touches.length === 1) {
      if (Math.hypot(dx, dy) > 0) {
        clearHoldTimer(s); // real movement cancels the "hold to drag" path's stillness requirement
        const mag = Math.hypot(dx, dy);
        const f = accel(mag);
        moveSender.queue(dx * f, dy * f);
      }
    } else if (touches.length === 2) {
      const curDist = distance(touches[0], touches[1]);
      const distDelta = s.pinchDist == null ? 0 : curDist - s.pinchDist;
      const translation = Math.hypot(dx, dy);

      if (Math.abs(distDelta) > translation * PINCH_DOMINANCE && s.pinchDist) {
        Conn.send({ type: "zoom", magnification: distDelta / s.pinchDist });
      } else {
        scrollSender.queue(dx, dy);
        // velocity sample for momentum on release
        s.momentum = { vx: dx / dt, vy: dy / dt };
      }
      s.pinchDist = curDist;
    } else if (touches.length >= 3 && !s.swipeFired) {
      const totalDx = c.x - s.startCentroid.x;
      const totalDy = c.y - s.startCentroid.y;
      if (Math.abs(totalDx) > SWIPE_THRESHOLD || Math.abs(totalDy) > SWIPE_THRESHOLD) {
        const dir = Math.abs(totalDx) > Math.abs(totalDy)
          ? (totalDx > 0 ? "right" : "left")
          : (totalDy > 0 ? "down" : "up");
        Conn.send({ type: "swipe", fingers: touches.length >= 4 ? 4 : 3, dir });
        s.swipeFired = true;
      }
    }

    s.lastCentroid = c;
    s.lastTime = now;
  }, { passive: false });

  function onEnd(e) {
    e.preventDefault();
    if (!session) return;
    const s = session;
    const remaining = e.touches.length;

    // Land any coalesced-but-not-yet-sent motion immediately instead of
    // waiting for the next animation frame, so lifting a finger mid-move
    // doesn't leave up to ~1 frame of motion stranded.
    moveSender.flushNow();
    scrollSender.flushNow();

    if (remaining === 0) {
      el.classList.remove("tapping");
      clearHoldTimer(s);
      const duration = performance.now() - s.startTime;
      const isTap = duration < TAP_MAX_MS && s.totalTravel < TAP_MAX_TRAVEL;

      if (s.dragging) {
        endDragIfActive(s);
      } else if (isTap && s.maxFingers === 1) {
        Conn.send({ type: "click", button: "left", count: 1 });
      } else if (isTap && s.maxFingers === 2) {
        Conn.send({ type: "click", button: "right", count: 1 });
      } else if (s.maxFingers === 2 && s.momentum) {
        startMomentum(s.momentum.vx, s.momentum.vy);
      }
      session = null;
    } else {
      // Fingers lifted but not all the way -> just resync centroid/dist
      // so the remaining touches don't cause a jump.
      const touches = Array.from(e.touches);
      s.startCentroid = centroid(touches);
      s.lastCentroid = centroid(touches);
      s.pinchDist = touches.length >= 2 ? distance(touches[0], touches[1]) : null;
    }
  }

  el.addEventListener("touchend", onEnd, { passive: false });
  el.addEventListener("touchcancel", onEnd, { passive: false });
})();

/* =========================================================================
 * Keyboard
 * ======================================================================= */

(() => {
  const layout = [
    [
      { k: "esc", label: "esc", small: true },
      { k: "1" }, { k: "2" }, { k: "3" }, { k: "4" }, { k: "5" },
      { k: "6" }, { k: "7" }, { k: "8" }, { k: "9" }, { k: "0" },
      { k: "-", label: "-" }, { k: "=", label: "=" },
      { k: "delete", label: "delete", wide: true, small: true },
    ],
    [
      { k: "tab", label: "tab", small: true },
      { k: "q" }, { k: "w" }, { k: "e" }, { k: "r" }, { k: "t" },
      { k: "y" }, { k: "u" }, { k: "i" }, { k: "o" }, { k: "p" },
      { k: "[", label: "[" }, { k: "]", label: "]" }, { k: "\\", label: "\\" },
    ],
    [
      { k: "capslock", label: "caps", small: true, mod: true },
      { k: "a" }, { k: "s" }, { k: "d" }, { k: "f" }, { k: "g" },
      { k: "h" }, { k: "j" }, { k: "k" }, { k: "l" },
      { k: ";", label: ";" }, { k: "'", label: "'" },
      { k: "return", label: "return", wide: true, small: true },
    ],
    [
      { k: "shift", label: "shift", wide: true, small: true, mod: true },
      { k: "z" }, { k: "x" }, { k: "c" }, { k: "v" }, { k: "b" },
      { k: "n" }, { k: "m" }, { k: ",", label: "," }, { k: ".", label: "." }, { k: "/", label: "/" },
      { k: "shift", label: "shift", wide: true, small: true, mod: true },
    ],
    [
      { k: "fn", label: "fn", small: true, mod: true },
      { k: "control", label: "ctrl", small: true, mod: true },
      { k: "option", label: "opt", small: true, mod: true },
      { k: "command", label: "cmd", wide: true, small: true, mod: true },
      { k: "space", label: "", xwide: true },
      { k: "command", label: "cmd", wide: true, small: true, mod: true },
      { k: "option", label: "opt", small: true, mod: true },
      { k: "left", label: "←", small: true },
      { k: "up", label: "↑", small: true },
      { k: "down", label: "↓", small: true },
      { k: "right", label: "→", small: true },
    ],
  ];

  const container = document.getElementById("keyboard");
  const activeModifiers = new Set(); // e.g. "cmd", "shift" ...

  // shift/capslock are visual-only modifiers here — they're not sent as
  // Quartz modifier flags for letters (macOS derives case from the actual
  // keycode's shifted glyph via flags), so treat "shift" as a real
  // modifier flag (it IS in helper.py's MODIFIER_FLAGS) and just don't
  // give capslock protocol meaning in v1.
  const REAL_MODIFIERS = new Set(["cmd", "opt", "ctrl", "shift", "fn"]);
  const KEY_TO_MODIFIER = {
    command: "cmd",
    option: "opt",
    control: "ctrl",
    shift: "shift",
    fn: "fn",
  };

  function buildKeyboard() {
    for (const row of layout) {
      const rowEl = document.createElement("div");
      rowEl.className = "krow";
      for (const spec of row) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "key" + (spec.mod ? " mod" : "") + (spec.small ? " small" : "") +
          (spec.wide ? " wide" : "") + (spec.xwide ? " xwide" : "");
        btn.textContent = spec.label !== undefined ? spec.label : spec.k;
        btn.dataset.key = spec.k;
        if (spec.mod) btn.dataset.mod = KEY_TO_MODIFIER[spec.k] || "";
        rowEl.appendChild(btn);
      }
      container.appendChild(rowEl);
    }
  }

  function setModifierVisual(modName, active) {
    document.querySelectorAll(`.key.mod[data-mod="${modName}"]`).forEach((btn) => {
      btn.classList.toggle("active", active);
    });
  }

  function handlePress(btn) {
    const key = btn.dataset.key;
    const modName = btn.dataset.mod;

    if (modName && REAL_MODIFIERS.has(modName)) {
      // Sticky: toggle on/off; stays active until used or tapped again.
      const willActivate = !activeModifiers.has(modName);
      if (willActivate) activeModifiers.add(modName); else activeModifiers.delete(modName);
      setModifierVisual(modName, willActivate);
      return;
    }

    if (key === "capslock") return; // no-op in v1

    const modifiers = Array.from(activeModifiers);
    Conn.send({ type: "key", key, modifiers, action: "press" });

    // Single-use sticky modifiers: clear after the next real key.
    if (activeModifiers.size) {
      activeModifiers.forEach((m) => setModifierVisual(m, false));
      activeModifiers.clear();
    }
  }

  buildKeyboard();

  container.addEventListener("touchstart", (e) => {
    const btn = e.target.closest(".key");
    if (!btn) return;
    e.preventDefault();
    btn.classList.add("pressed");
    handlePress(btn);
  }, { passive: false });

  container.addEventListener("touchend", (e) => {
    const btn = e.target.closest(".key");
    if (btn) btn.classList.remove("pressed");
  }, { passive: false });

  // -- literal text field --
  const typefield = document.getElementById("typefield");
  const typeclear = document.getElementById("typeclear");
  let lastValue = "";

  typefield.addEventListener("input", () => {
    const value = typefield.value;
    if (value.length > lastValue.length && value.startsWith(lastValue)) {
      // Pure append: send only the new characters as literal text.
      const added = value.slice(lastValue.length);
      Conn.send({ type: "text", string: added });
    } else if (value.length < lastValue.length && lastValue.startsWith(value)) {
      // Pure deletion: send one delete keypress per removed character.
      const removed = lastValue.length - value.length;
      for (let i = 0; i < removed; i++) {
        Conn.send({ type: "key", key: "delete", modifiers: [], action: "press" });
      }
    } else {
      // Autocorrect / mid-string edit — just resync by retyping the tail
      // that differs, cheaply: clear field's effect isn't tracked further
      // than best-effort for v1.
      Conn.send({ type: "text", string: value });
    }
    lastValue = value;
  });

  typeclear.addEventListener("click", () => {
    typefield.value = "";
    lastValue = "";
    typefield.blur();
  });
})();
