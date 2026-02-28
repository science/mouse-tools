"""Tests for mouse-filter filter logic.

Uses mock evdev devices to test the DelayedDebouncedMouse debounce logic
without requiring root access or real hardware.
"""

import time
from unittest.mock import MagicMock, patch

import pytest


# We need to import from the mouse-filter script which has a hyphenated name
# and no .py extension. Use importlib.
import importlib.util
import importlib.machinery
from pathlib import Path

SCRIPT_PATH = Path(__file__).parent.parent / "mouse-filter"


@pytest.fixture
def debounce_module():
    """Import the mouse-filter script as a module."""
    loader = importlib.machinery.SourceFileLoader("mouse_filter", str(SCRIPT_PATH))
    spec = importlib.util.spec_from_loader("mouse_filter", loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_event(type_code, code, value):
    """Create a mock evdev event with realistic kernel timestamps."""
    ev = MagicMock()
    ev.type = type_code
    ev.code = code
    ev.value = value
    # Provide numeric sec/usec so _measure_lag doesn't choke on MagicMock arithmetic
    now = time.time()
    ev.sec = int(now)
    ev.usec = int((now - int(now)) * 1_000_000)
    return ev


class TestDelayedDebouncedMouse:
    """Tests for the delayed-release debounce strategy."""

    def _make_mouse(self, debounce_module, threshold_ms=60, warn_threshold_ms=100):
        """Create a DelayedDebouncedMouse with a mock device."""
        from evdev import ecodes

        mock_device = MagicMock()
        mock_device.name = "Test Mouse"
        mock_device.fd = 99

        mock_uinput = MagicMock()

        with patch.object(debounce_module, 'UInput') as mock_uinput_class:
            mock_uinput_class.from_device.return_value = mock_uinput
            mouse = debounce_module.DelayedDebouncedMouse(
                mock_device, threshold_ms,
                warn_threshold_ms=warn_threshold_ms, quiet=True,
            )

        return mouse, mock_uinput

    def test_non_button_events_pass_through(self, debounce_module):
        """Movement and scroll events should be forwarded immediately."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module)
        ev = make_event(ecodes.EV_REL, ecodes.REL_X, 5)

        result = mouse.process_event(ev)

        assert result is True
        uinput.write_event.assert_called_once_with(ev)

    def test_press_forwarded_immediately(self, debounce_module):
        """A button press should be forwarded without delay."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module)
        ev = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1)

        result = mouse.process_event(ev)

        assert result is True
        uinput.write_event.assert_called_once_with(ev)

    def test_click_release_forwarded_immediately(self, debounce_module):
        """A click release (short hold) should be forwarded immediately."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module)

        # Press first (short hold = click)
        press = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1)
        mouse.process_event(press)
        uinput.write_event.reset_mock()

        # Release after short hold
        release = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0)
        mouse.process_event(release)

        # Click release should be forwarded immediately, not pending
        uinput.write_event.assert_called_once_with(release)
        assert ecodes.BTN_LEFT not in mouse.pending_release

    def test_drag_release_held_not_forwarded_immediately(self, debounce_module):
        """A drag release (long hold) should be queued, not forwarded immediately."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module)

        # Press and hold long enough to be a drag
        press = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1)
        mouse.process_event(press)
        time.sleep(0.2)  # 200ms hold = drag
        uinput.write_event.reset_mock()

        # Release
        release = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0)
        mouse.process_event(release)

        # Drag release should NOT have been forwarded yet
        uinput.write_event.assert_not_called()
        assert ecodes.BTN_LEFT in mouse.pending_release

    def test_drag_release_forwarded_after_threshold(self, debounce_module):
        """A held drag release should be forwarded once the threshold expires."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module, threshold_ms=50)

        # Press and hold long enough to be a drag
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.2)  # 200ms hold = drag
        uinput.write_event.reset_mock()

        release = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0)
        mouse.process_event(release)

        # Not forwarded yet (drag release is pending)
        uinput.write_event.assert_not_called()

        # Wait past threshold
        time.sleep(0.06)
        mouse.flush_pending()

        # Should be forwarded (release + SYN_REPORT)
        calls = uinput.write_event.call_args_list
        assert calls[0].args[0] == release
        assert ecodes.BTN_LEFT not in mouse.pending_release

    def test_bounce_suppressed(self, debounce_module):
        """A release followed by a fast re-press should suppress both (drag bounce)."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module, threshold_ms=60)

        # Initial press — hold long enough to be a drag (>=150ms)
        press1 = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1)
        mouse.process_event(press1)
        time.sleep(0.2)  # 200ms hold = drag
        uinput.write_event.reset_mock()

        # Release (queued in pending)
        release = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0)
        mouse.process_event(release)

        # Re-press within threshold (bounce)
        press2 = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1)
        result = mouse.process_event(press2)

        # Bounce should be suppressed
        assert result is False
        assert mouse.suppressed == 1
        # The release should have been removed from pending
        assert ecodes.BTN_LEFT not in mouse.pending_release
        # uinput should NOT have received the release or the re-press
        uinput.write_event.assert_not_called()

    def test_legitimate_double_click_passes(self, debounce_module):
        """A normal double-click (>threshold gap) should not be suppressed."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module, threshold_ms=50)

        # First click: press + release
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))

        # Wait for release to flush
        time.sleep(0.06)
        mouse.flush_pending()
        uinput.write_event.reset_mock()

        # Second click after a normal gap
        time.sleep(0.05)  # Total gap > threshold
        press2 = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1)
        result = mouse.process_event(press2)

        assert result is True
        assert mouse.suppressed == 0
        uinput.write_event.assert_called_once_with(press2)

    def test_different_buttons_independent(self, debounce_module):
        """Bounce detection should be per-button, not cross-button."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module, threshold_ms=60)

        # Press left, hold as drag, release (pending)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.2)  # 200ms hold = drag
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
        uinput.write_event.reset_mock()

        # Press RIGHT (different button) — should NOT be treated as bounce
        right_press = make_event(ecodes.EV_KEY, ecodes.BTN_RIGHT, 1)
        result = mouse.process_event(right_press)

        assert result is True
        uinput.write_event.assert_called_once_with(right_press)
        # Left drag release should still be pending
        assert ecodes.BTN_LEFT in mouse.pending_release

    def test_click_counter_increments(self, debounce_module):
        """Total clicks should count all presses including suppressed bounces."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module, threshold_ms=60)

        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.2)  # 200ms hold = drag, so bounce suppression applies
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
        # Bounce re-press
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))

        assert mouse.total_clicks == 2
        assert mouse.suppressed == 1

    def test_next_deadline_none_when_empty(self, debounce_module):
        """next_deadline should return None when no releases are pending."""
        mouse, _ = self._make_mouse(debounce_module)
        assert mouse.next_deadline() is None

    def test_next_deadline_returns_remaining_time(self, debounce_module):
        """next_deadline should return time until earliest pending release expires."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module, threshold_ms=60)

        # Must be a drag hold to get a pending release
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.2)  # 200ms hold = drag
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))

        deadline = mouse.next_deadline()
        assert deadline is not None
        assert 0 < deadline <= 0.060

    def test_non_debounced_button_codes_pass_through(self, debounce_module):
        """Key events for non-mouse buttons should pass through unchanged."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module)

        # A keyboard key event happening to come through (shouldn't, but test the guard)
        ev = make_event(ecodes.EV_KEY, 30, 1)  # KEY_A
        result = mouse.process_event(ev)

        assert result is True
        uinput.write_event.assert_called_once_with(ev)


class TestHoldAwareDebounce:
    """Tests for hold-duration-aware debounce.

    The debounce should distinguish between:
    - Fast double-clicks: short hold (<150ms), release, fast re-press → ALLOW
    - Drag bounce: long hold (>=150ms), phantom release, fast re-press → SUPPRESS

    This prevents the debounce filter from eating legitimate fast double-clicks
    while still catching switch bounce during drags.
    """

    def _make_mouse(self, debounce_module, threshold_ms=60, hold_threshold_ms=150):
        from evdev import ecodes

        mock_device = MagicMock()
        mock_device.name = "Test Mouse"
        mock_device.fd = 99
        mock_uinput = MagicMock()

        with patch.object(debounce_module, 'UInput') as mock_uinput_class:
            mock_uinput_class.from_device.return_value = mock_uinput
            mouse = debounce_module.DelayedDebouncedMouse(
                mock_device, threshold_ms,
                hold_threshold_ms=hold_threshold_ms, quiet=True,
            )
        return mouse, mock_uinput

    def test_fast_double_click_not_suppressed(self, debounce_module):
        """Short click followed by fast re-press = legitimate double-click.

        Simulates: press (hold 50ms) → release → press 30ms later.
        The hold was short (<150ms), so this should NOT be debounced.
        """
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module,
                                          threshold_ms=60, hold_threshold_ms=150)

        # First click: short hold
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.050)  # 50ms hold = short click

        # Release
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
        uinput.write_event.reset_mock()

        # Fast re-press (30ms later, within debounce threshold)
        time.sleep(0.030)
        press2 = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1)
        result = mouse.process_event(press2)

        # Should NOT be suppressed — this is a fast double-click
        assert result is True
        assert mouse.suppressed == 0

    def test_drag_bounce_still_suppressed(self, debounce_module):
        """Long hold followed by fast re-press = drag bounce.

        Simulates: press (hold 300ms) → phantom release → re-press 40ms later.
        The hold was long (>=150ms), so this IS bounce and should be suppressed.
        """
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module,
                                          threshold_ms=60, hold_threshold_ms=150)

        # Press and hold for a long drag
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.300)  # 300ms hold = drag

        # Phantom release (switch bounce)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
        uinput.write_event.reset_mock()

        # Bounce re-press 40ms later
        time.sleep(0.040)
        press2 = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1)
        result = mouse.process_event(press2)

        # Should be suppressed — this is drag bounce
        assert result is False
        assert mouse.suppressed == 1

    def test_hold_at_boundary_is_debounced(self, debounce_module):
        """Hold exactly at the hold threshold should be treated as a drag."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module,
                                          threshold_ms=60, hold_threshold_ms=150)

        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.160)  # Just over 150ms

        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
        uinput.write_event.reset_mock()

        time.sleep(0.030)
        result = mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))

        assert result is False
        assert mouse.suppressed == 1

    def test_short_hold_release_forwarded_immediately(self, debounce_module):
        """Short holds (clicks) should have their release forwarded immediately,
        not delayed. This ensures fast double-clicks are never disrupted."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module,
                                          threshold_ms=60, hold_threshold_ms=150)

        # Short click
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.050)
        uinput.write_event.reset_mock()

        release = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0)
        mouse.process_event(release)

        # Release should be forwarded immediately, not pending
        uinput.write_event.assert_called_once_with(release)
        assert ecodes.BTN_LEFT not in mouse.pending_release


class TestFlushSynReport:
    """Tests for SYN_REPORT after flushed releases.

    uinput virtual devices batch events and only deliver them to consumers
    on EV_SYN/SYN_REPORT. When we hold a button release and flush it later
    (after the debounce threshold), the flushed release must be followed by
    a SYN_REPORT. Otherwise the release sits in the uinput buffer until the
    next mouse movement event, causing clicks to appear "stuck" when the
    mouse is stationary.

    Bug: click a button without moving the mouse → release never delivered
    until mouse moves.
    """

    def _make_mouse(self, debounce_module, threshold_ms=50):
        from evdev import ecodes

        mock_device = MagicMock()
        mock_device.name = "Test Mouse"
        mock_device.fd = 99

        mock_uinput = MagicMock()

        with patch.object(debounce_module, 'UInput') as mock_uinput_class:
            mock_uinput_class.from_device.return_value = mock_uinput
            mouse = debounce_module.DelayedDebouncedMouse(
                mock_device, threshold_ms, quiet=True,
            )

        return mouse, mock_uinput

    def test_flushed_release_followed_by_syn(self, debounce_module):
        """A flushed pending drag release must be followed by SYN_REPORT.

        Simulates: user drags a button, releases, doesn't move the mouse,
        waits for threshold to expire. The flush must emit both the release
        event AND a SYN_REPORT so uinput delivers it immediately.
        """
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module, threshold_ms=50)

        # Drag: press, hold, then release (no movement after release)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.2)  # 200ms hold = drag
        uinput.write_event.reset_mock()

        release = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0)
        mouse.process_event(release)

        # Wait past threshold and flush
        time.sleep(0.06)
        mouse.flush_pending()

        # Should have TWO write_event calls: the release + SYN_REPORT
        calls = uinput.write_event.call_args_list
        assert len(calls) >= 2, (
            f"Expected release + SYN_REPORT, got {len(calls)} calls: {calls}"
        )

        # First call: the release event
        assert calls[0].args[0] == release

        # Second call: must be a SYN_REPORT
        syn_event = calls[1].args[0]
        assert syn_event.type == ecodes.EV_SYN
        assert syn_event.code == ecodes.SYN_REPORT
        assert syn_event.value == 0

    def test_suppressed_bounce_no_syn(self, debounce_module):
        """When bounce is suppressed, no SYN_REPORT should be emitted.

        If a release is pending and a re-press arrives (drag bounce), both are
        suppressed. No events should be written, including no SYN_REPORT.
        """
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module, threshold_ms=60)

        # Press and hold long enough to be a drag
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.2)  # 200ms hold = drag
        uinput.write_event.reset_mock()

        # Release (pending)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))

        # Bounce re-press before threshold
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))

        # Nothing should have been forwarded
        uinput.write_event.assert_not_called()

    def test_multiple_buttons_flushed_get_single_syn(self, debounce_module):
        """If multiple drag releases flush at the same time, one SYN_REPORT
        after all of them is sufficient (but one per release is also acceptable)."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module, threshold_ms=50)

        # Press and hold two buttons as drags, then release
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_RIGHT, 1))
        time.sleep(0.2)  # 200ms hold = drag for both
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_RIGHT, 0))
        uinput.write_event.reset_mock()

        time.sleep(0.06)
        mouse.flush_pending()

        calls = uinput.write_event.call_args_list
        # Must have at least one SYN_REPORT
        syn_calls = [c for c in calls if c.args[0].type == ecodes.EV_SYN]
        assert len(syn_calls) >= 1, (
            f"Expected at least one SYN_REPORT after flushing releases, "
            f"got calls: {calls}"
        )


class TestButtonRemapping:
    """Tests for button remapping (e.g., forward/back → volume up/down)."""

    def _make_mouse(self, debounce_module, threshold_ms=60, remap=None):
        from evdev import ecodes

        mock_device = MagicMock()
        mock_device.name = "Test Mouse"
        mock_device.fd = 99
        mock_uinput = MagicMock()

        with patch.object(debounce_module, 'UInput') as mock_uinput_class:
            # With remap: UInput(events=...) constructor is called
            mock_uinput_class.return_value = mock_uinput
            # Without remap: UInput.from_device() is called
            mock_uinput_class.from_device.return_value = mock_uinput
            mouse = debounce_module.DelayedDebouncedMouse(
                mock_device, threshold_ms, quiet=True,
                button_remap=remap,
            )
        return mouse, mock_uinput

    def test_remapped_button_press_uses_new_code(self, debounce_module):
        """A remapped button press should be forwarded with the new key code."""
        from evdev import ecodes

        remap = {ecodes.BTN_EXTRA: ecodes.KEY_VOLUMEUP}
        mouse, uinput = self._make_mouse(debounce_module, remap=remap)

        ev = make_event(ecodes.EV_KEY, ecodes.BTN_EXTRA, 1)
        result = mouse.process_event(ev)

        assert result is True
        written = uinput.write_event.call_args_list[-1].args[0]
        assert written.code == ecodes.KEY_VOLUMEUP
        assert written.value == 1

    def test_remapped_button_release_uses_new_code(self, debounce_module):
        """A remapped button release should be forwarded with the new key code."""
        from evdev import ecodes

        remap = {ecodes.BTN_EXTRA: ecodes.KEY_VOLUMEUP}
        mouse, uinput = self._make_mouse(debounce_module, remap=remap)

        # Press then release
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_EXTRA, 1))
        uinput.write_event.reset_mock()

        ev = make_event(ecodes.EV_KEY, ecodes.BTN_EXTRA, 0)
        result = mouse.process_event(ev)

        assert result is True
        written = uinput.write_event.call_args_list[-1].args[0]
        assert written.code == ecodes.KEY_VOLUMEUP
        assert written.value == 0

    def test_remapped_button_not_debounced(self, debounce_module):
        """Remapped buttons should be forwarded immediately, not debounced."""
        from evdev import ecodes

        remap = {ecodes.BTN_EXTRA: ecodes.KEY_VOLUMEUP}
        mouse, uinput = self._make_mouse(debounce_module, remap=remap)

        # Press
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_EXTRA, 1))
        uinput.write_event.reset_mock()

        # Release — should be forwarded immediately, NOT queued
        ev = make_event(ecodes.EV_KEY, ecodes.BTN_EXTRA, 0)
        mouse.process_event(ev)

        uinput.write_event.assert_called_once()
        assert ecodes.BTN_EXTRA not in mouse.pending_release

    def test_non_remapped_buttons_still_debounced(self, debounce_module):
        """Buttons not in the remap dict should still go through debounce for drags."""
        from evdev import ecodes

        remap = {ecodes.BTN_EXTRA: ecodes.KEY_VOLUMEUP}
        mouse, uinput = self._make_mouse(debounce_module, remap=remap)

        # BTN_LEFT is not remapped — drag release should be debounced
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.2)  # 200ms hold = drag
        uinput.write_event.reset_mock()

        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))

        # Drag release should be pending (debounced), not forwarded
        uinput.write_event.assert_not_called()
        assert ecodes.BTN_LEFT in mouse.pending_release

    def test_multiple_remaps(self, debounce_module):
        """Multiple buttons can be remapped simultaneously."""
        from evdev import ecodes

        remap = {
            ecodes.BTN_EXTRA: ecodes.KEY_VOLUMEUP,
            ecodes.BTN_SIDE: ecodes.KEY_VOLUMEDOWN,
            ecodes.BTN_MIDDLE: ecodes.KEY_MUTE,
        }
        mouse, uinput = self._make_mouse(debounce_module, remap=remap)

        # Test each remapped button
        for src, dst in remap.items():
            uinput.write_event.reset_mock()
            ev = make_event(ecodes.EV_KEY, src, 1)
            mouse.process_event(ev)
            written = uinput.write_event.call_args_list[-1].args[0]
            assert written.code == dst, f"Expected {dst} for {src}, got {written.code}"

    def test_no_remap_default(self, debounce_module):
        """With no remap configured, drag releases go through normal debounce."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module, remap=None)

        # BTN_EXTRA drag should be debounced normally (no remap)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_EXTRA, 1))
        time.sleep(0.2)  # 200ms hold = drag
        uinput.write_event.reset_mock()

        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_EXTRA, 0))

        # Drag release should be pending (debounced)
        assert ecodes.BTN_EXTRA in mouse.pending_release


class TestFindMice:
    """Tests for mouse device auto-detection."""

    def test_skips_virtual_devices(self, debounce_module):
        """find_mice should skip input-remapper and uinput virtual devices."""
        from evdev import ecodes

        real_mouse = MagicMock()
        real_mouse.name = "MX Anywhere 2S Mouse"
        real_mouse.phys = "cc:15:31:96:1f:58"
        real_mouse.capabilities.return_value = {
            ecodes.EV_KEY: [ecodes.BTN_LEFT],
            ecodes.EV_REL: [ecodes.REL_X],
        }

        virtual_mouse = MagicMock()
        virtual_mouse.name = "input-remapper MX Anywhere 2S Mouse forwarded"
        virtual_mouse.phys = "py-evdev-uinput"
        virtual_mouse.capabilities.return_value = {
            ecodes.EV_KEY: [ecodes.BTN_LEFT],
            ecodes.EV_REL: [ecodes.REL_X],
        }

        debounced_mouse = MagicMock()
        debounced_mouse.name = "debounced MX Anywhere 2S Mouse"
        debounced_mouse.phys = "py-evdev-uinput"
        debounced_mouse.capabilities.return_value = {
            ecodes.EV_KEY: [ecodes.BTN_LEFT],
            ecodes.EV_REL: [ecodes.REL_X],
        }

        with patch('evdev.list_devices', return_value=['/dev/input/event1', '/dev/input/event2', '/dev/input/event3']):
            with patch('evdev.InputDevice', side_effect=[real_mouse, virtual_mouse, debounced_mouse]):
                mice = debounce_module.find_mice()

        assert len(mice) == 1
        assert mice[0].name == "MX Anywhere 2S Mouse"


class TestDeviceReconnection:
    """Tests for automatic device reconnection after disconnect.

    When a mouse disconnects (e.g., Bluetooth auto-power-off, USB unplug),
    mouse-filter should detect when it comes back via inotify on /dev/input/
    and reconnect immediately. No polling — inotify gives us a pollable FD
    for the existing select() loop with zero overhead until an event fires.

    Bug evidence from production logs:
        [16:25:11] Device disconnected: MX Anywhere 2S Mouse
        ... (7 hours of silence, no reconnection attempt) ...
        [23:34:47] Debounce filter stopped.  (manual restart)
    """

    def test_find_new_mice_excludes_existing(self, debounce_module):
        """find_new_mice returns only devices not already being monitored."""
        existing = MagicMock()
        existing.path = "/dev/input/event10"
        existing.phys = "usb-0000:00:14.0-1/input0"
        existing.name = "Already Monitored Mouse"

        new_dev = MagicMock()
        new_dev.path = "/dev/input/event11"
        new_dev.phys = "bt-addr"
        new_dev.name = "Reconnected Mouse"

        with patch.object(debounce_module, 'find_mice', return_value=[existing, new_dev]):
            result = debounce_module.find_new_mice({"/dev/input/event10"})

        assert len(result) == 1
        assert result[0] is new_dev

    def test_find_new_mice_closes_duplicate_fds(self, debounce_module):
        """find_new_mice closes FDs for already-monitored devices to prevent leaks."""
        dup = MagicMock()
        dup.path = "/dev/input/event5"

        with patch.object(debounce_module, 'find_mice', return_value=[dup]):
            result = debounce_module.find_new_mice({"/dev/input/event5"})

        assert result == []
        dup.close.assert_called_once()

    def test_find_new_mice_returns_all_when_none_active(self, debounce_module):
        """With no active devices, all discovered mice are new."""
        mouse = MagicMock()
        mouse.path = "/dev/input/event7"

        with patch.object(debounce_module, 'find_mice', return_value=[mouse]):
            result = debounce_module.find_new_mice(set())

        assert len(result) == 1
        assert result[0] is mouse
        mouse.close.assert_not_called()

    def test_reconnect_scenario_end_to_end(self, debounce_module):
        """Full reconnection flow: setup → disconnect → inotify → reconnect.

        Simulates the exact scenario from production: BT mouse powers off,
        device disconnects (OSError), mouse powers back on and reappears
        at a possibly different event path, inotify fires, check_new_device
        identifies it, filter picks it up.
        """
        from evdev import ecodes

        # 1. Initial setup — one mouse being monitored
        mock_device = MagicMock()
        mock_device.name = "MX Anywhere 2S Mouse"
        mock_device.path = "/dev/input/event22"
        mock_device.fd = 10
        mock_uinput = MagicMock()

        with patch.object(debounce_module, 'UInput') as mock_cls:
            mock_cls.from_device.return_value = mock_uinput
            dm = debounce_module.DelayedDebouncedMouse(mock_device, 60, quiet=True)

        mice = [dm]
        fd_map = {dm.device.fd: dm}

        # 2. Device disconnects (simulating OSError path in main loop)
        dm.close()
        del fd_map[dm.device.fd]
        mice.remove(dm)

        assert len(mice) == 0
        assert len(fd_map) == 0

        # 3. Inotify fires for reconnected device (different event path)
        active_paths = {m.device.path for m in mice}
        reconnected = MagicMock()
        reconnected.name = "MX Anywhere 2S Mouse"
        reconnected.path = "/dev/input/event25"
        reconnected.phys = "cc:15:31:96:1f:58"
        reconnected.fd = 14
        reconnected.capabilities.return_value = {
            ecodes.EV_KEY: [ecodes.BTN_LEFT],
            ecodes.EV_REL: [ecodes.REL_X],
        }

        with patch('evdev.InputDevice', return_value=reconnected):
            dev = debounce_module.check_new_device(
                '/dev/input/event25', active_paths)

        assert dev is not None
        assert dev.name == "MX Anywhere 2S Mouse"

        # 4. New device gets set up successfully
        new_uinput = MagicMock()
        with patch.object(debounce_module, 'UInput') as mock_cls:
            mock_cls.from_device.return_value = new_uinput
            new_dm = debounce_module.DelayedDebouncedMouse(
                reconnected, 60, quiet=True)

        mice.append(new_dm)
        fd_map[new_dm.device.fd] = new_dm

        assert len(mice) == 1
        assert len(fd_map) == 1

        # Verify the new device is functional (can process events)
        ev = make_event(ecodes.EV_REL, ecodes.REL_X, 5)
        new_dm.process_event(ev)
        new_uinput.write_event.assert_called_with(ev)


class TestMoveDiagnostics:
    """Tests for MoveDiagnostics — per-interval movement pipeline telemetry."""

    def _make_diag(self, debounce_module, lag_threshold_ms=10):
        return debounce_module.MoveDiagnostics(lag_threshold_ms=lag_threshold_ms)

    def _make_mouse_with_diag(self, debounce_module, threshold_ms=60):
        """Create a DelayedDebouncedMouse with diagnose_move=True."""
        from evdev import ecodes

        mock_device = MagicMock()
        mock_device.name = "Test Mouse"
        mock_device.fd = 99
        mock_uinput = MagicMock()

        with patch.object(debounce_module, 'UInput') as mock_uinput_class:
            mock_uinput_class.from_device.return_value = mock_uinput
            mouse = debounce_module.DelayedDebouncedMouse(
                mock_device, threshold_ms, quiet=True,
                diagnose_move=True,
            )
        return mouse, mock_uinput

    def test_move_diag_created_when_enabled(self, debounce_module):
        """DelayedDebouncedMouse(diagnose_move=True) should create a MoveDiagnostics."""
        mouse, _ = self._make_mouse_with_diag(debounce_module)
        assert isinstance(mouse._move_diag, debounce_module.MoveDiagnostics)

    def test_move_diag_not_created_by_default(self, debounce_module):
        """Default construction should have _move_diag is None."""
        from evdev import ecodes

        mock_device = MagicMock()
        mock_device.name = "Test Mouse"
        mock_device.fd = 99
        mock_uinput = MagicMock()

        with patch.object(debounce_module, 'UInput') as mock_uinput_class:
            mock_uinput_class.from_device.return_value = mock_uinput
            mouse = debounce_module.DelayedDebouncedMouse(
                mock_device, 60, quiet=True,
            )
        assert mouse._move_diag is None

    def test_record_input_counts_movements(self, debounce_module):
        """record_input() should increment move_count."""
        diag = self._make_diag(debounce_module)
        for _ in range(5):
            diag.record_input(1.0)
        assert diag.move_count == 5

    def test_record_input_tracks_max_lag(self, debounce_module):
        """record_input() should track the maximum lag value."""
        diag = self._make_diag(debounce_module)
        for lag in [5.0, 12.0, 3.0]:
            diag.record_input(lag)
        assert diag.move_max_lag_ms == 12.0

    def test_record_input_counts_lag_spikes(self, debounce_module):
        """record_input() should count values exceeding lag threshold."""
        diag = self._make_diag(debounce_module, lag_threshold_ms=10)
        for lag in [5, 15, 8, 20]:
            diag.record_input(lag)
        assert diag.move_lag_spikes == 2

    def test_hz_calculation(self, debounce_module):
        """hz() should return approximate event rate."""
        diag = self._make_diag(debounce_module)
        # Simulate 100 events over ~0.1s
        diag._interval_start = time.monotonic() - 0.1
        diag.move_count = 100
        hz = diag.hz()
        assert 900 < hz < 1100  # ~1000 Hz with timing tolerance

    def test_record_batch_tracking(self, debounce_module):
        """record_batch() should track max batch size and total batches."""
        diag = self._make_diag(debounce_module)
        diag.record_batch(1)
        diag.record_batch(5)
        diag.record_batch(3)
        assert diag.max_batch_size == 5
        assert diag.total_batches == 3

    def test_large_batch_detection(self, debounce_module):
        """record_batch() should count batches exceeding threshold."""
        diag = self._make_diag(debounce_module)
        diag.record_batch(15)
        assert diag.large_batch_count == 1

    def test_record_loop_time(self, debounce_module):
        """record_loop_time() should track max and count stalls."""
        diag = self._make_diag(debounce_module)
        diag.record_loop_time(0.5)
        diag.record_loop_time(3.0)
        diag.record_loop_time(1.0)
        assert diag.loop_max_ms == 3.0
        assert diag.loop_stall_count == 1  # 3.0ms > 2ms threshold

    def test_record_write(self, debounce_module):
        """record_write() should track write count and slow writes."""
        diag = self._make_diag(debounce_module)
        diag.record_write(0.0001)  # 0.1ms — fast
        diag.record_write(0.001)   # 1.0ms — slow (>0.5ms)
        assert diag.write_count == 2
        assert diag.write_slow_count == 1
        assert diag.write_max_s == 0.001

    def test_verdict_clean(self, debounce_module):
        """Fresh instance with no issues should report CLEAN."""
        diag = self._make_diag(debounce_module)
        assert diag.verdict() == "CLEAN"

    def test_verdict_input_lag(self, debounce_module):
        """Lag spikes should produce INPUT_LAG verdict."""
        diag = self._make_diag(debounce_module)
        diag.move_lag_spikes = 1
        assert diag.verdict() == "INPUT_LAG"

    def test_verdict_priority(self, debounce_module):
        """INPUT_LAG should take priority over LOOP_STALL."""
        diag = self._make_diag(debounce_module)
        diag.move_lag_spikes = 1
        diag.loop_stall_count = 1
        assert diag.verdict() == "INPUT_LAG"

    def test_verdict_loop_stall(self, debounce_module):
        """Loop stalls without input lag should produce LOOP_STALL."""
        diag = self._make_diag(debounce_module)
        diag.loop_stall_count = 1
        assert diag.verdict() == "LOOP_STALL"

    def test_verdict_write_lag(self, debounce_module):
        """Slow writes should produce WRITE_LAG verdict."""
        diag = self._make_diag(debounce_module)
        diag.write_slow_count = 1
        assert diag.verdict() == "WRITE_LAG"

    def test_verdict_x11_stall(self, debounce_module):
        """X11 stalls should produce X11_STALL verdict."""
        diag = self._make_diag(debounce_module)
        diag.x11_stalls = 1
        assert diag.verdict() == "X11_STALL"

    def test_report_format(self, debounce_module):
        """report() should contain expected fields."""
        diag = self._make_diag(debounce_module)
        diag.record_input(0.8)
        report = diag.report()
        assert "MOVE_DIAG:" in report
        assert "rate=" in report
        assert "input(" in report
        assert "-> " in report

    def test_reset_clears_interval_counters(self, debounce_module):
        """reset() should zero interval counters but preserve _move_forwarded_count."""
        diag = self._make_diag(debounce_module)
        diag.record_input(5.0)
        diag.record_input(15.0)
        diag.record_batch(3)
        diag.record_loop_time(3.0)
        diag.record_write(0.001)
        diag._move_forwarded_count = 42

        diag.reset()

        assert diag.move_count == 0
        assert diag.move_max_lag_ms == 0.0
        assert diag.move_lag_spikes == 0
        assert diag.max_batch_size == 0
        assert diag.total_batches == 0
        assert diag.large_batch_count == 0
        assert diag.loop_max_ms == 0.0
        assert diag.loop_stall_count == 0
        assert diag.write_count == 0
        assert diag.write_slow_count == 0
        assert diag.write_max_s == 0.0
        assert diag.x11_stalls == 0
        # _move_forwarded_count must be preserved (X11 probe needs it monotonically increasing)
        assert diag._move_forwarded_count == 42


class TestMoveDiagIntegration:
    """Tests for MoveDiagnostics hooks in DelayedDebouncedMouse."""

    def _make_mouse(self, debounce_module, threshold_ms=60, diagnose_move=True):
        from evdev import ecodes

        mock_device = MagicMock()
        mock_device.name = "Test Mouse"
        mock_device.fd = 99
        mock_uinput = MagicMock()

        with patch.object(debounce_module, 'UInput') as mock_uinput_class:
            mock_uinput_class.from_device.return_value = mock_uinput
            mouse = debounce_module.DelayedDebouncedMouse(
                mock_device, threshold_ms, quiet=True,
                diagnose_move=diagnose_move,
            )
        return mouse, mock_uinput

    def test_movement_event_recorded_in_diag(self, debounce_module):
        """EV_REL events should be recorded in move diagnostics."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module)
        ev = make_event(ecodes.EV_REL, ecodes.REL_X, 5)
        mouse.process_event(ev)

        assert mouse._move_diag.move_count == 1

    def test_button_event_not_recorded_in_diag(self, debounce_module):
        """EV_KEY events should NOT be recorded in move diagnostics."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module)
        ev = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1)
        mouse.process_event(ev)

        assert mouse._move_diag.move_count == 0

    def test_write_timing_recorded_for_movement(self, debounce_module):
        """Movement event writes should be timed in diagnostics."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module)
        ev = make_event(ecodes.EV_REL, ecodes.REL_X, 5)
        mouse.process_event(ev)

        assert mouse._move_diag.write_count == 1

    def test_write_timing_not_recorded_for_buttons(self, debounce_module):
        """Button event writes should NOT be timed in diagnostics."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module)
        ev = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1)
        mouse.process_event(ev)

        assert mouse._move_diag.write_count == 0

    def test_movement_passthrough_unchanged_with_diag(self, debounce_module):
        """Movement events should still be forwarded correctly with diag active."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module)
        ev = make_event(ecodes.EV_REL, ecodes.REL_X, 5)
        result = mouse.process_event(ev)

        assert result is True
        uinput.write_event.assert_called_once_with(ev)

    def test_measure_lag_returns_value(self, debounce_module):
        """_measure_lag() should return a float on second+ calls."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module)

        # First call calibrates, returns None
        ev1 = make_event(ecodes.EV_REL, ecodes.REL_X, 1)
        result1 = mouse._measure_lag(ev1)
        assert result1 is None

        # Second call with kernel timestamp 5ms in the past to guarantee
        # positive lag (avoids recalibration from near-zero jitter)
        ev2 = make_event(ecodes.EV_REL, ecodes.REL_X, 1)
        ev2.usec -= 5000  # 5ms older kernel timestamp
        result2 = mouse._measure_lag(ev2)
        assert isinstance(result2, float)

    def test_move_forwarded_count_increments(self, debounce_module):
        """_move_forwarded_count should increment for each movement write."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module)
        for _ in range(3):
            mouse.process_event(make_event(ecodes.EV_REL, ecodes.REL_X, 5))

        assert mouse._move_diag._move_forwarded_count == 3


class TestX11PointerProbe:
    """Tests for X11PointerProbe stall detection logic."""

    def _make_probe(self, debounce_module):
        diag = debounce_module.MoveDiagnostics()
        probe = debounce_module.X11PointerProbe(diag)
        return probe, diag

    def test_check_stall_detects_stall(self, debounce_module):
        """10+ polls with increasing count but same position = stall."""
        probe, diag = self._make_probe(debounce_module)
        # Initial call to establish position baseline
        probe._check_stall(100, 200, count=0)
        # Simulate 10 consecutive stall samples (events forwarded, pointer stuck)
        for i in range(10):
            result = probe._check_stall(100, 200, count=i + 1)
        assert result is True
        assert diag.x11_stalls >= 1

    def test_check_stall_no_false_positive_when_idle(self, debounce_module):
        """Same position and same count (no new events) = not a stall."""
        probe, diag = self._make_probe(debounce_module)
        for _ in range(15):
            result = probe._check_stall(100, 200, count=0)
        assert diag.x11_stalls == 0

    def test_check_stall_no_false_positive_when_moving(self, debounce_module):
        """Increasing count with changing position = pointer is moving, no stall."""
        probe, diag = self._make_probe(debounce_module)
        for i in range(15):
            probe._check_stall(100 + i, 200 + i, count=i + 1)
        assert diag.x11_stalls == 0

    def test_check_stall_resets_on_movement(self, debounce_module):
        """Stall counter should reset when pointer moves again."""
        probe, diag = self._make_probe(debounce_module)
        # Build up 5 stall samples (not enough for a stall event)
        for i in range(5):
            probe._check_stall(100, 200, count=i + 1)
        # Pointer moves — should reset counter
        probe._check_stall(110, 210, count=6)
        # Continue with same position — needs 10 more to trigger
        for i in range(9):
            probe._check_stall(110, 210, count=7 + i)
        assert diag.x11_stalls == 0  # Not enough after reset

    def test_graceful_degradation(self, debounce_module):
        """start() should return False when DISPLAY is unavailable."""
        diag = debounce_module.MoveDiagnostics()
        probe = debounce_module.X11PointerProbe(diag)
        with patch.dict('os.environ', {}, clear=True):
            # Remove DISPLAY from environment
            result = probe.start()
        assert result is False


class TestDeviceMonitor:
    """Tests for inotify-based device hotplug monitoring.

    Instead of polling find_mice() every N seconds (which blocks the event
    loop for 50-350ms scanning all /dev/input/* devices), we use inotify
    to watch /dev/input/ for new device files. The inotify FD goes into
    the existing select() call — zero overhead until a device actually
    connects. When it fires, we check only the specific new device path.
    """

    def test_drain_inotify_parses_event_files(self, debounce_module):
        """drain_inotify should extract /dev/input/event* paths from data."""
        import struct as s
        name = b'event25\0'
        data = s.pack('iIII', 1, 0x100, 0, len(name)) + name

        with patch('os.read', return_value=data):
            paths = debounce_module.drain_inotify(5)

        assert paths == ['/dev/input/event25']

    def test_drain_inotify_ignores_non_event_files(self, debounce_module):
        """drain_inotify should ignore non-event files (mice, js0, etc)."""
        import struct as s
        # "mice" device node — not an event* file, should be ignored
        name = b'mice\0\0\0\0'
        data = s.pack('iIII', 1, 0x100, 0, len(name)) + name

        with patch('os.read', return_value=data):
            paths = debounce_module.drain_inotify(5)

        assert paths == []

    def test_drain_inotify_handles_multiple_events(self, debounce_module):
        """drain_inotify should parse multiple inotify events in one read."""
        import struct as s
        name1 = b'event10\0'
        name2 = b'event11\0'
        data = (s.pack('iIII', 1, 0x100, 0, len(name1)) + name1 +
                s.pack('iIII', 1, 0x100, 0, len(name2)) + name2)

        with patch('os.read', return_value=data):
            paths = debounce_module.drain_inotify(5)

        assert paths == ['/dev/input/event10', '/dev/input/event11']

    def test_drain_inotify_handles_read_error(self, debounce_module):
        """drain_inotify should return empty list on read error."""
        with patch('os.read', side_effect=OSError("EAGAIN")):
            paths = debounce_module.drain_inotify(5)

        assert paths == []

    def test_check_new_device_finds_mouse(self, debounce_module):
        """check_new_device should return device for a physical mouse."""
        from evdev import ecodes

        mock_dev = MagicMock()
        mock_dev.name = "Test Mouse"
        mock_dev.phys = "usb-0000:00:14.0-1/input0"
        mock_dev.capabilities.return_value = {
            ecodes.EV_KEY: [ecodes.BTN_LEFT],
            ecodes.EV_REL: [ecodes.REL_X],
        }

        with patch('evdev.InputDevice', return_value=mock_dev):
            result = debounce_module.check_new_device(
                '/dev/input/event5', set())

        assert result is mock_dev
        mock_dev.close.assert_not_called()

    def test_check_new_device_rejects_already_monitored(self, debounce_module):
        """check_new_device should return None for already-monitored paths."""
        result = debounce_module.check_new_device(
            '/dev/input/event5', {'/dev/input/event5'})
        assert result is None

    def test_check_new_device_rejects_virtual(self, debounce_module):
        """check_new_device should reject uinput virtual devices."""
        from evdev import ecodes

        mock_dev = MagicMock()
        mock_dev.name = "debounced Test Mouse"
        mock_dev.phys = "py-evdev-uinput"
        mock_dev.capabilities.return_value = {
            ecodes.EV_KEY: [ecodes.BTN_LEFT],
            ecodes.EV_REL: [ecodes.REL_X],
        }

        with patch('evdev.InputDevice', return_value=mock_dev):
            result = debounce_module.check_new_device(
                '/dev/input/event5', set())

        assert result is None
        mock_dev.close.assert_called_once()

    def test_check_new_device_rejects_non_mouse(self, debounce_module):
        """check_new_device should reject devices without mouse capabilities."""
        from evdev import ecodes

        mock_dev = MagicMock()
        mock_dev.name = "USB Keyboard"
        mock_dev.phys = "usb-0000:00:14.0-2/input0"
        mock_dev.capabilities.return_value = {
            ecodes.EV_KEY: [30, 31, 32],  # letter keys, no BTN_LEFT
        }

        with patch('evdev.InputDevice', return_value=mock_dev):
            result = debounce_module.check_new_device(
                '/dev/input/event5', set())

        assert result is None
        mock_dev.close.assert_called_once()

    def test_main_uses_inotify_not_polling(self, debounce_module):
        """main() should use inotify for hotplug, not periodic polling.

        Polling find_mice() every N seconds blocks the event loop for
        50-350ms, causing lag spikes. inotify is event-driven with zero
        overhead until a device actually connects.
        """
        import inspect
        source = inspect.getsource(debounce_module.main)

        assert 'init_inotify' in source, (
            "main() should use inotify to watch /dev/input/ for new devices"
        )
        assert 'drain_inotify' in source, (
            "main() should drain inotify events when the monitor FD is readable"
        )


class TestClickDiagnostics:
    """Tests for --diagnose-clicks click-level telemetry.

    When diagnose_clicks=True, the filter logs every press/release/flush
    with full timing details to the log file (not stdout). This helps
    diagnose double-click issues by making all click decision paths visible.
    """

    def _make_mouse(self, debounce_module, threshold_ms=60, hold_threshold_ms=150,
                    diagnose_clicks=True):
        from evdev import ecodes

        mock_device = MagicMock()
        mock_device.name = "Test Mouse"
        mock_device.fd = 99
        mock_uinput = MagicMock()

        with patch.object(debounce_module, 'UInput') as mock_uinput_class:
            mock_uinput_class.from_device.return_value = mock_uinput
            mouse = debounce_module.DelayedDebouncedMouse(
                mock_device, threshold_ms,
                hold_threshold_ms=hold_threshold_ms, quiet=True,
                diagnose_clicks=diagnose_clicks,
            )
        return mouse, mock_uinput

    def test_diagnose_clicks_state_initialized(self, debounce_module):
        """diagnose_clicks=True should initialize tracking state."""
        mouse, _ = self._make_mouse(debounce_module)
        assert mouse.diagnose_clicks is True
        assert mouse.last_press_forwarded == {}

    def test_diagnose_clicks_disabled_by_default(self, debounce_module):
        """Default construction should have diagnose_clicks=False."""
        mouse, _ = self._make_mouse(debounce_module, diagnose_clicks=False)
        assert mouse.diagnose_clicks is False

    def test_normal_press_logs_click_diag(self, debounce_module):
        """Normal press should log CLICK_DIAG with path=normal."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module)

        with patch.object(debounce_module, 'log') as mock_log:
            mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))

        diag_calls = [c for c in mock_log.call_args_list
                      if 'CLICK_DIAG' in str(c) and 'PRESS' in str(c)]
        assert len(diag_calls) == 1
        msg = diag_calls[0].args[0]
        assert 'LEFT PRESS' in msg
        assert 'path=normal' in msg
        assert diag_calls[0].kwargs.get('also_print') is False

    def test_normal_press_logs_pp_gap(self, debounce_module):
        """Second press should show press-to-press gap."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module)

        # First press
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        # Release + flush
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
        time.sleep(0.07)
        mouse.flush_pending()

        time.sleep(0.05)

        with patch.object(debounce_module, 'log') as mock_log:
            mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))

        diag_calls = [c for c in mock_log.call_args_list
                      if 'CLICK_DIAG' in str(c) and 'PRESS' in str(c)]
        assert len(diag_calls) == 1
        msg = diag_calls[0].args[0]
        assert 'pp_gap=' in msg
        assert 'pp_gap=--' not in msg  # Should have a real value

    def test_release_logs_click_diag(self, debounce_module):
        """Click release should log CLICK_DIAG with hold duration and forwarded."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.050)

        with patch.object(debounce_module, 'log') as mock_log:
            mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))

        diag_calls = [c for c in mock_log.call_args_list
                      if 'CLICK_DIAG' in str(c) and 'RELEASE' in str(c)]
        assert len(diag_calls) == 1
        msg = diag_calls[0].args[0]
        assert 'LEFT RELEASE' in msg
        assert 'hold=' in msg
        assert 'type=click -> forwarded' in msg
        assert diag_calls[0].kwargs.get('also_print') is False

    def test_release_drag_type_logged(self, debounce_module):
        """Long hold release should be logged as type=drag."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.200)  # 200ms > 150ms hold threshold

        with patch.object(debounce_module, 'log') as mock_log:
            mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))

        diag_calls = [c for c in mock_log.call_args_list
                      if 'CLICK_DIAG' in str(c) and 'RELEASE' in str(c)]
        assert len(diag_calls) == 1
        assert 'type=drag' in diag_calls[0].args[0]

    def test_flush_logs_click_diag(self, debounce_module):
        """Flushed pending drag release should log CLICK_DIAG FLUSH."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module, threshold_ms=50)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.2)  # 200ms hold = drag
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
        time.sleep(0.06)

        with patch.object(debounce_module, 'log') as mock_log:
            mouse.flush_pending()

        diag_calls = [c for c in mock_log.call_args_list
                      if 'CLICK_DIAG' in str(c) and 'FLUSH' in str(c)]
        assert len(diag_calls) == 1
        msg = diag_calls[0].args[0]
        assert 'LEFT FLUSH' in msg
        assert 'waited=' in msg
        assert 'type=drag' in msg
        assert diag_calls[0].kwargs.get('also_print') is False

    def test_suppressed_logs_click_diag(self, debounce_module):
        """Suppressed drag bounce should log CLICK_DIAG SUPPRESSED."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module, threshold_ms=60)

        # Long hold (drag)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.200)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))

        # Bounce re-press
        time.sleep(0.030)
        with patch.object(debounce_module, 'log') as mock_log:
            mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))

        diag_calls = [c for c in mock_log.call_args_list
                      if 'CLICK_DIAG' in str(c) and 'SUPPRESSED' in str(c)]
        assert len(diag_calls) == 1
        msg = diag_calls[0].args[0]
        assert 'LEFT PRESS SUPPRESSED' in msg
        assert 'pp_gap=' in msg
        assert diag_calls[0].kwargs.get('also_print') is False

    def test_no_click_diag_when_disabled(self, debounce_module):
        """With diagnose_clicks=False, no CLICK_DIAG should be logged."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module, diagnose_clicks=False)

        with patch.object(debounce_module, 'log') as mock_log:
            # Full click cycle: press, release, flush
            mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
            time.sleep(0.050)
            mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
            time.sleep(0.07)
            mouse.flush_pending()

        diag_calls = [c for c in mock_log.call_args_list
                      if 'CLICK_DIAG' in str(c)]
        assert len(diag_calls) == 0

    def test_last_press_forwarded_tracked(self, debounce_module):
        """last_press_forwarded should be updated on forwarded presses."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module)

        before = time.monotonic()
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        after = time.monotonic()

        assert ecodes.BTN_LEFT in mouse.last_press_forwarded
        assert before <= mouse.last_press_forwarded[ecodes.BTN_LEFT] <= after

    def test_rp_gap_shown_after_flush(self, debounce_module):
        """After a release is flushed, next press should show rp_gap."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module, threshold_ms=50)

        # Click + flush
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
        time.sleep(0.06)
        mouse.flush_pending()

        time.sleep(0.050)

        with patch.object(debounce_module, 'log') as mock_log:
            mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))

        diag_calls = [c for c in mock_log.call_args_list
                      if 'CLICK_DIAG' in str(c) and 'PRESS' in str(c)]
        assert len(diag_calls) == 1
        msg = diag_calls[0].args[0]
        assert 'rp_gap=' in msg
        assert 'rp_gap=--' not in msg  # Should have a real value


class TestQuietMode:
    """Tests for --quiet stats suppression.

    In quiet mode, STATS should only log when something notable happened
    (suppressions or lag spikes) since the last stats interval.
    """

    def _make_mouse(self, debounce_module, threshold_ms=60):
        from evdev import ecodes

        mock_device = MagicMock()
        mock_device.name = "Test Mouse"
        mock_device.fd = 99
        mock_uinput = MagicMock()

        with patch.object(debounce_module, 'UInput') as mock_uinput_class:
            mock_uinput_class.from_device.return_value = mock_uinput
            mouse = debounce_module.DelayedDebouncedMouse(
                mock_device, threshold_ms, quiet=True,
            )
        return mouse, mock_uinput

    def test_no_notable_events_on_fresh_instance(self, debounce_module):
        """Fresh instance should have no notable events."""
        mouse, _ = self._make_mouse(debounce_module)
        assert mouse.has_notable_events() is False

    def test_suppression_is_notable(self, debounce_module):
        """A drag bounce suppression should make has_notable_events() True."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module, threshold_ms=60)

        # Drag + bounce → suppression
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.2)  # drag
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))

        assert mouse.suppressed == 1
        assert mouse.has_notable_events() is True

    def test_record_stats_resets_notable(self, debounce_module):
        """After record_stats(), has_notable_events() should be False again."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module, threshold_ms=60)

        # Trigger a suppression
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.2)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))

        assert mouse.has_notable_events() is True
        mouse.record_stats()
        assert mouse.has_notable_events() is False

    def test_lag_spike_is_notable(self, debounce_module):
        """A lag spike should make has_notable_events() True."""
        mouse, _ = self._make_mouse(debounce_module)
        mouse.lag_spikes = 1  # Simulate a spike
        assert mouse.has_notable_events() is True

    def test_new_events_after_record_stats(self, debounce_module):
        """New notable events after record_stats() should trigger again."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module, threshold_ms=60)

        # First suppression
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.2)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        mouse.record_stats()
        assert mouse.has_notable_events() is False

        # Second suppression (need a new drag)
        time.sleep(0.2)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        assert mouse.suppressed == 2
        assert mouse.has_notable_events() is True

    def test_normal_clicks_not_notable(self, debounce_module):
        """Normal clicks without any issues should not be notable."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module)

        # Normal click cycle
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
        time.sleep(0.07)
        mouse.flush_pending()

        assert mouse.has_notable_events() is False
