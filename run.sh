#!/usr/bin/env bash
# Quick-launch mouse-filter with standard volume remap config.
# Usage: sudo ./run.sh
#
# Debounce is OFF by default. To re-enable for hardware with bouncing
# switches, add: --debounce --threshold 70
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/mouse-filter" \
    --remap BTN_EXTRA=KEY_VOLUMEUP \
    --remap BTN_SIDE=KEY_VOLUMEDOWN \
    --remap BTN_MIDDLE=KEY_MUTE \
    "$@"
