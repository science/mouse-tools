#!/usr/bin/env bash
set -euo pipefail

INSTALL_BIN="/usr/local/bin/mouse-debounce"
SERVICE_FILE="/etc/systemd/system/mouse-debounce.service"
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

do_install() {
    check_root

    echo "Installing mouse-debounce..."

    # Install binary
    cp "$SCRIPT_DIR/mouse-debounce" "$INSTALL_BIN"
    chmod 755 "$INSTALL_BIN"
    echo "  Installed $INSTALL_BIN"

    # Install systemd service
    cat > "$SERVICE_FILE" <<'UNIT'
[Unit]
Description=Mouse button debounce filter (fixes hardware switch bounce)
After=multi-user.target

[Service]
Type=simple
ExecStart=/usr/local/bin/mouse-debounce --quiet
Restart=on-failure
RestartSec=3

# Harden the service
ProtectHome=read-only
ProtectSystem=strict
ReadWritePaths=/home
PrivateTmp=true
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
UNIT
    echo "  Installed $SERVICE_FILE"

    systemctl daemon-reload
    systemctl enable mouse-debounce.service
    systemctl start mouse-debounce.service
    echo "  Service enabled and started"

    echo ""
    echo "Done. Check status: systemctl status mouse-debounce.service"
}

do_uninstall() {
    check_root

    echo "Uninstalling mouse-debounce..."

    if systemctl is-active --quiet mouse-debounce.service 2>/dev/null; then
        systemctl stop mouse-debounce.service
        echo "  Stopped service"
    fi

    if systemctl is-enabled --quiet mouse-debounce.service 2>/dev/null; then
        systemctl disable mouse-debounce.service
        echo "  Disabled service"
    fi

    if [[ -f "$SERVICE_FILE" ]]; then
        rm "$SERVICE_FILE"
        systemctl daemon-reload
        echo "  Removed $SERVICE_FILE"
    fi

    if [[ -f "$INSTALL_BIN" ]]; then
        rm "$INSTALL_BIN"
        echo "  Removed $INSTALL_BIN"
    fi

    echo ""
    echo "Done. Log files remain at ~/.local/share/mouse-debounce/ (remove manually if desired)."
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
