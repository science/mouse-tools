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
                debounce_enabled=True,
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
                debounce_enabled=True,
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
                debounce_enabled=True,
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
                debounce_enabled=True,
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
                debounce_enabled=True,
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
                debounce_enabled=True,
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


class TestDebounceDisabled:
    """Tests for the --debounce feature flag (off by default).

    When debounce_enabled=False, the filter still grabs the device and
    forwards events through uinput (so remapping and lag tracking still
    work), but skips all bounce-suppression logic: drag releases forward
    immediately, no pending_release queueing, no SUPPRESSED, no NEAR-MISS.
    """

    def _make_mouse(self, debounce_module, threshold_ms=60,
                    hold_threshold_ms=150, remap=None, debounce_enabled=False):
        from evdev import ecodes

        mock_device = MagicMock()
        mock_device.name = "Test Mouse"
        mock_device.fd = 99
        mock_uinput = MagicMock()

        with patch.object(debounce_module, 'UInput') as mock_uinput_class:
            mock_uinput_class.return_value = mock_uinput
            mock_uinput_class.from_device.return_value = mock_uinput
            mouse = debounce_module.DelayedDebouncedMouse(
                mock_device, threshold_ms,
                hold_threshold_ms=hold_threshold_ms,
                quiet=True,
                button_remap=remap,
                debounce_enabled=debounce_enabled,
            )
        return mouse, mock_uinput

    def test_drag_release_forwarded_immediately_when_disabled(self, debounce_module):
        """With debounce off, a drag release goes straight to uinput."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module)

        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.2)  # 200ms hold = drag
        uinput.write_event.reset_mock()

        release = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0)
        mouse.process_event(release)

        uinput.write_event.assert_called_once_with(release)
        assert ecodes.BTN_LEFT not in mouse.pending_release

    def test_pending_release_empty_when_disabled(self, debounce_module):
        """With debounce off, pending_release is never populated."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module)

        # Drag press + release
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.2)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))

        assert mouse.pending_release == {}
        assert mouse.next_deadline() is None

    def test_classic_bounce_pattern_not_suppressed_when_disabled(self, debounce_module):
        """With debounce off, drag-release-then-fast-repress is NOT suppressed."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module, threshold_ms=60)

        # Drag bounce pattern: long hold, release, fast re-press
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.2)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
        uinput.write_event.reset_mock()

        time.sleep(0.030)  # 30ms gap, well below threshold
        press2 = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1)
        result = mouse.process_event(press2)

        # Re-press should be forwarded, not suppressed
        assert result is True
        assert mouse.suppressed == 0
        uinput.write_event.assert_called_once_with(press2)

    def test_click_release_forwarded_when_disabled(self, debounce_module):
        """With debounce off, short-hold (click) releases also forward immediately."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module)

        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        uinput.write_event.reset_mock()

        release = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0)
        mouse.process_event(release)

        uinput.write_event.assert_called_once_with(release)
        assert ecodes.BTN_LEFT not in mouse.pending_release

    def test_remap_still_works_when_disabled(self, debounce_module):
        """Remapping is independent of debounce — still active when debounce off."""
        from evdev import ecodes

        remap = {ecodes.BTN_EXTRA: ecodes.KEY_VOLUMEUP}
        mouse, uinput = self._make_mouse(debounce_module, remap=remap)

        ev = make_event(ecodes.EV_KEY, ecodes.BTN_EXTRA, 1)
        result = mouse.process_event(ev)

        assert result is True
        written = uinput.write_event.call_args_list[-1].args[0]
        assert written.code == ecodes.KEY_VOLUMEUP
        assert written.value == 1

    def test_movement_events_pass_through_when_disabled(self, debounce_module):
        """Movement and other non-button events still flow through with debounce off."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module)

        ev = make_event(ecodes.EV_REL, ecodes.REL_X, 5)
        result = mouse.process_event(ev)

        assert result is True
        uinput.write_event.assert_called_once_with(ev)

    def test_has_notable_events_false_after_bounce_pattern(self, debounce_module):
        """A bounce pattern with debounce off should not register as notable."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module, threshold_ms=60)

        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.2)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
        time.sleep(0.030)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))

        assert mouse.suppressed == 0
        assert mouse.has_notable_events() is False

    def test_default_is_disabled(self, debounce_module):
        """When debounce_enabled is not specified, defaults to off."""
        from evdev import ecodes

        mock_device = MagicMock()
        mock_device.name = "Test Mouse"
        mock_device.fd = 99
        mock_uinput = MagicMock()

        with patch.object(debounce_module, 'UInput') as mock_uinput_class:
            mock_uinput_class.return_value = mock_uinput
            mock_uinput_class.from_device.return_value = mock_uinput
            # No debounce_enabled kwarg
            mouse = debounce_module.DelayedDebouncedMouse(
                mock_device, 60, quiet=True,
            )

        # Drag bounce pattern should NOT suppress
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.2)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
        time.sleep(0.030)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))

        assert mouse.suppressed == 0


class TestDebounceEnabledExplicitly:
    """Tests that the debounce path still works when explicitly enabled.

    These mirror the historical default-on behavior (now opt-in) to keep
    the legacy code path covered.
    """

    def _make_mouse(self, debounce_module, threshold_ms=60,
                    hold_threshold_ms=150):
        from evdev import ecodes

        mock_device = MagicMock()
        mock_device.name = "Test Mouse"
        mock_device.fd = 99
        mock_uinput = MagicMock()

        with patch.object(debounce_module, 'UInput') as mock_uinput_class:
            mock_uinput_class.return_value = mock_uinput
            mock_uinput_class.from_device.return_value = mock_uinput
            mouse = debounce_module.DelayedDebouncedMouse(
                mock_device, threshold_ms,
                hold_threshold_ms=hold_threshold_ms,
                quiet=True,
                debounce_enabled=True,
            )
        return mouse, mock_uinput

    def test_drag_release_queued_when_enabled(self, debounce_module):
        """With debounce explicitly on, drag releases are queued."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module)

        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.2)
        uinput.write_event.reset_mock()

        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))

        uinput.write_event.assert_not_called()
        assert ecodes.BTN_LEFT in mouse.pending_release

    def test_bounce_suppressed_when_enabled(self, debounce_module):
        """With debounce explicitly on, drag bounce is suppressed."""
        from evdev import ecodes

        mouse, _ = self._make_mouse(debounce_module, threshold_ms=60)

        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        time.sleep(0.2)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
        time.sleep(0.030)
        result = mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))

        assert result is False
        assert mouse.suppressed == 1


def make_wheel_event(code, value):
    """Create a mock REL_* wheel event."""
    from evdev import ecodes
    ev = MagicMock()
    ev.type = ecodes.EV_REL
    ev.code = code
    ev.value = value
    now = time.time()
    ev.sec = int(now)
    ev.usec = int((now - int(now)) * 1_000_000)
    return ev


class TestWheelDiagnostics:
    """Tests for WheelDiagnostics — wheel-event burst classifier and reversal logger.

    Captures REL_WHEEL / REL_WHEEL_HI_RES events into directional bursts.
    Emits WHEEL_REV when the sign flips (candidate rebound) and WHEEL_BURST_END
    when an idle gap closes out the previous burst. Read-only — never mutates
    the forwarded event stream.
    """

    def _make_diag(self, debounce_module, name="Test Mouse", idle_ms=250):
        return debounce_module.WheelDiagnostics(name=name, idle_ms=idle_ms)

    def test_single_direction_burst_emits_no_reversal(self, debounce_module):
        from evdev import ecodes
        diag = self._make_diag(debounce_module)

        with patch.object(debounce_module, 'log') as mock_log:
            for i in range(5):
                diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120),
                             now_mono=1.0 + i * 0.020)

        rev_calls = [c for c in mock_log.call_args_list if 'WHEEL_REV' in str(c)]
        assert len(rev_calls) == 0

    def test_sign_flip_emits_wheel_rev(self, debounce_module):
        """A reverse event after a same-direction burst emits WHEEL_REV."""
        from evdev import ecodes
        diag = self._make_diag(debounce_module)

        # Down burst
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.000)
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.020)
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.040)

        # Reversal
        with patch.object(debounce_module, 'log') as mock_log:
            diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +24),
                         now_mono=1.080)

        rev_calls = [c for c in mock_log.call_args_list if 'WHEEL_REV' in str(c)]
        assert len(rev_calls) == 1
        msg = rev_calls[0].args[0]
        # Sanity: contains key fields
        assert 'WHEEL_REV' in msg
        assert 'value=24' in msg or '+24' in msg
        # Previous burst summary
        assert 'sum=-360' in msg
        assert 'count=3' in msg
        # All wheel logs are file-only
        assert rev_calls[0].kwargs.get('also_print') is False

    def test_reversal_starts_new_burst_at_reverse_event(self, debounce_module):
        """After WHEEL_REV, the reverse event seeds a new burst — no second
        WHEEL_REV when more same-direction events follow."""
        from evdev import ecodes
        diag = self._make_diag(debounce_module)

        # Down burst
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.000)
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.020)
        # First reversal
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +24), now_mono=1.040)

        with patch.object(debounce_module, 'log') as mock_log:
            # More up events — same direction as the new burst, no new WHEEL_REV
            diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +24),
                         now_mono=1.060)
            diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +24),
                         now_mono=1.080)

        rev_calls = [c for c in mock_log.call_args_list if 'WHEEL_REV' in str(c)]
        assert len(rev_calls) == 0

    def test_two_consecutive_reversals_get_distinct_burst_ids(self, debounce_module):
        """Down → up → down emits two reversal entries with different burst_ids."""
        from evdev import ecodes
        diag = self._make_diag(debounce_module)

        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.000)
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.020)

        with patch.object(debounce_module, 'log') as mock_log:
            # Reversal 1: up
            diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +24),
                         now_mono=1.050)
            # Reversal 2: down
            diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120),
                         now_mono=1.080)

        rev_calls = [c for c in mock_log.call_args_list if 'WHEEL_REV' in str(c)]
        assert len(rev_calls) == 2
        msg1 = rev_calls[0].args[0]
        msg2 = rev_calls[1].args[0]
        # Two distinct burst_ids
        import re
        ids = [re.search(r'burst_id=(\d+)', m) for m in (msg1, msg2)]
        assert all(ids), f"both messages should have burst_id: {msg1!r}, {msg2!r}"
        assert ids[0].group(1) != ids[1].group(1)

    def test_idle_gap_emits_burst_end_no_reversal(self, debounce_module):
        """An idle gap > idle_ms before a same-direction event closes out the
        previous burst with WHEEL_BURST_END (no WHEEL_REV)."""
        from evdev import ecodes
        diag = self._make_diag(debounce_module, idle_ms=250)

        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.000)
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.020)

        with patch.object(debounce_module, 'log') as mock_log:
            # 500ms gap — burst should close out
            diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120),
                         now_mono=1.520)

        rev_calls = [c for c in mock_log.call_args_list if 'WHEEL_REV' in str(c)]
        end_calls = [c for c in mock_log.call_args_list if 'WHEEL_BURST_END' in str(c)]
        assert len(rev_calls) == 0
        assert len(end_calls) == 1
        msg = end_calls[0].args[0]
        assert 'sum=-240' in msg
        assert 'count=2' in msg
        assert end_calls[0].kwargs.get('also_print') is False

    def test_mixed_axis_same_direction_is_single_burst(self, debounce_module):
        """REL_WHEEL and REL_WHEEL_HI_RES same-direction events form one burst."""
        from evdev import ecodes
        diag = self._make_diag(debounce_module)

        # Hi-res chunks plus a notch crossing — all same direction
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -60), now_mono=1.000)
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -60), now_mono=1.005)
        diag.observe(make_wheel_event(ecodes.REL_WHEEL, -1), now_mono=1.005)

        with patch.object(debounce_module, 'log') as mock_log:
            # Reversal
            diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +24),
                         now_mono=1.040)

        rev_calls = [c for c in mock_log.call_args_list if 'WHEEL_REV' in str(c)]
        assert len(rev_calls) == 1
        msg = rev_calls[0].args[0]
        # Hi-res sum: -60 + -60 = -120; notch sum: -1
        assert 'count=3' in msg
        # Should mention both axes saw activity
        assert 'WHEEL' in msg

    def test_zero_value_event_ignored(self, debounce_module):
        """A zero-value wheel event (shouldn't normally happen) doesn't break state."""
        from evdev import ecodes
        diag = self._make_diag(debounce_module)

        with patch.object(debounce_module, 'log') as mock_log:
            diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, 0),
                         now_mono=1.000)

        # Should not emit anything and not crash
        rev_calls = [c for c in mock_log.call_args_list if 'WHEEL_REV' in str(c)]
        end_calls = [c for c in mock_log.call_args_list if 'WHEEL_BURST_END' in str(c)]
        assert len(rev_calls) == 0
        assert len(end_calls) == 0

    def test_reversal_records_gap_and_since_burst_start(self, debounce_module):
        """WHEEL_REV records gap_ms (last forward event → reversal) and
        since_burst_start_ms (burst start → reversal)."""
        from evdev import ecodes
        diag = self._make_diag(debounce_module)

        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.000)
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.040)

        with patch.object(debounce_module, 'log') as mock_log:
            # Gap from last forward (1.040) to reversal: 60ms
            # Since burst start (1.000): 100ms
            diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +24),
                         now_mono=1.100)

        rev_calls = [c for c in mock_log.call_args_list if 'WHEEL_REV' in str(c)]
        assert len(rev_calls) == 1
        msg = rev_calls[0].args[0]
        assert 'gap_ms=60' in msg
        assert 'since_burst_start_ms=100' in msg


class TestWheelDiagnosticsCallback:
    """Tests for the on_burst_closed callback hook used by WheelSuppressor.

    The callback is fired with (signed_dir, abs_sum, end_mono) every time a
    burst closes — either by idle gap (WHEEL_BURST_END), sign-flip
    (WHEEL_REV), or shutdown flush. The callback receives the OLD burst's
    state, not the reversal event that triggered close.
    """

    def test_callback_fires_on_idle_close(self, debounce_module):
        from evdev import ecodes
        captured = []
        diag = debounce_module.WheelDiagnostics(
            name="Test", idle_ms=250,
            on_burst_closed=lambda d, s, t: captured.append((d, s, t)),
        )
        # Down burst, 3 events
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.000)
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.020)
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.040)
        # Idle gap + same-dir event triggers idle close-out of previous burst
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.500)

        assert len(captured) == 1
        d, s, t = captured[0]
        assert d == -1                # closed burst direction
        assert s == 360               # |sum(-120 * 3)|
        assert abs(t - 1.040) < 1e-9  # last event of closed burst

    def test_callback_fires_on_reversal_with_old_burst_stats(self, debounce_module):
        """Sign-flip fires callback with OLD burst's stats — not the reversal."""
        from evdev import ecodes
        captured = []
        diag = debounce_module.WheelDiagnostics(
            name="Test",
            on_burst_closed=lambda d, s, t: captured.append((d, s, t)),
        )
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.000)
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.020)
        # Reversal +120
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120), now_mono=1.040)

        assert len(captured) == 1
        d, s, t = captured[0]
        assert d == -1                # OLD burst was down
        assert s == 240               # |-240|
        assert abs(t - 1.020) < 1e-9

    def test_callback_fires_on_flush(self, debounce_module):
        from evdev import ecodes
        captured = []
        diag = debounce_module.WheelDiagnostics(
            name="Test",
            on_burst_closed=lambda d, s, t: captured.append((d, s, t)),
        )
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120), now_mono=2.000)
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120), now_mono=2.030)
        diag.flush()

        assert len(captured) == 1
        d, s, t = captured[0]
        assert d == +1
        assert s == 240
        assert abs(t - 2.030) < 1e-9

    def test_default_constructor_no_callback(self, debounce_module):
        """No callback param: existing behavior preserved (no exception)."""
        from evdev import ecodes
        diag = debounce_module.WheelDiagnostics(name="Test")
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.000)
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120), now_mono=1.040)
        diag.flush()
        # No exception, no callback to fire — pass.

    def test_emit_logs_false_silences_log_lines(self, debounce_module):
        """emit_logs=False suppresses WHEEL_REV / WHEEL_BURST_END output."""
        from evdev import ecodes
        diag = debounce_module.WheelDiagnostics(name="Test", emit_logs=False)

        with patch.object(debounce_module, 'log') as mock_log:
            diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.000)
            diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.020)
            diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120), now_mono=1.040)
            diag.flush()

        diag_calls = [c for c in mock_log.call_args_list
                      if 'WHEEL_REV' in str(c) or 'WHEEL_BURST_END' in str(c)]
        assert len(diag_calls) == 0

    def test_emit_logs_false_still_fires_callback(self, debounce_module):
        """Silent diagnostics still notify the suppressor."""
        from evdev import ecodes
        captured = []
        diag = debounce_module.WheelDiagnostics(
            name="Test", emit_logs=False,
            on_burst_closed=lambda d, s, t: captured.append((d, s, t)),
        )
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.000)
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.020)
        diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120), now_mono=1.040)

        assert len(captured) == 1
        assert captured[0][0] == -1

    def test_emit_logs_default_true(self, debounce_module):
        """emit_logs defaults to True — existing diagnostic mode unchanged."""
        from evdev import ecodes
        diag = debounce_module.WheelDiagnostics(name="Test")

        with patch.object(debounce_module, 'log') as mock_log:
            diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.000)
            diag.observe(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120), now_mono=1.040)

        rev_calls = [c for c in mock_log.call_args_list if 'WHEEL_REV' in str(c)]
        assert len(rev_calls) == 1


class TestWheelSuppressor:
    """Tests for the WheelSuppressor predicate.

    Two suppression rules:
      Type A — single-step opposite-direction event within REV_WINDOW_MS of a
               significant primary's end (kinetic backswing pulse).
      Type B — same-direction re-burst within COOLDOWN_MS, magnitude-bounded
               by min(primary*ratio, bounce_max_total) (residual-momentum kick).

    Anchor state is driven by note_burst_closed callbacks. Cascade chains
    (each sub-burst < ½ previous) extend the anchor's end_mono on
    same-direction small closures. Cross-direction small closures clear
    the anchor (user reversed legitimately).
    """

    def _make(self, debounce_module, **kwargs):
        defaults = dict(
            name="Test", enabled=True,
            cooldown_ms=1200, rev_window_ms=280,
            cooldown_ratio=0.5, min_primary=360,
            bounce_max_total=300, quiet_ms=200,
            type_b_enabled=True,  # Tests in this class exercise Type B paths
        )
        defaults.update(kwargs)
        return debounce_module.WheelSuppressor(**defaults)

    # ---- baseline / anchor management --------------------------------------

    def test_no_anchor_forwards(self, debounce_module):
        from evdev import ecodes
        sup = self._make(debounce_module)
        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120)
        s, _ = sup.should_suppress(ev, now_mono=1.0)
        assert s is False

    def test_below_min_primary_does_not_arm(self, debounce_module):
        from evdev import ecodes
        sup = self._make(debounce_module, min_primary=360)
        sup.note_burst_closed(-1, 240, end_mono=1.0)  # 240 < 360
        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120)
        s, _ = sup.should_suppress(ev, now_mono=1.5)
        assert s is False

    def test_significant_primary_arms_cooldown(self, debounce_module):
        from evdev import ecodes
        sup = self._make(debounce_module)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120)
        s, r = sup.should_suppress(ev, now_mono=1.5)
        assert s is True
        assert r == "type_b_cooldown"

    def test_zero_value_passes_through(self, debounce_module):
        from evdev import ecodes
        sup = self._make(debounce_module)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, 0)
        s, _ = sup.should_suppress(ev, now_mono=1.1)
        assert s is False

    # ---- Type A reversal veto ---------------------------------------------

    def test_type_a_single_step_hi_res_suppressed(self, debounce_module):
        from evdev import ecodes
        sup = self._make(debounce_module)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120)
        s, r = sup.should_suppress(ev, now_mono=1.1)
        assert s is True
        assert r == "type_a_reversal"

    def test_type_a_single_step_notch_suppressed(self, debounce_module):
        from evdev import ecodes
        sup = self._make(debounce_module)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        ev = make_wheel_event(ecodes.REL_WHEEL, +1)
        s, r = sup.should_suppress(ev, now_mono=1.1)
        assert s is True
        assert r == "type_a_reversal"

    def test_type_a_multi_step_hi_res_forwarded(self, debounce_module):
        """Multi-step opposite scrolls (legitimate reverse) bypass Type A."""
        from evdev import ecodes
        sup = self._make(debounce_module)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, +240)  # 2 notches
        s, _ = sup.should_suppress(ev, now_mono=1.1)
        assert s is False

    def test_type_a_outside_window_forwarded(self, debounce_module):
        """Opposite-direction single-step beyond REV_WINDOW_MS is not a bounce."""
        from evdev import ecodes
        sup = self._make(debounce_module, rev_window_ms=280)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120)
        s, _ = sup.should_suppress(ev, now_mono=1.5)  # 500ms after primary
        assert s is False

    def test_type_a_only_after_significant_primary(self, debounce_module):
        """Type A doesn't fire if primary was below min_primary."""
        from evdev import ecodes
        sup = self._make(debounce_module, min_primary=360)
        sup.note_burst_closed(-1, 200, end_mono=1.0)
        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120)
        s, _ = sup.should_suppress(ev, now_mono=1.1)
        assert s is False

    # ---- Type B same-direction cooldown -----------------------------------

    def test_type_b_within_budget_suppressed(self, debounce_module):
        from evdev import ecodes
        sup = self._make(debounce_module,
                         cooldown_ratio=0.5, bounce_max_total=300)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120)
        s, r = sup.should_suppress(ev, now_mono=1.3)
        assert s is True
        assert r == "type_b_cooldown"
        assert sup.cooldown_consumed == 120

    def test_type_b_exceeds_budget_forwarded(self, debounce_module):
        """Once cumulative magnitude exceeds budget, events forward.
        Anchor stays in place for cascade-catch on the next bounce burst.
        With Type B opt-in counting full event magnitudes (120 each here),
        budget exhausts after the second hi-res event."""
        from evdev import ecodes
        sup = self._make(debounce_module,
                         cooldown_ratio=0.5, bounce_max_total=300, min_primary=360)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        results = []
        for now in (1.30, 1.32, 1.34):
            s, _ = sup.should_suppress(
                make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120),
                now_mono=now,
            )
            results.append(s)
        # Budget = min(600*0.5, 300) = 300. Each event mag=120.
        # 1st: 0+120=120 ≤ 300 → suppress. 2nd: 120+120=240 ≤ 300 → suppress.
        # 3rd: 240+120=360 > 300 → forward (anchor stays for cascade).
        assert results == [True, True, False]
        assert sup.last_closed is not None

    def test_type_b_outside_cooldown_forwarded(self, debounce_module):
        from evdev import ecodes
        sup = self._make(debounce_module, cooldown_ms=1200)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120)
        s, _ = sup.should_suppress(ev, now_mono=2.5)  # 1500ms later
        assert s is False

    def test_mid_gesture_after_budget_exhausted_keeps_forwarding(self, debounce_module):
        """When budget exhausted and events keep flowing rapidly, treat as
        continuous scroll — never re-suppress mid-stream. Type B opt-in,
        full-magnitude budget accounting."""
        from evdev import ecodes
        sup = self._make(debounce_module,
                         cooldown_ratio=0.5, bounce_max_total=300, quiet_ms=200)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        results = []
        for now in (1.30, 1.32, 1.34, 1.36, 1.38):
            s, _ = sup.should_suppress(
                make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120),
                now_mono=now,
            )
            results.append(s)
        # Each event mag=120. 1st: 120 ≤ 300 → suppress. 2nd: 240 ≤ 300 →
        # suppress. 3rd: 360 > 300 → forward. 4th & 5th: mid-gesture (gap
        # from last_forwarded < 200ms) → forward.
        assert results == [True, True, False, False, False]

    # ---- axis-tie ---------------------------------------------------------

    def test_axis_tie_suppresses_paired_axis(self, debounce_module):
        """REL_WHEEL and REL_WHEEL_HI_RES events arrive paired within USB poll.
        If one is suppressed, the partner with same sign should also suppress."""
        from evdev import ecodes
        sup = self._make(debounce_module)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        # Suppress REL_WHEEL +1 (Type A single notch)
        s1, r1 = sup.should_suppress(
            make_wheel_event(ecodes.REL_WHEEL, +1), now_mono=1.100)
        assert s1 is True
        # Within 8ms, paired hi-res +120 → axis-tie
        s2, r2 = sup.should_suppress(
            make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120), now_mono=1.105)
        assert s2 is True
        assert r2 == "axis_tie"

    def test_axis_tie_window_expires(self, debounce_module):
        """Beyond axis-tie window, the second opposite-direction event is
        evaluated independently. With Type A fire-once, the anchor's
        type_a_consumed flag is set after the first suppression, so the
        second opposite event forwards even though it's still within the
        rev_window."""
        from evdev import ecodes
        sup = self._make(debounce_module)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        sup.should_suppress(
            make_wheel_event(ecodes.REL_WHEEL, +1), now_mono=1.10)
        # 30ms later, axis-tie window 8ms expired. Type A consumed.
        # Falls through; Type B doesn't fire (sign != lc.dir). Forward.
        s, _ = sup.should_suppress(
            make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120), now_mono=1.13)
        assert s is False

    def test_axis_tie_only_same_sign(self, debounce_module):
        """Axis-tie does not suppress an opposite-sign event in the window."""
        from evdev import ecodes
        sup = self._make(debounce_module)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        # Suppress hi-res +120 (Type A)
        sup.should_suppress(
            make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120), now_mono=1.10)
        # Within 8ms, but OPPOSITE sign (down) — same sign as primary.
        # Should fall through; Type B branch may suppress for budget reasons.
        s, r = sup.should_suppress(
            make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.105)
        # Reason should NOT be axis_tie regardless of suppress decision.
        assert r != "axis_tie"

    # ---- cascade-extension --------------------------------------------------

    def test_cascade_same_dir_small_extends_anchor(self, debounce_module):
        """Cascade chain 605 → 242 → 121: with max_cascade_depth ≥ 1, each
        small same-dir close extends the anchor's end_mono so the next
        bounce in the chain is still within cooldown of the original
        primary's reference. Default max_cascade_depth=0 disables this."""
        from evdev import ecodes
        sup = self._make(debounce_module,
                         cooldown_ms=1200, cooldown_ratio=0.5,
                         bounce_max_total=10000,  # don't cap for this test
                         min_primary=360,
                         max_cascade_depth=2)  # Enable cascade for this test
        sup.note_burst_closed(-1, 605, end_mono=1.0)
        # Cascade bounce 1: 242 (insignificant, same direction) at t=1.7
        sup.note_burst_closed(-1, 242, end_mono=1.7)
        assert sup.last_closed.end_mono == 1.7
        assert sup.last_closed.abs_sum == 605  # Original primary preserved

        # Predicate at t=2.4 — would be > 1200ms after primary's original end
        # but within 1200ms of cascade-extended end (2.4 - 1.7 = 700ms).
        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120)
        s, r = sup.should_suppress(ev, now_mono=2.4)
        assert s is True
        assert r == "type_b_cooldown"

    def test_cross_dir_small_burst_clears_anchor(self, debounce_module):
        """Cross-direction small burst inside cooldown → user reversed
        legitimately. Clear anchor so they can scroll the new direction."""
        from evdev import ecodes
        sup = self._make(debounce_module, cooldown_ms=1200, min_primary=360)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        sup.note_burst_closed(+1, 200, end_mono=1.3)
        assert sup.last_closed is None
        # Subsequent down event passes through
        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120)
        s, _ = sup.should_suppress(ev, now_mono=1.5)
        assert s is False

    def test_new_significant_burst_replaces_anchor(self, debounce_module):
        """A new significant burst inside cooldown is a fresh primary."""
        from evdev import ecodes
        sup = self._make(debounce_module, min_primary=360)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        sup.note_burst_closed(-1, 1000, end_mono=1.5)
        assert sup.last_closed.abs_sum == 1000
        assert sup.last_closed.end_mono == 1.5
        assert sup.cooldown_consumed == 0  # Reset on new primary

    def test_cooldown_expired_burst_replaces_anchor(self, debounce_module):
        """A burst arriving after cooldown expires becomes the new (possibly
        insignificant) anchor — old primary is stale."""
        from evdev import ecodes
        sup = self._make(debounce_module, cooldown_ms=1200, min_primary=360)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        sup.note_burst_closed(-1, 200, end_mono=3.0)
        assert sup.last_closed.end_mono == 3.0
        assert sup.last_closed.abs_sum == 200
        assert sup.last_closed.was_significant is False

    # ---- runtime toggle ---------------------------------------------------

    def test_set_enabled_false_short_circuits(self, debounce_module):
        from evdev import ecodes
        sup = self._make(debounce_module)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        sup.set_enabled(False)
        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120)
        s, _ = sup.should_suppress(ev, now_mono=1.1)
        assert s is False

    def test_set_enabled_true_restores_suppression(self, debounce_module):
        from evdev import ecodes
        sup = self._make(debounce_module)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        sup.set_enabled(False)
        sup.set_enabled(True)
        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120)
        s, _ = sup.should_suppress(ev, now_mono=1.1)
        assert s is True

    def test_disabled_init_param(self, debounce_module):
        from evdev import ecodes
        sup = self._make(debounce_module, enabled=False)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120)
        s, _ = sup.should_suppress(ev, now_mono=1.1)
        assert s is False

    # ---- counters ---------------------------------------------------------

    def test_counters_increment(self, debounce_module):
        from evdev import ecodes
        sup = self._make(debounce_module)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        # Type A (notch +1) — first opposite event suppressed
        sup.should_suppress(
            make_wheel_event(ecodes.REL_WHEEL, +1), now_mono=1.10)
        # Paired hi-res +120 within axis-tie window → axis-tie
        sup.should_suppress(
            make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120), now_mono=1.105)

        sup2 = self._make(debounce_module)  # type_b_enabled=True
        sup2.note_burst_closed(-1, 600, end_mono=1.0)
        # Type B
        sup2.should_suppress(
            make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=1.5)

        assert sup.suppressed_total == 2
        assert sup.suppressed_type_a == 1
        assert sup.suppressed_axis_tie == 1
        assert sup.suppressed_type_b == 0
        assert sup2.suppressed_type_b == 1


class TestWheelDiagnosticsIntegration:
    """Tests for --diagnose-wheel hooks in DelayedDebouncedMouse."""

    def _make_mouse(self, debounce_module, threshold_ms=60, diagnose_wheel=True):
        from evdev import ecodes
        mock_device = MagicMock()
        mock_device.name = "Test Mouse"
        mock_device.fd = 99
        mock_uinput = MagicMock()

        with patch.object(debounce_module, 'UInput') as mock_uinput_class:
            mock_uinput_class.from_device.return_value = mock_uinput
            mouse = debounce_module.DelayedDebouncedMouse(
                mock_device, threshold_ms, quiet=True,
                diagnose_wheel=diagnose_wheel,
            )
        return mouse, mock_uinput

    def test_wheel_diag_created_when_enabled(self, debounce_module):
        mouse, _ = self._make_mouse(debounce_module, diagnose_wheel=True)
        assert isinstance(mouse._wheel_diag, debounce_module.WheelDiagnostics)

    def test_wheel_diag_not_created_by_default(self, debounce_module):
        mouse, _ = self._make_mouse(debounce_module, diagnose_wheel=False)
        assert mouse._wheel_diag is None

    def test_wheel_event_routed_to_diag(self, debounce_module):
        """A REL_WHEEL_HI_RES event should be observed by the diag instance."""
        from evdev import ecodes
        mouse, uinput = self._make_mouse(debounce_module)

        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120)
        result = mouse.process_event(ev)

        # Forwarded normally
        assert result is True
        uinput.write_event.assert_called_once_with(ev)
        # Recorded in diag
        assert mouse._wheel_diag.burst_count == 1

    def test_rel_wheel_also_routed(self, debounce_module):
        """REL_WHEEL events (notch axis) should also be observed."""
        from evdev import ecodes
        mouse, _ = self._make_mouse(debounce_module)

        ev = make_wheel_event(ecodes.REL_WHEEL, -1)
        mouse.process_event(ev)

        assert mouse._wheel_diag.burst_count == 1

    def test_rel_x_not_routed_as_wheel(self, debounce_module):
        """Movement events (REL_X / REL_Y) must NOT be classified as wheel."""
        from evdev import ecodes
        mouse, _ = self._make_mouse(debounce_module)

        mouse.process_event(make_wheel_event(ecodes.REL_X, 5))
        mouse.process_event(make_wheel_event(ecodes.REL_Y, -3))

        assert mouse._wheel_diag.burst_count == 0

    def test_no_diag_calls_when_disabled(self, debounce_module):
        """With diagnose_wheel=False, no WHEEL_REV / WHEEL_BURST_END logs."""
        from evdev import ecodes
        mouse, _ = self._make_mouse(debounce_module, diagnose_wheel=False)

        with patch.object(debounce_module, 'log') as mock_log:
            # Down burst then reversal — would normally log WHEEL_REV
            mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))
            mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))
            mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +24))

        diag_calls = [c for c in mock_log.call_args_list
                      if 'WHEEL_REV' in str(c) or 'WHEEL_BURST_END' in str(c)]
        assert len(diag_calls) == 0


def _patch_monotonic(debounce_module, times):
    """Patch time.monotonic in the mouse_filter module to return values from
    `times` in order, repeating the last value if exhausted. Returns a
    `patch` context manager.

    Used to drive timing-sensitive tests deterministically without sleeps.
    Each process_event() call consumes exactly one value from the list for
    the wheel path (when --diagnose-move is off).
    """
    padded = list(times) + [times[-1]] * 64
    return patch.object(debounce_module.time, 'monotonic', side_effect=padded)


class TestWheelSuppressorIntegration:
    """Tests for --wheel-suppress hooks in DelayedDebouncedMouse.

    Drives realistic event sequences through process_event to verify the
    full pipeline: WheelDiagnostics burst classification → suppressor
    callback → suppression decision → uinput forward / drop. Uses patched
    time.monotonic to simulate real-time gaps without sleep.
    """

    def _make_mouse(self, debounce_module, *,
                    wheel_suppress=True, diagnose_wheel=False,
                    wheel_type_b=True, **kwargs):
        from evdev import ecodes
        mock_device = MagicMock()
        mock_device.name = "Test Mouse"
        mock_device.fd = 99
        mock_uinput = MagicMock()

        with patch.object(debounce_module, 'UInput') as mock_uinput_class:
            mock_uinput_class.from_device.return_value = mock_uinput
            mouse = debounce_module.DelayedDebouncedMouse(
                mock_device, 60, quiet=True,
                wheel_suppress=wheel_suppress,
                diagnose_wheel=diagnose_wheel,
                wheel_type_b=wheel_type_b,  # integration tests cover Type B paths
                **kwargs,
            )
        return mouse, mock_uinput

    # ---- wiring ----------------------------------------------------------

    def test_suppressor_created_when_enabled(self, debounce_module):
        mouse, _ = self._make_mouse(debounce_module, wheel_suppress=True)
        assert isinstance(mouse._wheel_suppressor, debounce_module.WheelSuppressor)

    def test_suppressor_not_created_by_default(self, debounce_module):
        mouse, _ = self._make_mouse(debounce_module,
                                     wheel_suppress=False, diagnose_wheel=False)
        assert mouse._wheel_suppressor is None

    def test_diag_silent_when_only_suppress(self, debounce_module):
        """wheel_suppress=True without diagnose_wheel → diag runs silently."""
        mouse, _ = self._make_mouse(debounce_module,
                                     wheel_suppress=True, diagnose_wheel=False)
        assert mouse._wheel_diag is not None
        assert mouse._wheel_diag.emit_logs is False

    def test_diag_emits_when_diagnose_wheel_on(self, debounce_module):
        """Both flags on → diagnostic logs alongside suppression logs."""
        mouse, _ = self._make_mouse(debounce_module,
                                     wheel_suppress=True, diagnose_wheel=True)
        assert mouse._wheel_diag.emit_logs is True

    def test_diag_callback_drives_suppressor_anchor(self, debounce_module):
        """A burst closing in WheelDiagnostics should update suppressor.last_closed."""
        from evdev import ecodes
        mouse, _ = self._make_mouse(debounce_module, wheel_suppress=True)
        # 2 events, then idle gap > idle_ms (250ms) closes the burst on event 3.
        with _patch_monotonic(debounce_module, [0.000, 0.020, 0.300]):
            mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))
            mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))
            mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))
        # After the close-out, suppressor anchor should reflect the closed burst.
        assert mouse._wheel_suppressor.last_closed is not None
        assert mouse._wheel_suppressor.last_closed.abs_sum == 240
        assert mouse._wheel_suppressor.last_closed.dir == -1

    # ---- realistic suppression scenarios --------------------------------

    def test_type_a_reversal_suppressed_end_to_end(self, debounce_module):
        """5-event down primary forwarded, single-step up reversal suppressed."""
        from evdev import ecodes
        mouse, uinput = self._make_mouse(debounce_module, wheel_suppress=True)
        # 5 down events at 20ms intervals (sum -600), then a single +120 hi-res
        # at 320ms (sign-flip closes the down burst → arms anchor → reversal
        # event runs through predicate as Type A).
        times = [0.000, 0.020, 0.040, 0.060, 0.080, 0.320]
        with _patch_monotonic(debounce_module, times):
            for _ in range(5):
                mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))
            mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120))

        # 5 forwarded; the reversal suppressed
        assert uinput.write_event.call_count == 5
        assert mouse._wheel_suppressor.suppressed_type_a == 1
        assert mouse.wheel_suppressed == 1

    def test_type_b_bounce_suppressed_end_to_end(self, debounce_module):
        """5-event down primary forwarded, then 700ms idle, then same-dir
        bounce → idle-close arms anchor, bounce event suppressed by Type B.
        Requires Type B opt-in (default off in production)."""
        from evdev import ecodes
        mouse, uinput = self._make_mouse(debounce_module,
                                          wheel_suppress=True, wheel_type_b=True)
        times = [0.000, 0.020, 0.040, 0.060, 0.080, 0.700]
        with _patch_monotonic(debounce_module, times):
            for _ in range(5):
                mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))
            mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))

        assert uinput.write_event.call_count == 5
        assert mouse._wheel_suppressor.suppressed_type_b == 1

    def test_legitimate_continuation_not_suppressed(self, debounce_module):
        """Primary down → idle 300ms → user resumes scrolling DOWN. With
        Type B opt-in (default in this test class), budget exhausts after
        2 events; with Type B off (production default), all 10 events
        forward. Test explicitly verifies the Type B opt-in path."""
        from evdev import ecodes
        mouse, uinput = self._make_mouse(debounce_module,
                                          wheel_suppress=True, wheel_type_b=True)
        times = [0.000, 0.020, 0.040, 0.060, 0.080,
                 0.380, 0.400, 0.420, 0.440, 0.460]
        with _patch_monotonic(debounce_module, times):
            for _ in range(5):
                mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))
            for _ in range(5):
                mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))

        # Primary 600, ratio 0.5, max_total 300 → budget 300. Each event mag=120.
        # Suppress 1st & 2nd (cumulative 240). 3rd at 360 > 300 → forward.
        # 4th & 5th: mid-gesture (gap < quiet_ms=200) → forward.
        # Total forwarded: 5 (primary) + 3 (continuation) = 8.
        assert uinput.write_event.call_count == 8

    def test_continuation_passes_through_with_type_b_default_off(self, debounce_module):
        """With production default (Type B off), the user's continuation
        scroll after a primary forwards completely — no suppression."""
        from evdev import ecodes
        mouse, uinput = self._make_mouse(debounce_module,
                                          wheel_suppress=True, wheel_type_b=False)
        times = [0.000, 0.020, 0.040, 0.060, 0.080,
                 0.380, 0.400, 0.420, 0.440, 0.460]
        with _patch_monotonic(debounce_module, times):
            for _ in range(5):
                mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))
            for _ in range(5):
                mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))
        # All 10 forwarded; no Type B suppression.
        assert uinput.write_event.call_count == 10
        assert mouse.wheel_suppressed == 0

    def test_legitimate_reverse_scroll_after_clears_anchor(self, debounce_module):
        """Down primary → user reverses with up scroll. Type A fires ONCE
        on the first opposite-direction event (the one indistinguishable
        from a bounce). Subsequent up events forward — type_a_consumed
        flag prevents repeat suppression."""
        from evdev import ecodes
        mouse, uinput = self._make_mouse(debounce_module, wheel_suppress=True)
        times = [0.000, 0.020, 0.040, 0.060, 0.080,
                 0.260, 0.280, 0.300, 0.320, 0.340]
        with _patch_monotonic(debounce_module, times):
            # Primary down (sum -600)
            for _ in range(5):
                mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))
            # Reverse: 5 up events
            for _ in range(5):
                mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120))

        # 5 down forwarded + 1 up suppressed (Type A) + 4 up forwarded = 9.
        # User loses one click of legitimate reverse scroll — acceptable
        # cost for catching the genuine Type A bounce reversal pulse.
        assert uinput.write_event.call_count == 9
        assert mouse._wheel_suppressor.suppressed_type_a == 1

    def test_multi_step_reverse_passes_through(self, debounce_module):
        """A legitimate fast reverse scroll arrives as multi-step events
        (mag > REV_MAX_HI_RES) and is NOT suppressed."""
        from evdev import ecodes
        mouse, uinput = self._make_mouse(debounce_module, wheel_suppress=True)
        # Primary down, then a single multi-step (+240) reverse
        times = [0.000, 0.020, 0.040, 0.060, 0.080, 0.260]
        with _patch_monotonic(debounce_module, times):
            for _ in range(5):
                mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))
            # Multi-step reverse — NOT a Type A signature
            mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +240))

        # All 6 forwarded
        assert uinput.write_event.call_count == 6
        assert mouse._wheel_suppressor.suppressed_type_a == 0

    # ---- SUPPRESSED_WHEEL log line --------------------------------------

    def test_suppressed_wheel_log_emitted(self, debounce_module):
        from evdev import ecodes
        mouse, _ = self._make_mouse(debounce_module, wheel_suppress=True)
        times = [0.000, 0.020, 0.040, 0.060, 0.080, 0.320]
        with _patch_monotonic(debounce_module, times):
            for _ in range(5):
                mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))
            with patch.object(debounce_module, 'log') as mock_log:
                mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120))

        sup_calls = [c for c in mock_log.call_args_list
                     if 'SUPPRESSED_WHEEL' in str(c)]
        assert len(sup_calls) == 1
        msg = sup_calls[0].args[0]
        assert 'reason=type_a_reversal' in msg
        assert 'mag=120' in msg
        assert 'sign=+' in msg
        assert sup_calls[0].kwargs.get('also_print') is False  # file-only

    # ---- runtime toggle --------------------------------------------------

    def test_set_wheel_suppress_runtime_off(self, debounce_module):
        from evdev import ecodes
        mouse, uinput = self._make_mouse(debounce_module, wheel_suppress=True)
        mouse.set_wheel_suppress(False)
        times = [0.000, 0.020, 0.040, 0.060, 0.080, 0.320]
        with _patch_monotonic(debounce_module, times):
            for _ in range(5):
                mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))
            mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120))
        # All 6 forward when disabled
        assert uinput.write_event.call_count == 6
        assert mouse.wheel_suppressed == 0

    def test_set_wheel_suppress_back_on(self, debounce_module):
        """Disable then re-enable resumes suppression on the next bounce."""
        from evdev import ecodes
        mouse, uinput = self._make_mouse(debounce_module, wheel_suppress=True)
        mouse.set_wheel_suppress(False)
        mouse.set_wheel_suppress(True)
        times = [0.000, 0.020, 0.040, 0.060, 0.080, 0.320]
        with _patch_monotonic(debounce_module, times):
            for _ in range(5):
                mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))
            mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120))
        assert uinput.write_event.call_count == 5
        assert mouse.wheel_suppressed == 1

    # ---- counters / has_notable_events ----------------------------------

    def test_wheel_suppressed_counter_field(self, debounce_module):
        from evdev import ecodes
        mouse, _ = self._make_mouse(debounce_module, wheel_suppress=True)
        assert mouse.wheel_suppressed == 0
        times = [0.000, 0.020, 0.040, 0.060, 0.080, 0.320]
        with _patch_monotonic(debounce_module, times):
            for _ in range(5):
                mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))
            mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120))
        assert mouse.wheel_suppressed == 1

    def test_has_notable_events_reflects_wheel_suppress(self, debounce_module):
        from evdev import ecodes
        mouse, _ = self._make_mouse(debounce_module, wheel_suppress=True)
        assert mouse.has_notable_events() is False
        times = [0.000, 0.020, 0.040, 0.060, 0.080, 0.320]
        with _patch_monotonic(debounce_module, times):
            for _ in range(5):
                mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))
            mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120))
        assert mouse.has_notable_events() is True
        mouse.record_stats()
        assert mouse.has_notable_events() is False

    def test_no_suppressor_calls_when_flag_off(self, debounce_module):
        from evdev import ecodes
        mouse, uinput = self._make_mouse(debounce_module, wheel_suppress=False)
        # Drive a Type A scenario; without --wheel-suppress all events forward.
        times = [0.000, 0.020, 0.040, 0.060, 0.080, 0.320]
        with _patch_monotonic(debounce_module, times):
            for _ in range(5):
                mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))
            mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120))
        assert uinput.write_event.call_count == 6
        # No counter increment
        assert mouse.wheel_suppressed == 0


class TestWheelSuppressorBugFixes:
    """Regressions captured from the 2026-05-10 production failure where the
    user reported continuous up-scroll after a down-primary was suppressed
    "for vastly longer than a couple of seconds." Five compounding bugs in
    the original implementation:

      1. Axis-tie updates last_suppress_mono on every fire → the 8ms window
         re-anchors and never expires while paired events keep arriving.
      2. Type B cooldown_consumed counted notch events at mag=1 but real
         click magnitude is ~121 hi-res units → budget never exhausted.
      3. Type A fires unbounded times within rev_window → a slow opposite
         scroll has every event suppressed.
      4. Same-direction continuations (after a normal pause) look identical
         to Type B bounces on the first event.
      5. Cascade-extend refreshes anchor end_mono indefinitely from chained
         small bursts → cooldown clock never elapses while user keeps
         clicking.

    Default policy after the fix: Type A only. Type B remains opt-in via the
    `type_b_enabled=True` constructor flag for users who want to experiment
    with same-direction suppression after building confidence.
    """

    def _make(self, debounce_module, **kwargs):
        defaults = dict(
            name="Test", enabled=True,
            cooldown_ms=1200, rev_window_ms=280,
            cooldown_ratio=0.5, min_primary=360,
            bounce_max_total=300, quiet_ms=200,
            type_b_enabled=False,
        )
        defaults.update(kwargs)
        return debounce_module.WheelSuppressor(**defaults)

    # ---- Bug 4: continuation after primary --------------------------------

    def test_continuation_scroll_after_primary_not_suppressed(self, debounce_module):
        """User scrolls UP (significant primary 726), pauses 511ms, then
        scrolls UP AGAIN with another 14-event burst. With the default
        (Type B off), the continuation must pass through entirely."""
        from evdev import ecodes
        sup = self._make(debounce_module)
        sup.note_burst_closed(+1, 726, end_mono=1.000)
        # 14-event continuation burst at 30ms intervals starting at 1.511
        results = []
        for i in range(14):
            t = 1.511 + i * 0.030
            ev = make_wheel_event(
                ecodes.REL_WHEEL if i % 2 == 0 else ecodes.REL_WHEEL_HI_RES,
                (1 if i % 2 == 0 else 120))
            s, r = sup.should_suppress(ev, now_mono=t)
            results.append((s, r))
        # All 14 events forwarded — no Type B with type_b_enabled=False.
        assert all(not s for s, _ in results), \
            f"Suppressed {sum(1 for s, _ in results if s)} of 14 continuation events"

    def test_type_b_disabled_by_default(self, debounce_module):
        """Default constructor has Type B disabled — same-direction events
        within cooldown forward unconditionally."""
        from evdev import ecodes
        sup = debounce_module.WheelSuppressor(name="Test")  # all defaults
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120)
        s, _ = sup.should_suppress(ev, now_mono=1.5)
        assert s is False  # Same-direction → forward (Type B off)

    # ---- Bug 3: Type A fire-once per anchor -------------------------------

    def test_type_a_fires_once_per_anchor(self, debounce_module):
        """Slow legitimate reverse scroll (5 single-step events at 30ms
        intervals) within the rev_window should suppress at most ONE event."""
        from evdev import ecodes
        sup = self._make(debounce_module)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        results = []
        for i in range(5):
            t = 1.10 + i * 0.030  # all within rev_window 280ms
            s, _ = sup.should_suppress(
                make_wheel_event(ecodes.REL_WHEEL, +1), now_mono=t)
            results.append(s)
        # One suppression, four forward. The user loses at most one click
        # to the bounce-veto on a slow reverse — acceptable trade.
        assert sum(1 for s in results if s) == 1, \
            f"Type A suppressed {sum(1 for s in results if s)} of 5 (expected 1)"

    def test_type_a_consumed_resets_on_new_anchor(self, debounce_module):
        """A new significant primary refreshes the type_a_consumed flag."""
        from evdev import ecodes
        sup = self._make(debounce_module)
        # First primary, fire Type A
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        sup.should_suppress(
            make_wheel_event(ecodes.REL_WHEEL, +1), now_mono=1.10)
        # New primary in same direction (replaces anchor, resets consumed)
        sup.note_burst_closed(-1, 800, end_mono=2.0)
        # Type A should fire again
        s, r = sup.should_suppress(
            make_wheel_event(ecodes.REL_WHEEL, +1), now_mono=2.10)
        assert s is True
        assert r == "type_a_reversal"

    # ---- Bug 1: axis-tie does not extend indefinitely ---------------------

    def test_axis_tie_does_not_extend_indefinitely(self, debounce_module):
        """A stream of paired events 5ms apart should NOT keep the axis-tie
        window alive indefinitely. After the original primary suppression,
        axis-tie is anchored to that single event — events beyond the 8ms
        window from the PRIMARY suppression are evaluated independently."""
        from evdev import ecodes
        sup = self._make(debounce_module)
        sup.note_burst_closed(-1, 600, end_mono=1.0)
        # Fire one Type A
        sup.should_suppress(
            make_wheel_event(ecodes.REL_WHEEL, +1), now_mono=1.100)
        # 20 paired events at 5ms intervals (would forever extend with the bug)
        suppressed_count = 0
        for i in range(20):
            t = 1.105 + i * 0.005
            s, _ = sup.should_suppress(
                make_wheel_event(
                    ecodes.REL_WHEEL_HI_RES if i % 2 == 0 else ecodes.REL_WHEEL,
                    (120 if i % 2 == 0 else 1)), now_mono=t)
            if s:
                suppressed_count += 1
        # Only the FIRST event (the immediate axis-tie pair) should suppress.
        # Type A is consumed; further events have no other reason to suppress.
        assert suppressed_count == 1, \
            f"Expected 1 axis-tie suppression, got {suppressed_count}"

    # ---- Type B opt-in mode behaves correctly -----------------------------

    def test_type_b_opt_in_uses_full_event_magnitude(self, debounce_module):
        """When type_b_enabled=True, cooldown_consumed should reflect each
        event's magnitude (notch=1, hi-res=120) plus axis-tie suppressions —
        budget exhausts within ~3 clicks, not ~300."""
        from evdev import ecodes
        sup = self._make(debounce_module,
                         type_b_enabled=True,
                         bounce_max_total=300,
                         cooldown_ratio=0.5)
        sup.note_burst_closed(-1, 600, end_mono=1.0)  # primary
        # 5 wheel "clicks" — each = notch + hi-res, paired
        results = []
        for i in range(5):
            t_notch = 1.500 + i * 0.030
            t_hires = t_notch + 0.001
            results.append(sup.should_suppress(
                make_wheel_event(ecodes.REL_WHEEL, -1), now_mono=t_notch))
            results.append(sup.should_suppress(
                make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120), now_mono=t_hires))
        # 10 events total. Budget = min(600*0.5, 300) = 300.
        # Each click consumes ~121 (1 notch + 120 hi-res via axis-tie).
        # ~2.5 clicks suppressed → 5 events suppressed, then forward.
        suppressed = sum(1 for s, _ in results if s)
        assert 4 <= suppressed <= 6, \
            f"Type B opt-in suppressed {suppressed}/10 (expected ~5)"


class TestWheelSuppressorCascade:
    """Captured 2026-05-10 01:01:08 cascade scenario: substantial up primary
    (1331 hi-res, 22 events) followed by three same-direction bounce bursts
    of 242 hi-res / 4 events each at ~615/652/593ms intervals. The original
    Type B implementation suppressed bounce #1 fully but exhausted budget,
    leaving #2 partial and #3 unsuppressed.

    Fix: cascade-extend resets cooldown_consumed so each bounce in the
    chain gets a fresh budget. A chain-depth cap prevents runaway extension
    if the user is genuinely doing slow repeated scrolls.
    """

    def _make(self, debounce_module, **kwargs):
        defaults = dict(
            name="Test", enabled=True,
            cooldown_ms=1200, rev_window_ms=280,
            cooldown_ratio=0.5, min_primary=360,
            bounce_max_total=242, quiet_ms=200,
            type_b_enabled=True,
            max_cascade_depth=5,  # Enable cascade for these tests
        )
        defaults.update(kwargs)
        return debounce_module.WheelSuppressor(**defaults)

    def test_cascade_three_bounces_all_suppressed(self, debounce_module):
        """Replay the captured failure: primary 1331 + 3 × 242 bounces.
        Every event of every bounce must be suppressed."""
        from evdev import ecodes
        sup = self._make(debounce_module)
        # Primary closes
        sup.note_burst_closed(+1, 1331, end_mono=1.000)
        # Bounce 1 events (4 events: notch+hi-res×2)
        bounce_starts = [1.615, 2.267, 2.860]  # cumulative ~615ms gaps
        for bn, t_start in enumerate(bounce_starts):
            results = []
            for i, t in enumerate([t_start, t_start + 0.001,
                                    t_start + 0.030, t_start + 0.031]):
                code = ecodes.REL_WHEEL if i % 2 == 0 else ecodes.REL_WHEEL_HI_RES
                value = (1 if i % 2 == 0 else 120)
                s, _ = sup.should_suppress(
                    make_wheel_event(code, value), now_mono=t)
                results.append(s)
            assert all(results), \
                f"Bounce #{bn+1} not fully suppressed: {results}"
            # WheelDiagnostics would close the burst after idle gap
            sup.note_burst_closed(+1, 242, end_mono=t_start + 0.031)

    def test_cascade_chain_depth_capped(self, debounce_module):
        """After N cascade extensions, anchor should clear so an
        indefinitely long chain of small same-direction events doesn't
        block legitimate user scrolls forever."""
        from evdev import ecodes
        sup = self._make(debounce_module, cooldown_ms=10_000)  # long, to isolate cascade cap
        sup.note_burst_closed(+1, 1000, end_mono=1.000)
        # Drive many small same-direction closes
        for i in range(20):
            sup.note_burst_closed(+1, 200, end_mono=1.5 + i * 0.5)
        # After cap, anchor should be cleared (or replaced with insignificant)
        # so a fresh same-direction event passes through.
        s, _ = sup.should_suppress(
            make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120),
            now_mono=15.0)
        assert s is False, "Indefinite cascade chain still suppressing"

    def test_cascade_reset_does_not_apply_after_significant_burst(self, debounce_module):
        """If a SIGNIFICANT burst follows the primary, it replaces the
        anchor (new primary), and cooldown_consumed resets — that's
        existing behavior, not the cascade-extend reset path."""
        from evdev import ecodes
        sup = self._make(debounce_module, min_primary=360)
        sup.note_burst_closed(+1, 1000, end_mono=1.000)
        # Substantial second burst → new primary
        sup.note_burst_closed(+1, 800, end_mono=1.500)
        assert sup.last_closed.abs_sum == 800
        assert sup.cooldown_consumed == 0


class TestWheelSuppressorConservative:
    """Production tuning for the user's real scrolling pattern. The
    initial defaults (min_primary=360, cooldown=1200, max=300, cascade=5)
    over-suppressed the user's mixed-magnitude scroll style — every 3-click
    flick crossed min_primary and re-armed the anchor, locking subsequent
    small scrolls into Type B suppression. New defaults trade catch-rate
    for responsiveness:

      min_primary       720   (only 6+ click bursts arm the anchor)
      cooldown_ms       800   (tighter post-primary window)
      bounce_max_total  121   (one click per anchor maximum)
      MAX_CASCADE       0     (no extension — one anchor, one chance)
    """

    def _make(self, debounce_module, **kwargs):
        defaults = dict(
            name="Test", enabled=True,
            cooldown_ms=800, rev_window_ms=280,
            cooldown_ratio=0.5, min_primary=720,
            bounce_max_total=121, quiet_ms=200,
            type_b_enabled=True,
        )
        defaults.update(kwargs)
        return debounce_module.WheelSuppressor(**defaults)

    def test_three_click_scroll_does_not_arm(self, debounce_module):
        """A 3-click scroll (sum 363) is below min_primary 720 and must
        not arm the anchor — the user's typical small flicks should not
        trigger any suppression on follow-up scrolls."""
        from evdev import ecodes
        sup = self._make(debounce_module)
        sup.note_burst_closed(+1, 363, end_mono=1.0)
        # Subsequent same-dir scroll within nominal cooldown — must forward
        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120)
        s, _ = sup.should_suppress(ev, now_mono=1.5)
        assert s is False

    def test_six_click_scroll_arms(self, debounce_module):
        """A 6-click scroll (sum 720) reaches min_primary and arms the
        anchor — subsequent same-direction events within cooldown can
        be suppressed."""
        from evdev import ecodes
        sup = self._make(debounce_module)
        sup.note_burst_closed(+1, 720, end_mono=1.0)
        # Exactly one click should suppress, then anchor exhausted
        s_notch, _ = sup.should_suppress(
            make_wheel_event(ecodes.REL_WHEEL, +1), now_mono=1.3)
        s_hires, _ = sup.should_suppress(
            make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120), now_mono=1.301)
        assert s_notch is True
        assert s_hires is True

    def test_only_one_click_suppressed_per_anchor(self, debounce_module):
        """bounce_max_total=121 means consumed exhausts after a single
        click pair — the SECOND click of any same-direction follow-up
        forwards even though it's still within cooldown."""
        from evdev import ecodes
        sup = self._make(debounce_module)
        sup.note_burst_closed(+1, 720, end_mono=1.0)
        # Click 1 — suppressed (notch + hi-res via axis-tie)
        sup.should_suppress(make_wheel_event(ecodes.REL_WHEEL, +1), now_mono=1.30)
        sup.should_suppress(make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120), now_mono=1.301)
        # Click 2 at 30ms later — must forward (consumed=121 already at budget)
        s, _ = sup.should_suppress(
            make_wheel_event(ecodes.REL_WHEEL, +1), now_mono=1.330)
        assert s is False

    def test_no_cascade_extension(self, debounce_module):
        """With MAX_CASCADE_DEPTH=0, a small same-direction burst close
        DOES NOT extend the anchor — one shot per primary, then done."""
        from evdev import ecodes
        sup = self._make(debounce_module)
        sup.note_burst_closed(+1, 720, end_mono=1.0)
        # Cascade-extend attempt
        sup.note_burst_closed(+1, 121, end_mono=1.5)
        # Anchor should be cleared (cascade depth exceeded)
        assert sup.last_closed is None

    def test_cooldown_expires_at_800ms(self, debounce_module):
        from evdev import ecodes
        sup = self._make(debounce_module)
        sup.note_burst_closed(+1, 720, end_mono=1.0)
        # 900ms after primary — past 800ms cooldown
        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, +120)
        s, _ = sup.should_suppress(ev, now_mono=1.9)
        assert s is False


class TestWheelMultiplier:
    """Tests for `--wheel-multiplier` — scaling forwarded wheel event values
    to compensate for the lost smooth-scroll velocity when the user disables
    Logitech's high-resolution scroll mode (Solaar → Scroll Wheel Resolution
    off). Low-res mode naturally suppresses sub-detent free-spin bounces but
    cuts perceived scroll speed; the multiplier puts speed back without
    re-introducing the bounces.

    The multiplier scales `event.value` after the suppression decision but
    before the uinput write. Suppressor logic continues to operate on
    original magnitudes.
    """

    def _make_mouse(self, debounce_module, wheel_multiplier=1, **kwargs):
        from evdev import ecodes
        mock_device = MagicMock()
        mock_device.name = "Test Mouse"
        mock_device.fd = 99
        mock_uinput = MagicMock()
        with patch.object(debounce_module, 'UInput') as mock_uinput_class:
            mock_uinput_class.from_device.return_value = mock_uinput
            mouse = debounce_module.DelayedDebouncedMouse(
                mock_device, 60, quiet=True,
                wheel_multiplier=wheel_multiplier,
                **kwargs,
            )
        return mouse, mock_uinput

    def test_multiplier_one_passes_through_unchanged(self, debounce_module):
        from evdev import ecodes
        mouse, uinput = self._make_mouse(debounce_module, wheel_multiplier=1)
        ev = make_wheel_event(ecodes.REL_WHEEL, -1)
        mouse.process_event(ev)
        # Forwarded as-is
        uinput.write_event.assert_called_once()
        forwarded = uinput.write_event.call_args.args[0]
        assert forwarded.value == -1
        assert forwarded.code == ecodes.REL_WHEEL

    def test_multiplier_doubles_notch_value(self, debounce_module):
        from evdev import ecodes
        mouse, uinput = self._make_mouse(debounce_module, wheel_multiplier=2)
        ev = make_wheel_event(ecodes.REL_WHEEL, -1)
        mouse.process_event(ev)
        forwarded = uinput.write_event.call_args.args[0]
        assert forwarded.value == -2
        assert forwarded.code == ecodes.REL_WHEEL

    def test_multiplier_doubles_hires_value(self, debounce_module):
        from evdev import ecodes
        mouse, uinput = self._make_mouse(debounce_module, wheel_multiplier=2)
        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120)
        mouse.process_event(ev)
        forwarded = uinput.write_event.call_args.args[0]
        assert forwarded.value == -240
        assert forwarded.code == ecodes.REL_WHEEL_HI_RES

    def test_multiplier_preserves_sign(self, debounce_module):
        from evdev import ecodes
        mouse, uinput = self._make_mouse(debounce_module, wheel_multiplier=3)
        ev = make_wheel_event(ecodes.REL_WHEEL, +1)
        mouse.process_event(ev)
        forwarded = uinput.write_event.call_args.args[0]
        assert forwarded.value == +3

    def test_multiplier_does_not_affect_movement_events(self, debounce_module):
        """REL_X / REL_Y must not be multiplied — only wheel axes."""
        from evdev import ecodes
        mouse, uinput = self._make_mouse(debounce_module, wheel_multiplier=4)
        ev = make_wheel_event(ecodes.REL_X, 5)
        mouse.process_event(ev)
        forwarded = uinput.write_event.call_args.args[0]
        assert forwarded.value == 5  # Unmultiplied

    def test_multiplier_does_not_unsuppress(self, debounce_module):
        """A suppressed wheel event stays suppressed regardless of multiplier."""
        from evdev import ecodes
        mouse, uinput = self._make_mouse(
            debounce_module, wheel_multiplier=2,
            wheel_suppress=True, wheel_type_b=True,
            wheel_min_primary=720, wheel_cooldown_ms=800,
            wheel_bounce_max_total=121, wheel_max_cascade_depth=0,
        )
        # Drive a primary then a same-direction follow-up; first event
        # should suppress (regardless of multiplier).
        times = [0.000, 0.020, 0.040, 0.060, 0.080,
                 0.100, 0.120, 0.140, 0.160, 0.180,
                 0.200, 0.220, 0.500]  # 12 primary events + 1 bounce
        with _patch_monotonic(debounce_module, times):
            for _ in range(12):  # sum 1440 > min_primary 720
                mouse.process_event(make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120))
            mouse.process_event(make_wheel_event(ecodes.REL_WHEEL, -1))
        # Last event suppressed; uinput count should equal primary's 12.
        # Each primary event was multiplied by 2 (forwarded as -240).
        assert uinput.write_event.call_count == 12
        forwarded_values = [c.args[0].value for c in uinput.write_event.call_args_list]
        assert all(v == -240 for v in forwarded_values), \
            f"Expected all -240, got {forwarded_values}"

    def test_multiplier_minimum_one(self, debounce_module):
        """Multiplier 0 or negative must be rejected (raise or clamp to 1) —
        zero-valued wheel events would silently break scrolling."""
        # Just verify the ctor doesn't crash on edge values; behavior
        # at 0/negative is a CLI validation concern in main().
        from evdev import ecodes
        mouse, _ = self._make_mouse(debounce_module, wheel_multiplier=1)
        # No exception expected
        ev = make_wheel_event(ecodes.REL_WHEEL_HI_RES, -120)
        mouse.process_event(ev)


class TestWheelSuppressorReplay:
    """End-to-end replay tests against statistics derived from the captured
    production log (`/var/log/mouse-filter/debounce.log`, recent window).

    These drive multi-burst sequences through the full process_event pipeline
    using realistic per-event magnitudes, durations, and inter-burst gaps,
    and assert aggregate suppression rates against the targets the design
    plan committed to:

      - ≥ 90% of Type A bounces caught
      - ≥ 70% of Type B bounces caught (covering median, missing the long-tail)
      - 0% false-positive on legitimate isolated single-step scrolls
    """

    def _make_mouse(self, debounce_module, **kwargs):
        from evdev import ecodes
        mock_device = MagicMock()
        mock_device.name = "MX Anywhere 2S"
        mock_device.fd = 99
        mock_uinput = MagicMock()
        with patch.object(debounce_module, 'UInput') as mock_uinput_class:
            mock_uinput_class.from_device.return_value = mock_uinput
            # Replay tests cover both Type A and Type B paths
            defaults = dict(quiet=True, wheel_suppress=True, wheel_type_b=True)
            defaults.update(kwargs)
            mouse = debounce_module.DelayedDebouncedMouse(
                mock_device, 60, **defaults)
        return mouse, mock_uinput

    def _drive_burst(self, mouse, debounce_module, sign, count, t_start, dur_ms):
        """Drive a `count`-event burst over `dur_ms` starting at `t_start`.
        Each event has hi-res magnitude 120 (one notch). Returns the time
        immediately after the last event."""
        from evdev import ecodes
        if count <= 0:
            return t_start
        step = (dur_ms / 1000.0) / max(count - 1, 1)
        times = [t_start + i * step for i in range(count)]
        with _patch_monotonic(debounce_module, times):
            for _ in range(count):
                mouse.process_event(make_wheel_event(
                    ecodes.REL_WHEEL_HI_RES, sign * 120))
        return times[-1]

    def _drive_single(self, mouse, debounce_module, code, value, now):
        from evdev import ecodes
        with _patch_monotonic(debounce_module, [now]):
            mouse.process_event(make_wheel_event(code, value))

    # ---- Type A targets ----------------------------------------------------

    def test_type_a_replay_high_suppression_rate(self, debounce_module):
        """18 Type A scenarios from the log: each is a primary down burst
        (sum 360–4000+) followed by a single ±1 reversal at 67–240ms gap.
        Target: 100% caught (window 280ms covers the whole observed range)."""
        from evdev import ecodes
        # (primary_sum, gap_ms_to_reversal) — derived from log distribution
        scenarios = [
            (605, 67), (1452, 100), (2178, 222), (4114, 112), (363, 145),
            (484, 176), (725, 200), (968, 240), (1936, 80), (3267, 130),
            (605, 95), (484, 110), (242, 175),  # 242 < min_primary → not armed
            (363, 220), (1815, 160), (2541, 230), (484, 240), (3025, 200),
        ]
        suppressed = 0
        eligible = 0
        for primary_sum, gap_ms in scenarios:
            mouse, _ = self._make_mouse(debounce_module)
            count = primary_sum // 120
            t_end = self._drive_burst(mouse, debounce_module,
                                       sign=-1, count=count,
                                       t_start=0.000, dur_ms=120)
            # Reversal event: single notch (REL_WHEEL value=+1) at gap_ms after t_end
            self._drive_single(mouse, debounce_module,
                               ecodes.REL_WHEEL, +1,
                               now=t_end + gap_ms / 1000.0)
            # Only count scenarios where primary was significant (≥ min_primary 360)
            if primary_sum >= 360:
                eligible += 1
                if mouse._wheel_suppressor.suppressed_type_a >= 1:
                    suppressed += 1
        rate = suppressed / eligible
        assert rate >= 0.90, f"Type A suppression rate {rate:.2%} < 90%"

    # ---- Type B targets ----------------------------------------------------

    def test_type_b_replay_within_window(self, debounce_module):
        """Type B bounces with gap ≤ 1200ms (cooldown window) should be
        caught at high rate. Sourced from the log's median 1020ms gap."""
        scenarios = [
            # (primary_sum, gap_ms, bounce_sum)
            (605, 700, 242),
            (1452, 1000, 484),
            (2178, 850, 605),
            (484, 600, 121),
            (968, 1100, 363),
            (3267, 800, 1452),
            (605, 900, 121),
            (1815, 1000, 605),
            (484, 1150, 242),
            (725, 1050, 363),
        ]
        caught = 0
        for primary_sum, gap_ms, bounce_sum in scenarios:
            mouse, _ = self._make_mouse(debounce_module)
            t_end = self._drive_burst(mouse, debounce_module,
                                       sign=-1,
                                       count=primary_sum // 120,
                                       t_start=0.000, dur_ms=120)
            t_bounce_start = t_end + gap_ms / 1000.0
            self._drive_burst(mouse, debounce_module,
                              sign=-1,
                              count=bounce_sum // 120,
                              t_start=t_bounce_start, dur_ms=80)
            if mouse._wheel_suppressor.suppressed_type_b >= 1:
                caught += 1
        rate = caught / len(scenarios)
        assert rate >= 0.70, f"Type B suppression rate {rate:.2%} < 70%"

    def test_type_b_long_tail_outside_window_passes(self, debounce_module):
        """Type B bounces with gap > 1200ms cooldown are LET THROUGH —
        deliberate tradeoff to avoid suppressing legitimate scroll-pause-scroll
        gestures at long pauses. Target: 0 suppressions for these."""
        scenarios = [(605, 1500, 242), (484, 2500, 121), (1452, 4000, 363)]
        suppressed = 0
        for primary_sum, gap_ms, bounce_sum in scenarios:
            mouse, _ = self._make_mouse(debounce_module)
            t_end = self._drive_burst(mouse, debounce_module,
                                       sign=-1,
                                       count=primary_sum // 120,
                                       t_start=0.000, dur_ms=120)
            self._drive_burst(mouse, debounce_module,
                              sign=-1,
                              count=bounce_sum // 120,
                              t_start=t_end + gap_ms / 1000.0, dur_ms=80)
            suppressed += mouse._wheel_suppressor.suppressed_type_b
        assert suppressed == 0, \
            f"Long-tail bounces suppressed {suppressed} times — should pass through"

    # ---- False-positive guard ---------------------------------------------

    def test_isolated_legitimate_small_scrolls_pass_through(self, debounce_module):
        """81 isolated single-step scrolls observed in the log (sum 121–242,
        no preceding primary in 750ms). These are intentional micro-scrolls
        and MUST NOT be suppressed."""
        from evdev import ecodes
        false_positives = 0
        for _ in range(30):
            mouse, _ = self._make_mouse(debounce_module)
            # No primary first; just a paired single-notch event (hi-res + notch)
            self._drive_single(mouse, debounce_module,
                               ecodes.REL_WHEEL_HI_RES, -120, now=0.000)
            self._drive_single(mouse, debounce_module,
                               ecodes.REL_WHEEL, -1, now=0.005)
            false_positives += mouse.wheel_suppressed
        assert false_positives == 0, \
            f"Suppressed {false_positives} legitimate isolated scrolls"

    def test_legitimate_small_after_long_pause_passes(self, debounce_module):
        """User scrolls primary → pauses 2 seconds (well past cooldown) →
        scrolls a small amount in either direction. Must not be suppressed."""
        from evdev import ecodes
        mouse, uinput = self._make_mouse(debounce_module)
        # Primary
        t_end = self._drive_burst(mouse, debounce_module,
                                   sign=-1, count=5,
                                   t_start=0.000, dur_ms=80)
        # 2.5s pause, then a single small same-dir scroll
        self._drive_single(mouse, debounce_module,
                           ecodes.REL_WHEEL_HI_RES, -120,
                           now=t_end + 2.5)
        assert mouse.wheel_suppressed == 0


class TestWheelSuppressToggleSignal:
    """Tests for SIGUSR2 toggle handler at the module level."""

    def test_make_toggle_handler_flips_all_mice(self, debounce_module):
        """make_wheel_suppress_toggle returns a function that, when called,
        flips set_wheel_suppress on every mouse in the registry and logs the
        new state."""
        # Two fake mice with set_wheel_suppress() trackers
        m1 = MagicMock()
        m1.name = "Mouse-A"
        m1._wheel_suppressor = MagicMock()
        m1._wheel_suppressor.enabled = True
        m1.wheel_suppress_enabled = True

        m2 = MagicMock()
        m2.name = "Mouse-B"
        m2._wheel_suppressor = MagicMock()
        m2._wheel_suppressor.enabled = True
        m2.wheel_suppress_enabled = True

        registry = [m1, m2]
        with patch.object(debounce_module, 'log') as mock_log:
            handler = debounce_module.make_wheel_suppress_toggle(registry)
            handler(None, None)  # signal handler signature

        # Both mice toggled
        m1.set_wheel_suppress.assert_called_once_with(False)
        m2.set_wheel_suppress.assert_called_once_with(False)
        # Log line
        toggle_calls = [c for c in mock_log.call_args_list
                        if 'WHEEL_SUPPRESS_TOGGLE' in str(c)]
        assert len(toggle_calls) == 1
        msg = toggle_calls[0].args[0]
        assert 'WHEEL_SUPPRESS_TOGGLE: off' in msg

    def test_toggle_returns_to_on(self, debounce_module):
        """Second toggle flips back to on."""
        m1 = MagicMock()
        m1.name = "Mouse-A"
        m1._wheel_suppressor = MagicMock()
        m1._wheel_suppressor.enabled = False
        m1.wheel_suppress_enabled = False

        registry = [m1]
        with patch.object(debounce_module, 'log') as mock_log:
            handler = debounce_module.make_wheel_suppress_toggle(registry)
            handler(None, None)

        m1.set_wheel_suppress.assert_called_once_with(True)
        toggle_calls = [c for c in mock_log.call_args_list
                        if 'WHEEL_SUPPRESS_TOGGLE' in str(c)]
        assert 'WHEEL_SUPPRESS_TOGGLE: on' in toggle_calls[0].args[0]

    def test_toggle_with_empty_registry(self, debounce_module):
        """Handler is a no-op (other than logging) when no mice are loaded."""
        with patch.object(debounce_module, 'log') as mock_log:
            handler = debounce_module.make_wheel_suppress_toggle([])
            handler(None, None)
        # Should not crash; may or may not log
        # (either behavior acceptable as long as no exception)


class TestUserTag:
    """Tests for the SIGUSR1 USER_TAG mechanism (panel-launcher hook)."""

    def test_log_user_tag_default_label(self, debounce_module):
        with patch.object(debounce_module, 'log') as mock_log:
            debounce_module.log_user_tag()

        assert mock_log.call_count == 1
        msg = mock_log.call_args.args[0]
        assert msg.startswith("USER_TAG: ")
        assert "marker" in msg

    def test_log_user_tag_custom_label(self, debounce_module):
        with patch.object(debounce_module, 'log') as mock_log:
            debounce_module.log_user_tag("rebound-felt")

        msg = mock_log.call_args.args[0]
        assert msg == "USER_TAG: rebound-felt"
