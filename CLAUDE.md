# Mouse Tools Project

## What This Is

A collection of mouse management utilities for Linux, built on evdev. A single `mouse-filter` daemon handles:
- **Button remapping** — Remaps mouse buttons to keyboard keys (e.g., forward/back → volume up/down). Replaces input-remapper for simple mouse button remaps with zero additional overhead.
- **Button debounce (opt-in via `--debounce`)** — Suppresses hardware switch bounce (worn Omron micro-switches) that causes phantom releases during drags and false double-clicks. Off by default; the current production mouse hardware does not bounce. The code path is preserved so it can be re-enabled if a future mouse needs it.
- **Wheel hi-res drop (`--wheel-drop-hires`)** — Production default. Drops `REL_WHEEL_HI_RES` events at the forwarding step. Sub-detent phantoms (encoder noise, mechanical micro-rebounds) appear only on the hi-res axis with no notch event behind them, so dropping the axis eliminates the entire phantom class with no decision logic. Apps fall back to `REL_WHEEL` notch events for scroll signal — typically 3 lines per detent in Firefox. Trade-off: loss of smooth-scroll animation between detents.
- **Wheel-bounce suppression (legacy, opt-in via `--wheel-suppress`)** — Drops Type A (single-step sign-flip reversal) and Type B (same-direction re-burst) hardware rebounds on smooth-scroll wheels. Superseded by `--wheel-drop-hires` for the MX 2S. Tunable via `--wheel-cooldown-ms`, `--wheel-cooldown-ratio`, `--wheel-min-primary`, `--wheel-bounce-max-total`, `--wheel-rev-window-ms`, `--wheel-quiet-ms`. Toggle live via `mouse-suppress on|off|toggle` (SIGUSR2 — auth-free via the polkit rule install.sh deploys).
- **Wheel multiplier (`--wheel-multiplier N`)** — Scales forwarded wheel event values by N. Default 1. Combine with `--wheel-drop-hires` for tuning per-detent scroll distance.
- **Wheel scale (`--wheel-scale F`)** — Production default for the MX Anywhere 3S. Scales forwarded vertical wheel values by fraction F (default 1.0) for universal scroll-speed damping — applies to GUI hi-res deltas *and* terminal notch events alike, host and VM. Service runs `0.7` (~0.7x speed): the 3S free-spin wheel (Solaar `hires-smooth-resolution=True`) reports full-rate velocity-proportional hi-res that terminals turn into runaway scroll; scaling at the filter tames it everywhere. (0.7 was hand-tuned for a 1:1 wheel-to-screen feel in the smooth-hi-res regime Solaar applies on connect; an earlier `0.1` over-damped once the device reliably boots into that regime.) A per-axis residual accumulator carries the truncated fraction so fractional scaling stays smooth/drift-free and decimates notches correctly; truncation toward zero never over-scrolls. Composes with `--wheel-multiplier` (effective factor = multiplier × scale).

## Architecture

Tools:

- **`mouse-filter`** — The main filter. Grabs raw evdev mouse devices, remaps configured buttons, debounces drag releases. Creates virtual `debounced <name>` devices via uinput. Runs as a systemd service or manually via `run.sh`.
- **`mouse-drag-monitor`** — Diagnostic tool. Monitors raw evdev + X11 focus + BT adapter power state to identify the source of phantom releases. Used during investigation, not in production.
- **`run.sh`** — Quick-launch wrapper with standard volume remap config. Passes through extra args.

### Debounce Strategy (Drag-Only Delayed Release) — opt-in

Default: `--debounce` OFF. All releases forward immediately; the filter is effectively a transparent passthrough for buttons (remapping still applies).

When `--debounce` is enabled:

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
| `WHEEL_REV` | Wheel scroll direction reversal — candidate rebound (--diagnose-wheel only) | File only (not stdout) |
| `WHEEL_BURST_END` | Wheel burst closed out after idle gap (--diagnose-wheel only) | File only (not stdout) |
| `SUPPRESSED_WHEEL` | Wheel event dropped by suppression filter. Includes reason (type_a_reversal / type_b_cooldown / axis_tie), magnitude, age from primary, primary sum (--wheel-suppress only) | File only (not stdout) |
| `WHEEL_SUPPRESS_TOGGLE` | Suppression state change (on\|off). Emitted at startup and on each SIGUSR2. `mouse-suppress status` reads this. | Both |
| `USER_TAG` | User marker emitted on SIGUSR1 (panel launcher hook). Always emits when received. Triggered via `systemctl kill -s SIGUSR1 mouse-filter.service` (auth-free for invoking user via the polkit rule install.sh deploys). | Both (also stdout) |
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
- **`WheelDiagnostics`** — Wheel-event burst classifier. Per-mouse state machine that groups REL_WHEEL/REL_WHEEL_HI_RES events into directional bursts; emits `WHEEL_REV` on sign-flip events and `WHEEL_BURST_END` on idle close-out (when `emit_logs=True`). Fires `on_burst_closed(dir, abs_sum, end_mono)` callback on every burst close — the integration point that drives `WheelSuppressor`'s anchor. Created per-mouse when `--diagnose-wheel` OR `--wheel-suppress` is active; runs silent (no log emission) when only suppression is on.
- **`WheelSuppressor`** — Active wheel-bounce suppression. Two rules: Type A (single-step opposite-direction reversal within `rev_window_ms`) and Type B (same-direction re-burst within `cooldown_ms`, magnitude-bounded by `min(primary*ratio, bounce_max_total)`). Anchor managed via the WheelDiagnostics callback: same-direction small bursts extend the cooldown (cascade-decay handling); cross-direction small bursts clear the anchor (legitimate reverse). Mid-gesture guard prevents false-positives during continuous scrolling. Axis-tie keeps `REL_WHEEL` and `REL_WHEEL_HI_RES` paired. Hot-path cost is O(1). `set_enabled(b)` is the runtime toggle entry point — driven by `make_wheel_suppress_toggle` SIGUSR2 handler.
- **`_ClosedBurst`** — Plain `__slots__` record holding `(dir, abs_sum, end_mono, was_significant)`. Mutable on purpose: cascade-extension updates `end_mono` in place to extend the cooldown anchor across decay-stage bounces.
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
sudo ./run.sh                                    # default: remaps + drop-hires + diagnose-wheel
sudo ./run.sh --quiet                            # production mode
sudo ./run.sh --debounce --threshold 70          # enable debounce (bouncy hardware)
sudo ./mouse-filter --wheel-suppress             # legacy software suppression
```

### Toggle wheel suppression at runtime
```bash
mouse-suppress status     # current state (on|off|unknown)
mouse-suppress off        # disable without restart, no auth prompt
mouse-suppress on         # re-enable
mouse-suppress            # plain toggle
```
Sends SIGUSR2 to the daemon; the polkit rule installed by install.sh permits this for the active console user without auth.

### Install as systemd service
```bash
sudo ./install.sh                      # install + enable + start
sudo ./install.sh --uninstall          # stop + disable + remove
```

### Deploy via yadm
The service is deployed through `sudo ./install.sh` and verified by `~/.config/yadm/test-dotfiles.sh` (tests under `framework13|linux-bambam`). The `python3-evdev` dependency is in `~/.config/yadm/packages/apt-desktop.txt`.
