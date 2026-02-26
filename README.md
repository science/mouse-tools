# mouse-tools

Mouse management utilities for Linux, built on evdev. A single `mouse-filter` daemon handles both button debounce and button remapping — replacing the need for separate tools like input-remapper.

## Features

- **Button debounce** — Fixes hardware switch bounce on worn Logitech (and other) mice that causes phantom button releases during drags and false double-clicks.
- **Button remapping** — Remaps mouse buttons to keyboard keys (e.g., forward/back to volume up/down). A lightweight replacement for input-remapper when all you need is simple button remaps.

## The Problem

Logitech mice use Omron micro-switches that wear out over time. When worn, the switch contacts briefly separate during a sustained press, producing a phantom release→re-press pair (typically 30-70ms gap). This manifests as:

- Lost text selections while drag-highlighting
- Dropped window grabs while dragging title bars
- Occasional phantom double-clicks

## How It Works

### Debounce

Intercepts raw evdev events from all physical mice. Button releases are held for a configurable threshold (default: 60ms). If a re-press arrives during the hold window, both events are suppressed — the button was never really released. Genuine releases get the threshold duration of added latency (imperceptible for non-gaming use).

```
Hardware: press ──── release(bounce) ── press(bounce) ──── release(real)
Output:   press ────────────────────────────────────────── release
```

Only drag-bounces are suppressed (holds >= 150ms). Fast double-clicks (holds < 150ms) are always allowed through, even if the gap between clicks is within the debounce window.

### Button Remapping

Remap mouse buttons to keyboard keys using `--remap`. Remapped buttons bypass debounce entirely and are forwarded immediately — they don't suffer from switch bounce and don't need the added latency.

The remap target keycodes are injected into the virtual uinput device's capability list, since the physical mouse doesn't advertise keyboard keys like `KEY_VOLUMEUP`.

### Event Pipeline

```
Physical mouse → /dev/input/eventN → mouse-filter (grabs device)
    → remap buttons (if configured) → debounce filter → uinput virtual device → X11/Wayland
```

## Requirements

- Linux with evdev and uinput
- Python 3
- python3-evdev (`sudo apt install python3-evdev`)
- Root access (for evdev device grab + uinput)

## Quick Start

```bash
# Run with debounce + volume button remapping
sudo ./run.sh

# Or run directly with custom options
sudo ./mouse-filter --threshold 70 \
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
  --threshold N         Debounce threshold in ms (default: 60)
  --hold-threshold N    Only debounce releases after holds longer than N ms.
                        Short clicks below this are never debounced. (default: 150)
  --warn-threshold N    Log NEAR-MISS for release→press gaps between
                        threshold and N ms (default: 100)
  --remap SRC=DST       Remap a button to a key, e.g. BTN_EXTRA=KEY_VOLUMEUP.
                        Remapped buttons bypass debounce. Repeatable.
  --device PATH         Specific evdev device(s) to filter (default: all mice)
  --lag-threshold N     Log LAG_SPIKE when processing > N ms behind kernel (default: 10)
  --quiet               Suppress per-event logging (SUPPRESSED/NEAR-MISS still log)
  --stats-interval N    Print stats every N seconds (default: 600)
  --log-dir PATH        Directory for log files
  --diagnose-move       Enable movement pipeline diagnostics (see below)
```

## Logging

Events are logged to `~/.local/share/mouse-filter/debounce.log` (uses `$SUDO_USER` home, not root). When installed as a service, logs go to `/var/log/mouse-filter/debounce.log`.

| Event | Meaning |
|-------|---------|
| `SUPPRESSED` | Drag-bounce caught and filtered. Shows the release→re-press gap and hold duration. Always logged. |
| `NEAR-MISS` | Release→re-press gap was between threshold and warn-threshold, after a drag hold. The filter didn't catch it. Consider raising `--threshold`. Always logged. |
| `STATS` | Periodic summary: total clicks, suppressions, events processed, lag spikes, max lag per device. |
| `MOVE_DIAG` | Movement pipeline breakdown with per-stage latency (only with `--diagnose-move`). |

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
