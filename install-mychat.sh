#!/bin/bash
# MyChat - automatic setup for Ubuntu / Debian
# Start with:  sudo bash setup-server.sh
set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "Please start with: sudo bash setup-server.sh"
  exit 1
fi
if ! command -v apt-get >/dev/null; then
  echo "This script only works on Ubuntu / Debian."
  exit 1
fi

SRC="$(cd "$(dirname "$0")" && pwd)"
APP=/opt/mychat
PORT=8088   # CasaOS already uses port 80

# Stop an older MyChat first, so its ports count as free
systemctl stop mychat 2>/dev/null || true
if ss -ltn | grep -q ":8765 "; then
  echo "Port 8765 is already used by another program."
  exit 1
fi
if ss -ltn | grep -q ":$PORT " && ! grep -qs "listen $PORT" /etc/nginx/sites-available/mychat; then
  echo "Port $PORT is already used by another program. Change PORT in this script."
  exit 1
fi

echo "===== 1/5 Installing programs ====="
# Don't let nginx start during install (port 80 belongs to CasaOS)
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod +x /usr/sbin/policy-rc.d
trap 'rm -f /usr/sbin/policy-rc.d' EXIT
dpkg --configure -a || true
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv nginx unattended-upgrades
rm -f /usr/sbin/policy-rc.d
# Remove nginx's default page (it would also try to use port 80)
rm -f /etc/nginx/sites-enabled/default

echo "===== 2/5 Copying the app ====="
id mychat >/dev/null 2>&1 || adduser --system --group --home "$APP" mychat
mkdir -p "$APP"
cp "$SRC/server.py" "$APP/"
rm -rf "$APP/public"
cp -r "$SRC/public" "$APP/"

echo "===== 3/5 Python packages ====="
[ -d "$APP/venv" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q flask flask-socketio simple-websocket gunicorn requests pillow
chown -R mychat:mychat "$APP"

echo "===== 4/5 Secret keys ====="
if [ ! -f /etc/mychat.env ]; then
  echo "SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" > /etc/mychat.env
  read -r -p "Paste your VirusTotal API key (or just press Enter to skip): " VT
  echo "VT_API_KEY=$VT" >> /etc/mychat.env
  chmod 600 /etc/mychat.env
else
  echo "/etc/mychat.env already exists - keeping it."
fi

echo "===== 5/5 Service and nginx ====="
cat > /etc/systemd/system/mychat.service <<'EOF'
[Unit]
Description=MyChat
After=network.target

[Service]
User=mychat
Group=mychat
WorkingDirectory=/opt/mychat
EnvironmentFile=/etc/mychat.env
ExecStart=/opt/mychat/venv/bin/gunicorn -w 1 --threads 100 --timeout 300 --bind 127.0.0.1:8765 server:app
Restart=always
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/mychat

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/nginx/sites-available/mychat <<'EOF'
# Requests from Tailscale Funnel come from 127.0.0.1 and are HTTPS
map $remote_addr $mychat_default_proto {
    127.0.0.1 https;
    default   $scheme;
}
map $http_x_forwarded_proto $mychat_proto {
    ""      $mychat_default_proto;
    default $http_x_forwarded_proto;
}
map $http_x_forwarded_for $mychat_for {
    ""      $remote_addr;
    default $http_x_forwarded_for;
}

server {
    listen 8088;
    server_name _;

    client_max_body_size 11M;
    server_tokens off;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-For $mychat_for;
        proxy_set_header X-Forwarded-Proto $mychat_proto;
        proxy_read_timeout 300s;
    }
}
EOF
ln -sf /etc/nginx/sites-available/mychat /etc/nginx/sites-enabled/mychat
nginx -t

systemctl daemon-reload
systemctl enable mychat
systemctl restart mychat
systemctl enable nginx
systemctl restart nginx
sleep 3

if systemctl is-active --quiet mychat; then
  IP=$(hostname -I | awk '{print $1}')
  echo ""
  echo "======================================"
  echo " MyChat is running:  http://$IP:$PORT"
  echo "======================================"
else
  echo "MyChat did not start. Last log lines:"
  journalctl -u mychat -n 30 --no-pager
  exit 1
fi
