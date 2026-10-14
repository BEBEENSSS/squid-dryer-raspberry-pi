#!/usr/bin/env bash
# Installs (or updates) the squid dryer services on the Pi. Safe to run again.
#   sudo bash deploy/install.sh
set -euo pipefail

APP=/home/bensdev/squid-dryer
cd "$APP"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo." >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  sudo -u bensdev python3 -m venv .venv
fi
sudo -u bensdev .venv/bin/pip install --quiet -r requirements.txt

install -m 644 deploy/squid-dryer.service /etc/systemd/system/squid-dryer.service
install -m 644 deploy/squid-netwatch.service /etc/systemd/system/squid-netwatch.service
systemctl daemon-reload

# the hotspot profile and its password (asks only when the password file does not exist yet)
.venv/bin/python netwatch.py --setup

systemctl enable --now squid-netwatch.service
systemctl enable squid-dryer.service
systemctl restart squid-dryer.service

echo
echo "Done. Next:"
if [ ! -f "$APP/secrets/local_pin" ]; then
  echo "  - set the panel PIN:  sudo -u bensdev .venv/bin/python local_api.py --set-pin"
fi
echo "  - watch the network:   journalctl -u squid-netwatch -f"
echo "  - copy the web build:  npm run deploy:pi   (on the computer that has the web app)"
