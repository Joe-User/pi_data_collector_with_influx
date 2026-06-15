#!/usr/bin/env bash
# pi-collector setup script
# Run once from the cloned repo directory on the Pi.
# Re-running is safe — it will update in place.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/pi-collector"
CONFIG_DIR="/etc/pi-collector"
CONFIG_FILE="$CONFIG_DIR/config.toml"
SERVICE_NAME="pi-collector"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
OLD_SERVICE="temperature"
VENV="$INSTALL_DIR/venv"
PYTHON="python3"

echo "==> pi-collector setup starting"
echo "    Repo:    $REPO_DIR"
echo "    Install: $INSTALL_DIR"
echo "    Config:  $CONFIG_FILE"
echo ""

# ---------------------------------------------------------------------------
# 1. Verify 1-Wire kernel overlay is active
# ---------------------------------------------------------------------------
echo "==> Checking 1-Wire bus..."
if ls /sys/bus/w1/devices/28-* &>/dev/null; then
    SENSOR_COUNT=$(ls -d /sys/bus/w1/devices/28-* 2>/dev/null | wc -l)
    echo "    Found $SENSOR_COUNT sensor(s)"
else
    echo "    WARNING: No 28-* devices found under /sys/bus/w1/devices/"
    echo "    Make sure 'dtoverlay=w1-gpio' is in /boot/firmware/config.txt"
    echo "    and the Pi has been rebooted."
fi

# ---------------------------------------------------------------------------
# 2. Create install and config directories (owned by current user)
# ---------------------------------------------------------------------------
echo "==> Creating directories..."
sudo mkdir -p "$INSTALL_DIR" "$CONFIG_DIR"
sudo chown "$(whoami):$(whoami)" "$INSTALL_DIR" "$CONFIG_DIR"

# ---------------------------------------------------------------------------
# 3. Copy application files
# ---------------------------------------------------------------------------
echo "==> Installing application files..."
cp "$REPO_DIR/collector.py" "$INSTALL_DIR/"
cp "$REPO_DIR/tui.py"       "$INSTALL_DIR/"
cp "$REPO_DIR/requirements.txt" "$INSTALL_DIR/"

# ---------------------------------------------------------------------------
# 4. Create or update virtualenv
# ---------------------------------------------------------------------------
echo "==> Setting up Python virtual environment..."
if [ ! -d "$VENV" ]; then
    $PYTHON -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
echo "    Done"

# ---------------------------------------------------------------------------
# 5. Generate initial config (skip if one already exists)
# ---------------------------------------------------------------------------
if [ ! -f "$CONFIG_FILE" ]; then
    echo "==> Generating initial config at $CONFIG_FILE..."

    # Pull local InfluxDB token from the existing daemon if it exists
    EXISTING_TOKEN=""
    if [ -f /home/jsnaza/temperature_daemon.py ]; then
        EXISTING_TOKEN=$(grep -oP 'token\s*=\s*"\K[^"]+' /home/jsnaza/temperature_daemon.py | head -1 || true)
    fi
    if [ -z "$EXISTING_TOKEN" ]; then
        EXISTING_TOKEN="REPLACE_WITH_YOUR_LOCAL_TOKEN"
    fi

    HOSTNAME_TAG=$(hostname)

    # Build sensor entries from currently detected devices
    SENSOR_ENTRIES=""
    for dev in /sys/bus/w1/devices/28-*; do
        sensor_id=$(basename "$dev")
        SENSOR_ENTRIES="${SENSOR_ENTRIES}
[sensors.\"${sensor_id}\"]
name = \"${sensor_id}\""
    done

    cat > "$CONFIG_FILE" <<TOML
# pi-collector configuration — $(hostname) — generated $(date -Iseconds)
# Edit sensor labels and InfluxDB settings via the TUI (Sensors / Settings tabs),
# or edit this file directly. The collector reloads it each cycle automatically.

[collector]
hostname          = "$HOSTNAME_TAG"
interval_seconds  = 15

# Sensor labels — auto-discovered sensors listed below.
# Change 'name' values to human-readable location names.
[sensors]
$SENSOR_ENTRIES

[influx.local]
url    = "http://localhost:8086"
token  = "$EXISTING_TOKEN"
org    = "mi8"
bucket = "${HOSTNAME_TAG}_telem"

# Uncomment and fill in to enable remote replication:
# [influx.remote]
# url    = "https://influx.example.com"
# token  = ""
# org    = ""
# bucket = ""
TOML
    echo "    Config written. Review and update the token if needed."
else
    echo "==> Config already exists at $CONFIG_FILE — leaving it untouched."
fi

# ---------------------------------------------------------------------------
# 6. Install TUI launcher in PATH
# ---------------------------------------------------------------------------
echo "==> Installing 'pi-collector-tui' launcher..."
sudo tee /usr/local/bin/pi-collector-tui > /dev/null <<SCRIPT
#!/usr/bin/env bash
exec "$VENV/bin/python" "$INSTALL_DIR/tui.py" "\$@"
SCRIPT
sudo chmod +x /usr/local/bin/pi-collector-tui

# ---------------------------------------------------------------------------
# 7. Add sudoers rule for service control without password
# ---------------------------------------------------------------------------
echo "==> Configuring passwordless sudo for service control..."
SUDOERS_FILE="/etc/sudoers.d/pi-collector"
sudo tee "$SUDOERS_FILE" > /dev/null <<SUDOERS
# Allows the pi-collector TUI to start/stop/restart the service without a password
$(whoami) ALL=(ALL) NOPASSWD: /bin/systemctl start ${SERVICE_NAME}, /bin/systemctl stop ${SERVICE_NAME}, /bin/systemctl restart ${SERVICE_NAME}
SUDOERS
sudo chmod 0440 "$SUDOERS_FILE"

# ---------------------------------------------------------------------------
# 8. Stop and disable old temperature.service if it exists
# ---------------------------------------------------------------------------
if systemctl list-units --all | grep -q "^  ${OLD_SERVICE}.service"; then
    echo "==> Stopping old ${OLD_SERVICE}.service..."
    sudo systemctl stop  "$OLD_SERVICE" 2>/dev/null || true
    sudo systemctl disable "$OLD_SERVICE" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 9. Install and enable pi-collector.service
# ---------------------------------------------------------------------------
echo "==> Installing systemd service..."
sudo cp "$REPO_DIR/pi-collector.service" "$SERVICE_FILE"
sudo sed -i "s|User=jsnaza|User=$(whoami)|g" "$SERVICE_FILE"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "==> Setup complete!"
echo ""
echo "    Service status:  systemctl status pi-collector"
echo "    Live logs:       journalctl -u pi-collector -f"
echo "    TUI:             pi-collector-tui"
echo "    Config:          $CONFIG_FILE"
echo ""
echo "    To update after a git pull:"
echo "      cd $REPO_DIR && git pull && bash setup.sh"
echo ""
