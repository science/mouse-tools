# Mouse Tools Project

## What This Is

A collection of mouse management utilities for Linux, built on evdev. A single `mouse-filter` daemon handles:
- **Button debounce** — Fixes hardware switch bounce (worn Omron micro-switches) that causes phantom releases during drags and false double-clicks.
- **Button remapping** — Remaps mouse buttons to keyboard keys (e.g., forward/back → volume up/down). Replaces input-remapper for simple mouse button remaps with zero additional overhead.

## Architecture

Tools:

- **`mouse-filter`** — The main filter. Grabs raw evdev mouse devices, remaps configured buttons, debounces button releases. Creates virtual `debounced <name>` devices via uinput. Runs as a systemd service or manually via `run.sh`.
- **`mouse-drag-monitor`** — Diagnostic tool. Monitors raw evdev + X11 focus + BT adapter power state to identify the source of phantom releases. Used during investigation, not in production.
- **`run.sh`** — Quick-launch wrapper with standard volume remap config. Passes through extra args.

### Debounce Strategy (Delayed Release)

```
Hardware: press ──────────── release(bounce) ── press(bounce) ──────── release(real)
Filter:   press ──────────── [hold 60ms...]  ── suppress both ──────── [hold 60ms...] → release
Output:   press ──────────────────────────────────────────────────────────────────────── release
```

Button releases are held for `threshold` ms. If a re-press arrives during the hold, both are suppressed (the button was never really released). Genuine releases get `threshold` ms of added latency.

**Hold-aware**: Only drag-bounces (hold >= 150ms) are suppressed. Fast double-clicks (hold < 150ms) are always allowed through silently — they're normal user behavior.

### Event Pipeline

```
Physical mouse → /dev/input/eventN → mouse-filter (grabs device)
    → remap buttons (if configured) → debounce filter → uinput virtual device → X11/Wayland
```

### Button Remapping

Remapping happens before debounce. Remapped buttons are forwarded immediately with the new code and are NOT debounced (volume/mute buttons don't have bounce issues). The remap target keycodes are injected into the uinput device's capability list since physical mice don't advertise keyboard keys.

Configure via `--remap SRC=DST` CLI flag (repeatable).

### Log Events

| Event | When | Logged in --quiet? |
|-------|------|-------------------|
| `SUPPRESSED` | Drag-bounce caught (release→re-press within threshold after long hold) | Yes |
| `NEAR-MISS` | Release→re-press gap escaped threshold but within warn window, after drag | Yes |
| `STATS` | Periodic summary (clicks, suppressions, lag) | Yes |
| `MOVE_DIAG` | Per-stage movement latency breakdown (--diagnose-move only) | Yes |
| Fast double-click allowed | Short hold + fast re-press (normal behavior) | No — silent by design |

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
| `run.sh` | Quick-launch with standard volume remap config |
| `install.sh` | Install/uninstall systemd service (cleans up legacy mouse-debounce too) |
| `mouse-drag-monitor` | Diagnostic/telemetry tool |
| `tests/` | Test suite |
| `CLAUDE.md` | This file — project rules for AI |
| `README.md` | User-facing documentation |

## Classes

- **`DelayedDebouncedMouse`** — The production implementation. Handles remapping, delayed releases, bounce suppression. This is the one that matters.
- **`MoveDiagnostics`** — Per-interval movement pipeline telemetry. Tracks 4 stages: input delivery lag/Hz, batch sizes, loop iteration time, uinput write latency. Also holds `x11_stalls` counter written by X11PointerProbe. Created per-mouse only when `--diagnose-move` is active.
- **`X11PointerProbe`** — Daemon thread polling `XQueryPointer` at 200Hz to detect downstream pointer stalls. If events are forwarded but the pointer hasn't moved for 50ms, logs an `x11_stall`. Gracefully degrades when `$DISPLAY` unavailable.
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
- Verify uinput capabilities include remap target keycodes

## Common Tasks

### Adjust threshold
Change `DEBOUNCE_MS` constant (default: 60). CLI override: `--threshold N`.

### Add a new button remap
Add `--remap BTN_xxx=KEY_yyy` to `run.sh` and `install.sh` ExecStart line.

### Add a new button to debounce
Add the `ecodes.BTN_*` constant to `BUTTON_CODES` set.

### Diagnose movement lag
Use `--diagnose-move` to instrument each stage of the movement pipeline. Reports at each stats interval with a verdict (`CLEAN`, `INPUT_LAG`, `LOOP_STALL`, `WRITE_LAG`, `X11_STALL`).
```bash
sudo ./run.sh --diagnose-move --stats-interval 30
```

### Run manually
```bash
sudo ./run.sh                          # standard config (debounce + volume remaps)
sudo ./run.sh --quiet                  # production mode
sudo ./run.sh --threshold 80           # override threshold
sudo ./mouse-filter --threshold 70     # direct invocation, no remaps
```

### Install as systemd service
```bash
sudo ./install.sh                      # install + enable + start
sudo ./install.sh --uninstall          # stop + disable + remove
```
