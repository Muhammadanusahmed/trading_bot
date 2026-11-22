#!/usr/bin/env bash
# One-time setup for running the bot 24/7 on a fresh Ubuntu 22.04/24.04 server.
# Run as root from inside the uploaded bot folder:
#     cd /opt/crypto-trend-bot && bash deploy/setup_server.sh
# Safe to re-run (e.g. after uploading new code): it reinstalls packages and
# restarts the services, but never overwrites /etc/crypto-bot.env.
set -euo pipefail

APP_DIR=/opt/crypto-trend-bot
ENV_FILE=/etc/crypto-bot.env
SERVICES="data_collector anomaly_detector decision_agent execution_engine"

if [ "$(id -u)" -ne 0 ]; then echo "Run as root (or with sudo)."; exit 1; fi
if [ ! -f "$APP_DIR/execution_engine.py" ]; then
    echo "Upload the bot folder to $APP_DIR first (see deploy/README.md)."; exit 1
fi

echo "== Installing Python =="
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv >/dev/null
timedatectl set-ntp true 2>/dev/null || true   # Binance rejects requests if the clock drifts

echo "== Creating the 'bot' user and Python environment =="
id bot >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin bot
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
mkdir -p "$APP_DIR/data"
chown -R bot:bot "$APP_DIR"

if [ ! -f "$ENV_FILE" ]; then
    echo "== Creating $ENV_FILE from the template - you must fill in your keys =="
    cp "$APP_DIR/deploy/crypto-bot.env.example" "$ENV_FILE"
fi
chown root:bot "$ENV_FILE"
chmod 640 "$ENV_FILE"   # keys readable by the bot only, not other users

echo "== Installing services =="
for svc in $SERVICES; do
    cat > "/etc/systemd/system/crypto-$svc.service" <<EOF
[Unit]
Description=Crypto trend bot - $svc
After=network-online.target
Wants=network-online.target

[Service]
User=bot
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python -u $svc.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
done
systemctl daemon-reload

if grep -q "your_email@gmail.com\|your_testnet_api_key" "$ENV_FILE"; then
    echo
    echo "Setup done, but the bot is NOT started yet."
    echo "Next: nano $ENV_FILE   (fill in your email settings; exchange keys stay empty for DRY_RUN), then run:"
    echo "  systemctl enable --now crypto-data_collector crypto-anomaly_detector crypto-decision_agent crypto-execution_engine"
    exit 0
fi

for svc in $SERVICES; do systemctl enable -q "crypto-$svc"; systemctl restart "crypto-$svc"; done
sleep 3
systemctl --no-pager --lines=0 status $(printf "crypto-%s " $SERVICES) | grep -E "crypto-|Active:"
echo
echo "Running. Watch the execution engine with:  journalctl -u crypto-execution_engine -f"
