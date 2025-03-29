#!/bin/bash

# Script to install SMS Forwarder from GitHub on Debian Buster

# Exit on any error after critical steps
set -e

# Variables
APP_DIR="/opt/sms_forwarder"
VENV_DIR="$APP_DIR/venv"
USER="freebsd"  # Adjust to your username
GROUP="freebsd"  # Adjust to your group
SERVICE_NAME="sms-forwarder"
CONFIG_FILE="$APP_DIR/config.json"
DB_FILE="$APP_DIR/sms_database.db"
SCRIPT_FILE="$APP_DIR/app.py"
REPO_URL="https://github.com/emantalusan/sms-fwd.git"
LOG_FILE="/var/log/sms_forwarder_install.log"

# Redirect output to log file
exec > >(tee -a "$LOG_FILE") 2>&1

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (e.g., sudo $0)"
    exit 1
fi

echo "Starting installation at $(date)"

# Update package list and install prerequisites
echo "Updating package list and installing prerequisites..."
apt-get update || { echo "Failed to update package list"; exit 1; }
apt-get install -y python3 python3-pip python3-venv sqlite3 git || { echo "Failed to install packages"; exit 1; }

# Ensure passwd package is installed for usermod
if ! command -v usermod >/dev/null 2>&1; then
    echo "usermod not found, installing passwd package..."
    apt-get install -y passwd || { echo "Failed to install passwd package"; exit 1; }
fi

# Create application directory
echo "Creating application directory: $APP_DIR"
mkdir -p "$APP_DIR" || { echo "Failed to create $APP_DIR"; exit 1; }
chown "$USER:$GROUP" "$APP_DIR"
chmod 755 "$APP_DIR"

# Clone the latest code from GitHub
echo "Cloning latest code from $REPO_URL..."
if [ -d "$APP_DIR/.git" ]; then
    echo "Repository already exists, pulling latest changes..."
    cd "$APP_DIR"
    git pull origin main || { echo "Failed to pull from GitHub"; exit 1; }
else
    git clone "$REPO_URL" "$APP_DIR" || { echo "Failed to clone from GitHub"; exit 1; }
fi
chown -R "$USER:$GROUP" "$APP_DIR"
chmod -R 755 "$APP_DIR"

# Verify app.py exists
if [ ! -f "$SCRIPT_FILE" ]; then
    echo "Error: app.py not found in $APP_DIR after cloning. Check the repository."
    exit 1
fi

# Create virtual environment
echo "Creating virtual environment in $VENV_DIR..."
python3 -m venv "$VENV_DIR" || { echo "Failed to create virtual environment"; exit 1; }
chown -R "$USER:$GROUP" "$VENV_DIR"

# Activate virtual environment and install dependencies
echo "Installing Python dependencies..."
source "$VENV_DIR/bin/activate"
pip install --upgrade pip || { echo "Failed to upgrade pip"; exit 1; }
pip install python-gsmmodem-new || { echo "Failed to install python-gsmmodem-new"; exit 1; }
deactivate

# Create default config.json if not provided
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Creating default config.json..."
    cat << EOF > "$CONFIG_FILE"
{
    "modem": {
        "port": "/dev/ttyUSB0",
        "baudrate": 115200,
        "pin": null
    },
    "sms_recipients": [],
    "email": {
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_user": "your_email@gmail.com",
        "smtp_password": "your_password",
        "sender": "custom_sender@example.com",
        "recipients": ["admin@tnet.ph"]
    },
    "database": {
        "file": "$DB_FILE"
    }
}
EOF
    chown "$USER:$GROUP" "$CONFIG_FILE"
    chmod 644 "$CONFIG_FILE"
    echo "Default config.json created. Please edit $CONFIG_FILE with your settings."
else
    echo "Config file already exists at $CONFIG_FILE. Skipping creation."
fi

# Add user to dialout group for modem access (non-critical, continue on failure)
echo "Adding user $USER to dialout group..."
if usermod -a -G dialout "$USER"; then
    echo "Successfully added $USER to dialout group."
else
    echo "Warning: Failed to add user to dialout group. Modem access might be restricted."
fi

# Create systemd service file
echo "Creating systemd service file at /etc/systemd/system/$SERVICE_NAME.service..."
cat << EOF > "/etc/systemd/system/$SERVICE_NAME.service"
[Unit]
Description=SMS Forwarding Service
After=network.target

[Service]
ExecStart=$VENV_DIR/bin/python3 $SCRIPT_FILE
WorkingDirectory=$APP_DIR
Restart=always
User=$USER
Group=$GROUP
Environment="PYTHONPATH=$APP_DIR"

[Install]
WantedBy=multi-user.target
EOF
if [ ! -f "/etc/systemd/system/$SERVICE_NAME.service" ]; then
    echo "Error: Failed to create service file"
    exit 1
fi

# Reload systemd, enable, and start service
echo "Configuring systemd service..."
systemctl daemon-reload || { echo "Failed to reload systemd"; exit 1; }
systemctl enable "$SERVICE_NAME.service" || { echo "Failed to enable service"; exit 1; }
systemctl start "$SERVICE_NAME.service" || { echo "Failed to start service"; exit 1; }

# Check service status
echo "Checking service status..."
systemctl status "$SERVICE_NAME.service"

echo "Installation complete at $(date)!"
echo " - Latest code fetched from $REPO_URL and installed in $APP_DIR."
echo " - Edit $CONFIG_FILE to set your modem port, baud rate, and forwarding details."
echo " - Logs: journalctl -u $SERVICE_NAME.service"
echo " - Manage service: systemctl [start|stop|restart] $SERVICE_NAME.service"
echo " - Installation log: $LOG_FILE"
