# Mouse Tools Project

## What This Is

A collection of mouse management utilities for Linux, built on evdev. A single `mouse-filter` daemon handles:
- **Button debounce** — Fixes hardware switch bounce (worn Omron micro-switches) that causes phantom releases during drags and false double-clicks.
- **Button remapping** — Remaps mouse buttons to keyboard keys (e.g., forward/back → volume up/down). Replaces input-remapper for simple mouse button remaps with zero additional overhead.

## Architecture

Tools:

- **`mouse-filter`** — The main filter. Grabs raw evdev mouse devices, remaps configured buttons, debounces drag releases. Creates virtual `debounced <name>` devices via uinput. Runs as a systemd service or manually via `run.sh`.
- **`mouse-drag-monitor`** — Diagnostic tool. Monitors raw evdev + X11 focus + BT adapter power state to identify the source of phantom releases. Used during investigation, not in production.
- **`run.sh`** — Quick-launch wrapper with standard volume remap config. Passes through extra args.

### Debounce Strategy (Drag-Only Delayed Release)

```
Drag:     press ─────────[>=150ms]───── release(bounce) ── press(bounce) ──── release(real)
Filter:   press ─────────────────────── [hold 70ms...]  ── suppress both ──── [hold 70ms...] → release
Output:   press ──────────────────────────────────────────────────────────────────────────────── release

Click:    press ──[<150ms]── release → forwarded immediately (no delay, no interference)
```

Only drag releases (hold >= 150ms) are delayed for bounce detection. Click releases (hold < 150ms) are forwarded immediately with zero latency — this ensures fast double-clicks always work.

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
| `SUPPRESSED` | Drag-bounce caught (release→re-press within threshold after long hold) | Yes (always) |
| `NEAR-MISS` | Release→re-press gap escaped threshold but within warn window, after drag | Yes (always) |
| `LAG_SPIKE` | Event processing lags behind kernel timestamp | Yes (always) |
| `STATS` | Periodic summary (clicks, suppressions, lag) | Only when notable events occurred |
| `MOVE_DIAG` | Per-stage movement latency breakdown (--diagnose-move only) | Only when non-CLEAN |
| `CLICK_DIAG` | Per-click decision logging (--diagnose-clicks only) | File only (not stdout) |
| Startup config | Full configuration banner | File only in --quiet; both otherwise |

### Log File Management

The log file (`~/.local/share/mouse-filter/debounce.log` or `--log-dir`) is capped to 2000 lines on startup using `tail`. No logrotate dependency.

## Development Rules

1. **TDD: Red → Green → Refactor** — Write or update tests FIRST, verify they fail, then implement. Every PR must include tests.
2. **Requires root for integration testing** — evdev grab + uinput need root. Unit tests should mock evdev where possible.
3. **Test with `pytest`** — Test files in `tests/`. Run: `pytest tests/`
4. **Don't break the filter loop** — The select() event loop is latency-sensitive. Avoid blocking operations, GC pressure, or heavy computation in the hot path.
5. **Log levels matter** — SUPPRESSED, NEAR-MISS, LAG_SPIKE always log. STATS only log in --quiet when notable. Don't add noisy logging to the hot path.

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

- **`DelayedDebouncedMouse`** — The production implementation. Handles remapping, immediate click releases, delayed drag releases, bounce suppression. This is the one that matters.
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
- Verify click releases forwarded immediately, drag releases delayed
- Verify `has_notable_events()` / `record_stats()` for --quiet mode

## Common Tasks

### Adjust threshold
Change `DEBOUNCE_MS` constant (default: 60). CLI override: `--threshold N`. Production uses 70ms.

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

### Deploy via yadm
The service is deployed through `sudo ./install.sh` and verified by `~/.config/yadm/test-dotfiles.sh` (tests under `framework13|linux-bambam`). The `python3-evdev` dependency is in `~/.config/yadm/packages/apt-desktop.txt`.
