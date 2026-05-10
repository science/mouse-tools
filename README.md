# mouse-tools

Mouse management utilities for Linux, built on evdev. A single `mouse-filter` daemon handles button remapping and (optionally) hardware-bounce suppression — replacing the need for separate tools like input-remapper.

## Features

- **Button remapping** — Remaps mouse buttons to keyboard keys (e.g., forward/back to volume up/down). A lightweight replacement for input-remapper when all you need is simple button remaps.
- **Button debounce (opt-in)** — Suppresses phantom button releases on worn Logitech (and other) mice with bouncing micro-switches. Off by default; enable with `--debounce` if your hardware needs it.
- **Wheel-bounce suppression (opt-in)** — Drops hardware-rebound wheel events on smooth-scroll wheels (e.g., Logitech MX 2 with detents disabled). Catches both cross-direction (sign-flip) and same-direction (re-burst) rebounds. Off by default; enable with `--wheel-suppress`. Toggle live with `mouse-suppress on|off|toggle`.

## The Bounce Problem (when --debounce is needed)

Logitech mice use Omron micro-switches that can wear out over time. When worn, the switch contacts briefly separate during a sustained press, producing a phantom release→re-press pair (typically 30–70ms gap). This manifests as:

- Lost text selections while drag-highlighting
- Dropped window grabs while dragging title bars
- Occasional phantom double-clicks

If you don't see these symptoms, leave `--debounce` off — the filter still grabs your mouse for remapping but adds no latency to button releases.

## How It Works

### Debounce (opt-in via `--debounce`)

Intercepts raw evdev events from all physical mice. With `--debounce` enabled, button releases that follow a long hold (a drag) are held for a configurable threshold (default: 60ms). If a re-press arrives during the hold window, both events are suppressed — the button was never really released. Genuine drag releases get the threshold duration of added latency (imperceptible for non-gaming use).

```
Hardware: press ──── release(bounce) ── press(bounce) ──── release(real)
Output:   press ────────────────────────────────────────── release
```

Only drag-bounces are suppressed (holds >= 150ms). Fast double-clicks (holds < 150ms) are always allowed through, even if the gap between clicks is within the debounce window.

When `--debounce` is off (the default), all releases are forwarded immediately — no latency, no suppression.

### Button Remapping

Remap mouse buttons to keyboard keys using `--remap`. Remapped buttons bypass debounce entirely and are forwarded immediately — they don't suffer from switch bounce and don't need the added latency.

The remap target keycodes are injected into the virtual uinput device's capability list, since the physical mouse doesn't advertise keyboard keys like `KEY_VOLUMEUP`.

### Wheel-Bounce Suppression (opt-in via `--wheel-suppress`)

Smooth-scroll wheels (Logitech MX 2 with detents disabled) exhibit hardware-level rebound after a scroll burst:

- **Type A** — single-step opposite-direction pulse arriving 67–240ms after a primary burst (kinetic backswing).
- **Type B** — same-direction re-burst arriving 250ms–4s later, typically ≤ ½ the primary's magnitude (residual momentum). Cascade chains observed: 605 → 242 → 121.

The suppressor arms a per-mouse cooldown anchor on every burst close (driven by `WheelDiagnostics`'s state machine) and applies two rules:

- **Type A veto** — drop a single-step (`±1` notch / `±120` hi-res) opposite-direction event arriving within `--wheel-rev-window-ms` of a significant primary's end.
- **Type B cooldown** — drop same-direction events within `--wheel-cooldown-ms` of a primary's end, capped at `min(primary*ratio, max_total)`. Once the cap is hit, the rest of the burst flows through normally.

A mid-gesture guard prevents false-positives during continuous scrolling, and an axis-tie keeps `REL_WHEEL` and `REL_WHEEL_HI_RES` paired across decisions. The decision is O(1) on the hot path; no buffering or added latency.

**Runtime toggle**: `mouse-suppress on|off|toggle|status`. Sends SIGUSR2 to the daemon (auth-free via the polkit rule install.sh deploys); the daemon flips state and writes a `WHEEL_SUPPRESS_TOGGLE` line to the log. Pair with `--diagnose-wheel` for the first week so you can audit `SUPPRESSED_WHEEL` lines against perceived behavior.

### Event Pipeline

```
Physical mouse → /dev/input/eventN → mouse-filter (grabs device)
    → wheel-burst classification → wheel-suppress decision (drop or pass)
    → remap buttons (if configured) → debounce filter → uinput virtual device → X11/Wayland
```

## Requirements

- Linux with evdev and uinput
- Python 3
- python3-evdev (`sudo apt install python3-evdev`)
- Root access (for evdev device grab + uinput)

## Quick Start

```bash
# Run with volume button remapping (debounce off by default)
sudo ./run.sh

# Enable debounce on top
sudo ./run.sh --debounce --threshold 70

# Or run directly with custom options
sudo ./mouse-filter \
    --remap BTN_EXTRA=KEY_VOLUMEUP \
    --remap BTN_SIDE=KEY_VOLUMEDOWN \
    --remap BTN_MIDDLE=KEY_MUTE
```

## Install (systemd service)

```bash
sudo ./install.sh
```

This copies `mouse-filter` to `/usr/local/bin/`, installs a systemd system service (with volume remap flags), and enables it to start on boot. Also cleans up any legacy `mouse-debounce` installation.

## Uninstall

```bash
sudo ./install.sh --uninstall
```

## CLI Reference

```bash
sudo mouse-filter [OPTIONS]

Options:
  --debounce            Enable hardware-bounce suppression. Off by default.
                        When off, all button releases forward immediately and
                        the threshold/hold/warn settings below are inert.
  --threshold N         Debounce window in ms (default: 60). Only used with --debounce.
  --hold-threshold N    Only debounce releases after holds longer than N ms.
                        Short clicks below this are never debounced. (default: 150)
  --warn-threshold N    Log NEAR-MISS for release→press gaps between
                        threshold and N ms (default: 100). Only used with --debounce.
  --remap SRC=DST       Remap a button to a key, e.g. BTN_EXTRA=KEY_VOLUMEUP.
                        Remapping is independent of debounce. Repeatable.
  --device PATH         Specific evdev device(s) to filter (default: all mice)
  --lag-threshold N     Log LAG_SPIKE when processing > N ms behind kernel (default: 10)
  --quiet               Suppress per-event logging (SUPPRESSED/NEAR-MISS still log)
  --stats-interval N    Print stats every N seconds (default: 600)
  --log-dir PATH        Directory for log files
  --diagnose-move       Enable movement pipeline diagnostics (see below)
  --diagnose-wheel      Log wheel-event burst classification + sign-flip
                        reversals to the log file. Use to characterize
                        wheel-rebound behavior on smooth-scroll wheels
                        before deciding on a suppression policy.
  --wheel-suppress      Enable wheel-bounce suppression (Type A + Type B).
                        Off by default. Toggle live: mouse-suppress on|off.
  --wheel-cooldown-ms N Type B same-direction window in ms (default: 1200).
  --wheel-rev-window-ms N
                        Type A reversal-veto window in ms (default: 280).
  --wheel-cooldown-ratio R
                        Suppress same-dir bounces up to primary*R (default: 0.5).
  --wheel-min-primary N Min burst magnitude (hi-res units) to arm the
                        cooldown (default: 360).
  --wheel-bounce-max-total N
                        Hard cap on cumulative magnitude suppressed per
                        cooldown (default: 300).
  --wheel-quiet-ms N    Mid-gesture guard window — never suppress within
                        this gap of a forwarded event (default: 200).
```

## Logging

Events are logged to `~/.local/share/mouse-filter/debounce.log` (uses `$SUDO_USER` home, not root). When installed as a service, logs go to `/var/log/mouse-filter/debounce.log`.

| Event | Meaning |
|-------|---------|
| `SUPPRESSED` | Drag-bounce caught and filtered. Shows the release→re-press gap and hold duration. Always logged. |
| `NEAR-MISS` | Release→re-press gap was between threshold and warn-threshold, after a drag hold. The filter didn't catch it. Consider raising `--threshold`. Always logged. |
| `STATS` | Periodic summary: total clicks, suppressions, events processed, lag spikes, max lag per device. |
| `MOVE_DIAG` | Movement pipeline breakdown with per-stage latency (only with `--diagnose-move`). |
| `WHEEL_REV` | Wheel scroll direction reversal — candidate rebound. Logs prev burst sum/count and reversal value/gap (only with `--diagnose-wheel`, file-only). |
| `WHEEL_BURST_END` | A wheel scroll burst closed out after an idle gap. Summarizes total / count / duration (only with `--diagnose-wheel`, file-only). |
| `SUPPRESSED_WHEEL` | A wheel event was dropped by the suppression filter. Includes `reason=` (type_a_reversal / type_b_cooldown / axis_tie), magnitude, age from primary end, primary sum. Always logged when `--wheel-suppress` is on; file-only. |
| `WHEEL_SUPPRESS_TOGGLE` | Suppression state change (on|off). Emitted at startup and on each SIGUSR2. Used by `mouse-suppress status` to read current state. |
| `USER_TAG` | User-pressed marker (SIGUSR1). Use to bookmark moments when a UX glitch was perceived, so the surrounding log lines can be inspected. Send via `systemctl kill -s SIGUSR1 mouse-filter.service` or the `mouse-tag` panel launcher. |

### Auth-free signaling (polkit rule)

`install.sh` writes a narrow polkit rule at `/etc/polkit-1/rules.d/50-mouse-filter-tag.rules` that allows the invoking user (`$SUDO_USER`) to send signals to `mouse-filter.service` without an auth prompt — but only when in an active console session, only on this one service, only for the `kill` verb. The rule is signal-agnostic and covers both:

- `mouse-tag` → SIGUSR1 → `USER_TAG: marker` log line
- `mouse-suppress on|off|toggle` → SIGUSR2 → flips wheel suppression + `WHEEL_SUPPRESS_TOGGLE` log line

No fingerprint prompt, no group membership expansion. Removed by `install.sh --uninstall`.

Fast double-clicks (short hold < 150ms followed by fast re-press) are silently allowed through without logging — they're normal user behavior, not bounces.

## Movement Diagnostics

Use `--diagnose-move` to instrument each stage of the movement pipeline when investigating cursor lag or jerkiness. Reports are suppressed for clean intervals — you only see output when something is wrong.

```bash
sudo ./run.sh --diagnose-move --stats-interval 30
```

When a problem is detected, you'll see a line like:

```
MOVE_DIAG: rate=99Hz input(max=0.4ms spikes=0) batch(max=48) loop(max=35.3ms stalls=3) write(max=0.17ms) x11(stalls=0) -> LOOP_STALL
```

### Pipeline Stages

```
kernel timestamp → [1: Input] → our read() → [2: Loop] → process_event()
  → [3: Write] → write_event() → [4: X11] → compositor → display
```

| Stage | Metrics | Problem Verdict | Likely Cause |
|-------|---------|-----------------|--------------|
| Input | lag vs kernel timestamp, event rate (Hz) | `INPUT_LAG` | Kernel scheduling delay, interrupt latency |
| Loop | batch size per read(), select-loop iteration time | `LOOP_STALL` | System contention (e.g., WiFi firmware crash, I/O stall) |
| Write | uinput write_event() duration | `WRITE_LAG` | uinput buffer pressure, kernel contention |
| X11 | pointer movement stalls (via XQueryPointer polling) | `X11_STALL` | Compositor lag, GPU stall |

If all stages are clean, the verdict is `CLEAN` and nothing is printed.

## Drag Monitor

The `mouse-drag-monitor` tool helps diagnose the root cause of phantom releases before applying the debounce filter. It monitors raw evdev, X11 focus changes, and BT adapter power state.

```bash
sudo mouse-drag-monitor --quiet
```

## Development

```bash
pytest tests/
```

See [CLAUDE.md](CLAUDE.md) for development conventions.
