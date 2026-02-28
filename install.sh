#!/usr/bin/env bash
set -euo pipefail

INSTALL_BIN="/usr/local/bin/mouse-filter"
INSTALL_MONITOR="/usr/local/bin/mouse-drag-monitor"
SERVICE_FILE="/etc/systemd/system/mouse-filter.service"
LOG_DIR="/var/log/mouse-filter"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    echo "Usage: sudo $0 [--uninstall]"
    echo ""
    echo "  Install:    sudo $0"
    echo "  Uninstall:  sudo $0 --uninstall"
}

check_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "Error: must run as root (sudo $0)"
        exit 1
    fi
}

check_deps() {
    if ! python3 -c "import evdev" 2>/dev/null; then
        echo "Error: python3-evdev is required."
        echo "  Install: sudo apt install python3-evdev"
        exit 1
    fi
}

do_install() {
    check_root
    check_deps

    # Clean up legacy mouse-debounce installation if present
    if systemctl is-active --quiet mouse-debounce.service 2>/dev/null; then
        systemctl stop mouse-debounce.service
    fi
    if systemctl is-enabled --quiet mouse-debounce.service 2>/dev/null; then
        systemctl disable mouse-debounce.service
    fi
    if [[ -f /etc/systemd/system/mouse-debounce.service ]]; then
        rm /etc/systemd/system/mouse-debounce.service
        systemctl daemon-reload
        echo "  Removed legacy mouse-debounce.service"
    fi
    [[ -f /usr/local/bin/mouse-debounce ]] && rm /usr/local/bin/mouse-debounce
    [[ -f /etc/logrotate.d/mouse-debounce ]] && rm /etc/logrotate.d/mouse-debounce

    # Clean up legacy logrotate config (log capping is now built into mouse-filter)
    [[ -f /etc/logrotate.d/mouse-filter ]] && rm /etc/logrotate.d/mouse-filter

    echo "Installing mouse-filter..."

    # Install binaries
    cp "$SCRIPT_DIR/mouse-filter" "$INSTALL_BIN"
    chmod 755 "$INSTALL_BIN"
    echo "  Installed $INSTALL_BIN"

    if [[ -f "$SCRIPT_DIR/mouse-drag-monitor" ]]; then
        cp "$SCRIPT_DIR/mouse-drag-monitor" "$INSTALL_MONITOR"
        chmod 755 "$INSTALL_MONITOR"
        echo "  Installed $INSTALL_MONITOR"
    fi

    # Create log directory
    mkdir -p "$LOG_DIR"
    echo "  Created $LOG_DIR"

    # Install systemd service
    cat > "$SERVICE_FILE" <<UNIT
[Unit]
Description=Mouse tools: debounce filter + button remapping
After=multi-user.target

[Service]
Type=simple
ExecStart=$INSTALL_BIN --quiet --threshold 70 --log-dir $LOG_DIR --remap BTN_EXTRA=KEY_VOLUMEUP --remap BTN_SIDE=KEY_VOLUMEDOWN --remap BTN_MIDDLE=KEY_MUTE
Restart=on-failure
RestartSec=3

# Harden the service
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$LOG_DIR
# Note: DeviceAllow glob patterns don't work reliably with cgroup v2.
# The service runs as root with default DevicePolicy=auto, which permits
# /dev/input/* and /dev/uinput access. The other directives (ProtectHome,
# ProtectSystem) provide meaningful containment.

[Install]
WantedBy=multi-user.target
UNIT
    echo "  Installed $SERVICE_FILE"

    systemctl daemon-reload
    systemctl enable mouse-filter.service
    systemctl start mouse-filter.service
    echo "  Service enabled and started"

    echo ""
    echo "Done."
    echo "  Status:  systemctl status mouse-filter.service"
    echo "  Logs:    journalctl -u mouse-filter.service -f"
    echo "  Logfile: $LOG_DIR/debounce.log"
}

do_uninstall() {
    check_root

    echo "Uninstalling mouse-filter..."

    if systemctl is-active --quiet mouse-filter.service 2>/dev/null; then
        systemctl stop mouse-filter.service
        echo "  Stopped service"
    fi

    if systemctl is-enabled --quiet mouse-filter.service 2>/dev/null; then
        systemctl disable mouse-filter.service
        echo "  Disabled service"
    fi

    if [[ -f "$SERVICE_FILE" ]]; then
        rm "$SERVICE_FILE"
        systemctl daemon-reload
        echo "  Removed $SERVICE_FILE"
    fi

    [[ -f /etc/logrotate.d/mouse-filter ]] && rm /etc/logrotate.d/mouse-filter

    if [[ -f "$INSTALL_BIN" ]]; then
        rm "$INSTALL_BIN"
        echo "  Removed $INSTALL_BIN"
    fi

    if [[ -f "$INSTALL_MONITOR" ]]; then
        rm "$INSTALL_MONITOR"
        echo "  Removed $INSTALL_MONITOR"
    fi

    echo ""
    echo "Done. Logs remain at $LOG_DIR (remove manually if desired)."
}

case "${1:-}" in
    --uninstall)
        do_uninstall
        ;;
    --help|-h)
        usage
        ;;
    "")
        do_install
        ;;
    *)
        echo "Unknown option: $1"
        usage
        exit 1
        ;;
esac
