#!/usr/bin/env bash
# Quick-launch mouse-filter with standard volume remap config.
# Usage: sudo ./run.sh
#
# Debounce: ON at 70ms — older MX Anywhere 2S unit shows wear-driven
# Omron left-click bounce. Drop --debounce to disable.
#
# Wheel: stock — no multiplier, no suppress, no drop-hires.
# --diagnose-wheel preserved for read-only WHEEL_REV / WHEEL_BURST_END
# logging while evaluating a replacement MX Anywhere 2S unit.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/mouse-filter" \
    --debounce --threshold 70 \
    --diagnose-wheel \
    --remap BTN_EXTRA=KEY_VOLUMEUP \
    --remap BTN_SIDE=KEY_VOLUMEDOWN \
    --remap BTN_MIDDLE=KEY_MUTE \
    "$@"
