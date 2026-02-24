# Mouse Tools Project

## What This Is

A collection of mouse management utilities for Linux, built on evdev. Currently provides:
- **Button debounce** — Fixes hardware switch bounce (worn Omron micro-switches) that causes phantom releases during drags and false double-clicks.
- **Button remapping** — Remaps mouse buttons to keyboard keys (e.g., forward/back → volume up/down). Replaces input-remapper for simple mouse button remaps.

## Architecture

Tools:

- **`mouse-filter`** — The main filter. Grabs raw evdev mouse devices, remaps configured buttons, holds button releases for a configurable threshold, suppresses bounce (release→re-press pairs within the window). Creates virtual `debounced <name>` devices via uinput. Intended to run as a system service.
- **`mouse-drag-monitor`** — Diagnostic tool. Monitors raw evdev + X11 focus + BT adapter power state to identify the source of phantom releases. Used during investigation, not in production.

### Debounce Strategy (Delayed Release)

```
Hardware: press ──────────── release(bounce) ── press(bounce) ──────── release(real)
Filter:   press ──────────── [hold 60ms...]  ── suppress both ──────── [hold 60ms...] → release
Output:   press ──────────────────────────────────────────────────────────────────────── release
```

Button releases are held for `threshold` ms. If a re-press arrives during the hold, both are suppressed (the button was never really released). Genuine releases get `threshold` ms of added latency.

### Event Pipeline

```
Physical mouse → /dev/input/eventN → mouse-filter (grabs device)
    → remap buttons → debounce filter → uinput virtual device → X11/Wayland
```

### Button Remapping

Remapping happens before debounce. Remapped buttons are forwarded with the new code and are NOT debounced (volume/mute buttons don't have bounce issues). Configure via `--remap` CLI flag or the `BUTTON_REMAP` dict in the source.

## Development Rules

1. **TDD: Red → Green → Refactor** — Write or update tests FIRST, verify they fail, then implement. Every PR must include tests.
2. **Requires root for integration testing** — evdev grab + uinput need root. Unit tests should mock evdev where possible.
3. **Test with `pytest`** — Test files in `tests/`. Run: `pytest tests/`
4. **Don't break the filter loop** — The select() event loop is latency-sensitive. Avoid blocking operations, GC pressure, or heavy computation in the hot path.
5. **Log levels matter** — SUPPRESSED and NEAR-MISS always log. Normal events only log without `--quiet`. Stats log on interval. Don't add noisy logging to the hot path.

## Key Files

| File | Purpose |
|------|---------|
| `mouse-filter` | Main filter: debounce + button remapping (runs as service) |
| `mouse-drag-monitor` | Diagnostic/telemetry tool |
| `tests/` | Test suite |
| `CLAUDE.md` | This file — project rules for AI |
| `README.md` | User-facing documentation |

## Classes

- **`DelayedDebouncedMouse`** — The production debounce implementation. Delays releases, suppresses bounce. This is the one that matters.
- **`DebouncedMouse`** — Earlier approach (suppress on re-press, no release delay). Kept for reference but not used. Can be removed.
- **`FocusMonitor`** (in drag-monitor) — Watches `_NET_ACTIVE_WINDOW` via xprop.
- **`BTSuspendMonitor`** (in drag-monitor) — Polls BT adapter USB power state.

## Testing Approach

Unit tests should test `DelayedDebouncedMouse` logic by mocking evdev devices:
- Simulate event sequences (press, release, bounce patterns)
- Verify correct events are forwarded/suppressed
- Verify timing behavior (threshold boundary conditions)
- Verify NEAR-MISS detection
- Verify multi-button independence
- Verify non-button events pass through unchanged
- Verify button remapping transforms codes correctly
- Verify remapped buttons are NOT debounced (forwarded immediately)

## Common Tasks

### Adjust threshold
Change `DEBOUNCE_MS` constant (default: 60). CLI override: `--threshold N`.

### Add a new button to debounce
Add the `ecodes.BTN_*` constant to `BUTTON_CODES` set.

### Deploy to ~/.local/bin
```bash
cp mouse-filter ~/.local/bin/mouse-filter
```

### Run manually
```bash
sudo ./mouse-filter              # verbose
sudo ./mouse-filter --quiet      # production mode
sudo ./mouse-filter --threshold 80 --warn-threshold 120
```
