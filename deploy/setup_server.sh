#!/usr/bin/env bash
# One-time setup for running the bot 24/7 on a fresh Ubuntu 22.04/24.04 server.
# Run as root from inside the uploaded bot folder:
#     cd /opt/crypto-trend-bot && bash deploy/setup_server.sh
# Safe to re-run (e.g. after uploading new code): it reinstalls packages and
# restarts the services, but never overwrites /etc/crypto-bot.env (it only
# adds settings that are missing, with empty/default values).
set -euo pipefail

APP_DIR=/opt/crypto-trend-bot
ENV_FILE=/etc/crypto-bot.env
SERVICES="data_collector anomaly_detector decision_agent execution_engine learning_agent"

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
# settings added after the first install: append the ones this server doesn't have yet
add_setting() { grep -q "^$1=" "$ENV_FILE" || echo "$1=$2" >> "$ENV_FILE"; }
grep -q "^# ---- Dashboard" "$ENV_FILE" || printf '\n# ---- Dashboard and learning agent (added by setup_server.sh) ----\n' >> "$ENV_FILE"
add_setting DASHBOARD_PORT 8787
add_setting DASHBOARD_TZ Asia/Karachi
add_setting GEMINI_API_KEY ""
add_setting GEMINI_MODEL gemini-2.5-flash
add_setting GEMINI_MAX_CALLS_PER_DAY 20
add_setting MARKET_REPORT_HOUR_UTC 3
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
# the learning agent is low priority: it must never slow down the trading layers
mkdir -p /etc/systemd/system/crypto-learning_agent.service.d
cat > /etc/systemd/system/crypto-learning_agent.service.d/limits.conf <<EOF
[Service]
Nice=10
MemoryMax=700M
EOF

# Dashboard: read-only web app. systemd makes the whole disk read-only for it
# (ProtectSystem=strict), so it cannot change the bot even if it had a bug.
# It listens on 127.0.0.1 only; Tailscale gives the HTTPS link (deploy/DASHBOARD.md).
cat > /etc/systemd/system/crypto-dashboard.service <<EOF
[Unit]
Description=Crypto trend bot - dashboard (read-only)
After=network-online.target

[Service]
User=bot
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
Environment=DASHBOARD_STATE_DIR=/var/lib/crypto-dashboard
Environment=DASHBOARD_HOST=127.0.0.1
StateDirectory=crypto-dashboard
ExecStart=$APP_DIR/.venv/bin/python -u dashboard/app.py
Restart=always
RestartSec=10
ProtectSystem=strict
ReadOnlyPaths=$APP_DIR
ProtectHome=yes
PrivateTmp=yes
NoNewPrivileges=yes
MemoryMax=250M
CPUQuota=50%

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

if grep -q "your_email@gmail.com\|your_testnet_api_key" "$ENV_FILE"; then
    echo
    echo "Setup done, but the bot is NOT started yet."
    echo "Next: nano $ENV_FILE   (fill in your email settings; exchange keys stay empty for DRY_RUN), then run:"
    echo "  sudo bash deploy/setup_server.sh"
    exit 0
fi

for svc in $SERVICES; do systemctl enable -q "crypto-$svc"; systemctl restart "crypto-$svc"; done

# dashboard: only start it if its port is free (or already ours) - never fight another app for a port
PORT=$(grep -E "^DASHBOARD_PORT=" "$ENV_FILE" | tail -1 | cut -d= -f2 | tr -d '"[:space:]')
PORT=${PORT:-8787}
systemctl stop crypto-dashboard 2>/dev/null || true
if ss -ltnH "sport = :$PORT" | grep -q .; then
    echo
    echo "!! Port $PORT is already used by another program - dashboard NOT started."
    echo "   Pick a free port: sudo nano $ENV_FILE  (DASHBOARD_PORT=8788), then re-run this script."
else
    systemctl enable -q crypto-dashboard
    systemctl restart crypto-dashboard
fi

sleep 3
systemctl --no-pager --lines=0 status $(printf "crypto-%s " $SERVICES) crypto-dashboard | grep -E "crypto-|Active:" || true
echo
grep -q "^DASHBOARD_PASSWORD_HASH=." "$ENV_FILE" || echo "Dashboard login not set yet: sudo $APP_DIR/.venv/bin/python $APP_DIR/dashboard/set_password.py"
grep -q "^GEMINI_API_KEY=." "$ENV_FILE" || echo "Learning agent is idle until you add GEMINI_API_KEY in $ENV_FILE (see deploy/DASHBOARD.md)."
echo "Running. Watch the execution engine with:  journalctl -u crypto-execution_engine -f"
