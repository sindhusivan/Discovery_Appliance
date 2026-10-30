#!/bin/bash
# TAO Discovery Appliance — one-shot installer for Photon OS 5.0
#
# Replaces deployment guide Phases 3-7 (dependencies, directory layout, file
# copy, DB init, systemd) and applies the Photon 5 fixes from the README
# (firewall rules for 8443/4739 persisted across reboot).
#
# Usage (as root, from the extracted bundle directory):
#   tdnf install -y tar   # not in Photon minimal
#   tar xzf discovery-appliance.tar.gz && cd discovery-appliance && bash deploy/install.sh
#
# Safe to re-run: upgrades code in place, keeps data/, logs/, reports/ and the
# existing env file (so the admin password does not change).

set -euo pipefail

APP_DIR=/opt/discovery-appliance
ENV_FILE=/etc/discovery-appliance.env
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Run as root"; exit 1; }

echo "==> Installing OS packages"
tdnf install -y python3 python3-pip sqlite iptables tar >/dev/null

echo "==> Copying application to $APP_DIR"
mkdir -p "$APP_DIR"/{web/templates,engine,collector,scripts,data,logs,reports}
cp -r "$SRC_DIR"/web/app.py "$APP_DIR"/web/
cp -r "$SRC_DIR"/web/templates/. "$APP_DIR"/web/templates/
cp -r "$SRC_DIR"/engine/*.py "$APP_DIR"/engine/
cp -r "$SRC_DIR"/collector/*.py "$APP_DIR"/collector/
cp -r "$SRC_DIR"/scripts/*.py "$APP_DIR"/scripts/ 2>/dev/null || true
cp "$SRC_DIR"/requirements.txt "$APP_DIR"/
chmod -R 750 "$APP_DIR"

echo "==> Creating Python virtualenv"
[ -x "$APP_DIR/venv/bin/python3" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [ ! -f "$ENV_FILE" ]; then
    echo "==> Generating secrets in $ENV_FILE"
    ADMIN_PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
    cat > "$ENV_FILE" <<EOF
DISCOVERY_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
DISCOVERY_ADMIN_USER=admin
DISCOVERY_ADMIN_PASSWORD=$ADMIN_PW
DISCOVERY_DB_PATH=$APP_DIR/data/discovery.db
DISCOVERY_REPORTS_DIR=$APP_DIR/reports
DISCOVERY_LOG_PATH=$APP_DIR/logs/ipfix-listener.log
EOF
    chmod 600 "$ENV_FILE"
    NEW_PW=1
fi

echo "==> Initialising database"
set -a; . "$ENV_FILE"; set +a
"$APP_DIR/venv/bin/python3" "$APP_DIR/engine/db_init.py"

echo "==> Installing systemd services"
cat > /etc/systemd/system/discovery-web.service <<EOF
[Unit]
Description=TAO Discovery Appliance Web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
WorkingDirectory=$APP_DIR/web
ExecStart=$APP_DIR/venv/bin/python3 $APP_DIR/web/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/ipfix-listener.service <<EOF
[Unit]
Description=TAO Discovery IPFIX Flow Listener
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/venv/bin/python3 $APP_DIR/collector/ipfix_listener.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable discovery-web ipfix-listener >/dev/null 2>&1
systemctl restart discovery-web ipfix-listener

echo "==> Opening firewall (TCP 8443 web UI, UDP 4739 IPFIX)"
iptables -C INPUT -p tcp --dport 8443 -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport 8443 -j ACCEPT
iptables -C INPUT -p udp --dport 4739 -j ACCEPT 2>/dev/null || iptables -I INPUT -p udp --dport 4739 -j ACCEPT
iptables-save > /etc/systemd/scripts/ip4save   # Photon restores this on boot

echo "==> Health check"
sleep 3
systemctl is-active discovery-web ipfix-listener
curl -s -o /dev/null -w "Web UI HTTP: %{http_code}\n" http://localhost:8443/login || true

IP="$(ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1 | head -1)"
echo
echo "Done. Web UI: http://$IP:8443   (login: admin)"
if [ "${NEW_PW:-0}" = 1 ]; then
    echo "Admin password (shown once, stored in $ENV_FILE): $ADMIN_PW"
fi
