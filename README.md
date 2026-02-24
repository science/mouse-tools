# mouse-tools

Mouse management utilities for Linux, built on evdev.

- **Button debounce** — Fixes hardware switch bounce on worn Logitech (and other) mice that causes phantom button releases during drags and false double-clicks.
- **Button remapping** — Remaps mouse buttons to keyboard keys (e.g., forward/back to volume up/down). A lightweight replacement for input-remapper when all you need is simple button remaps.

## The Problem

Logitech mice use Omron micro-switches that wear out over time. When worn, the switch contacts briefly separate during a sustained press, producing a phantom release→re-press pair (typically 30-70ms gap). This manifests as:

- Lost text selections while drag-highlighting
- Dropped window grabs while dragging title bars
- Occasional phantom double-clicks

## How It Works

Intercepts raw evdev events from all physical mice. Button releases are held for a configurable threshold (default: 60ms). If a re-press arrives during the hold window, both events are suppressed — the button was never really released. Genuine releases get the threshold duration of added latency (imperceptible for non-gaming use).

```
Hardware: press ──── release(bounce) ── press(bounce) ──── release(real)
Output:   press ────────────────────────────────────────── release
```

## Requirements

- Linux with evdev and uinput
- Python 3
- python3-evdev (`sudo apt install python3-evdev`)
- Root access (for evdev device grab + uinput)

## Install

```bash
sudo ./install.sh
```

This copies the filter to `/usr/local/bin/`, installs a systemd system service, and enables it to start on boot.

## Uninstall

```bash
sudo ./install.sh --uninstall
```

## Manual Usage

```bash
# Run with default 60ms threshold
sudo mouse-filter

# Quiet mode (only log suppressions to file)
sudo mouse-filter --quiet

# Custom threshold and near-miss warning window
sudo mouse-filter --threshold 80 --warn-threshold 120

# Filter specific device(s) only
sudo mouse-filter --device /dev/input/event23
```

## Button Remapping

Remap mouse buttons to keyboard keys using `--remap`:

```bash
# Remap forward/back to volume up/down, middle-click to mute
sudo mouse-filter --remap BTN_EXTRA=KEY_VOLUMEUP --remap BTN_SIDE=KEY_VOLUMEDOWN --remap BTN_MIDDLE=KEY_MUTE
```

Remapped buttons are forwarded immediately (not debounced), since volume/mute buttons don't suffer from switch bounce. This replaces input-remapper for simple mouse button remapping with zero additional overhead — the events are already flowing through the debounce filter.

## Logging

Events are logged to `~/.local/share/mouse-filter/debounce.log` (uses `$SUDO_USER` home, not root).

| Event | Meaning |
|-------|---------|
| `SUPPRESSED` | Bounce caught and filtered. Shows the release→re-press gap in ms. |
| `NEAR-MISS` | Release→re-press gap exceeded threshold but was within warning window. The filter didn't catch it. Consider raising `--threshold` if these correlate with UX glitches. |
| `STATS` | Periodic summary of total clicks and suppressions per device. |

## Diagnostics

The `mouse-drag-monitor` tool helps diagnose the root cause of phantom releases before applying the debounce filter. It monitors raw evdev, X11 focus changes, and BT adapter power state.

```bash
sudo mouse-drag-monitor --quiet
```

## Development

```bash
pytest tests/
```

See [CLAUDE.md](CLAUDE.md) for development conventions.
