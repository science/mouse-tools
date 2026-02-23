"""Tests for mouse-debounce filter logic.

Uses mock evdev devices to test the DelayedDebouncedMouse debounce logic
without requiring root access or real hardware.
"""

import time
from unittest.mock import MagicMock, patch

import pytest


# We need to import from the mouse-debounce script which has a hyphenated name
# and no .py extension. Use importlib.
import importlib.util
import importlib.machinery
from pathlib import Path

SCRIPT_PATH = Path(__file__).parent.parent / "mouse-debounce"


@pytest.fixture
def debounce_module():
    """Import the mouse-debounce script as a module."""
    loader = importlib.machinery.SourceFileLoader("mouse_debounce", str(SCRIPT_PATH))
    spec = importlib.util.spec_from_loader("mouse_debounce", loader)
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

    def test_release_held_not_forwarded_immediately(self, debounce_module):
        """A button release should be queued, not forwarded immediately."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module)

        # Press first
        press = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1)
        mouse.process_event(press)
        uinput.write_event.reset_mock()

        # Release
        release = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0)
        mouse.process_event(release)

        # Release should NOT have been forwarded yet
        uinput.write_event.assert_not_called()
        assert ecodes.BTN_LEFT in mouse.pending_release

    def test_release_forwarded_after_threshold(self, debounce_module):
        """A held release should be forwarded once the threshold expires."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module, threshold_ms=50)

        # Press then release
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        uinput.write_event.reset_mock()

        release = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0)
        mouse.process_event(release)

        # Not forwarded yet
        uinput.write_event.assert_not_called()

        # Wait past threshold
        time.sleep(0.06)
        mouse.flush_pending()

        # Should be forwarded (release + SYN_REPORT)
        calls = uinput.write_event.call_args_list
        assert calls[0].args[0] == release
        assert ecodes.BTN_LEFT not in mouse.pending_release

    def test_bounce_suppressed(self, debounce_module):
        """A release followed by a fast re-press should suppress both."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module, threshold_ms=60)

        # Initial press
        press1 = make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1)
        mouse.process_event(press1)
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

        # Press left
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        # Release left (pending)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
        uinput.write_event.reset_mock()

        # Press RIGHT (different button) — should NOT be treated as bounce
        right_press = make_event(ecodes.EV_KEY, ecodes.BTN_RIGHT, 1)
        result = mouse.process_event(right_press)

        assert result is True
        uinput.write_event.assert_called_once_with(right_press)
        # Left release should still be pending
        assert ecodes.BTN_LEFT in mouse.pending_release

    def test_click_counter_increments(self, debounce_module):
        """Total clicks should count all presses including suppressed bounces."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module, threshold_ms=60)

        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
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

        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
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
        """A flushed pending release must be followed by SYN_REPORT.

        Simulates: user clicks a button, doesn't move the mouse, waits
        for threshold to expire. The flush must emit both the release
        event AND a SYN_REPORT so uinput delivers it immediately.
        """
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module, threshold_ms=50)

        # Click: press then release (no movement)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
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

        If a release is pending and a re-press arrives (bounce), both are
        suppressed. No events should be written, including no SYN_REPORT.
        """
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module, threshold_ms=60)

        # Press
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        uinput.write_event.reset_mock()

        # Release (pending)
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))

        # Bounce re-press before threshold
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))

        # Nothing should have been forwarded
        uinput.write_event.assert_not_called()

    def test_multiple_buttons_flushed_get_single_syn(self, debounce_module):
        """If multiple button releases flush at the same time, one SYN_REPORT
        after all of them is sufficient (but one per release is also acceptable)."""
        from evdev import ecodes

        mouse, uinput = self._make_mouse(debounce_module, threshold_ms=50)

        # Press and release two buttons
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
        mouse.process_event(make_event(ecodes.EV_KEY, ecodes.BTN_RIGHT, 1))
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
