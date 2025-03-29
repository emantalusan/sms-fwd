#!/bin/bash

# Script to install SMS Forwarder on Debian Buster

# Exit on any error
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

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (e.g., sudo $0)"
    exit 1
fi

# Update package list and install prerequisites
echo "Updating package list and installing prerequisites..."
apt-get update
apt-get install -y python3 python3-pip python3-venv sqlite3

# Create application directory
echo "Creating application directory: $APP_DIR"
mkdir -p "$APP_DIR"
chown "$USER:$GROUP" "$APP_DIR"
chmod 755 "$APP_DIR"

# Create virtual environment
echo "Creating virtual environment in $VENV_DIR..."
python3 -m venv "$VENV_DIR"
chown -R "$USER:$GROUP" "$VENV_DIR"

# Activate virtual environment and install dependencies
echo "Installing Python dependencies..."
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install python-gsmmodem-new
deactivate

# Copy app.py to APP_DIR (assuming it's in the current directory)
if [ -f "app.py" ]; then
    echo "Copying app.py to $APP_DIR..."
    cp "app.py" "$SCRIPT_FILE"
    chown "$USER:$GROUP" "$SCRIPT_FILE"
    chmod 644 "$SCRIPT_FILE"
else
    echo "Error: app.py not found in current directory. Please place it here and rerun."
    exit 1
fi

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
        "sender": "support@tnet.ph",
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

# Add user to dialout group for modem access
echo "Adding user $USER to dialout group..."
usermod -a -G dialout "$USER"

# Create systemd service file
echo "Creating systemd service file..."
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

# Reload systemd, enable, and start service
echo "Configuring systemd service..."
systemctl daemon-reload
systemctl enable "$SERVICE_NAME.service"
systemctl start "$SERVICE_NAME.service"

# Check service status
echo "Checking service status..."
systemctl status "$SERVICE_NAME.service"

echo "Installation complete!"
echo " - Edit $CONFIG_FILE to set your modem port, baud rate, and forwarding details."
echo " - Logs: journalctl -u $SERVICE_NAME.service"
echo " - Manage service: systemctl [start|stop|restart] $SERVICE_NAME.service"
