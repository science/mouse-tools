# mouse-debounce

Universal mouse button debounce filter for Linux. Fixes hardware switch bounce on worn Logitech (and other) mice that causes phantom button releases during drags and false double-clicks.

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
sudo mouse-debounce

# Quiet mode (only log suppressions to file)
sudo mouse-debounce --quiet

# Custom threshold and near-miss warning window
sudo mouse-debounce --threshold 80 --warn-threshold 120

# Filter specific device(s) only
sudo mouse-debounce --device /dev/input/event23
```

## Logging

Events are logged to `~/.local/share/mouse-debounce/debounce.log` (uses `$SUDO_USER` home, not root).

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
