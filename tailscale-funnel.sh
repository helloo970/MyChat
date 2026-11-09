#!/bin/bash
# MyChat - make it reachable from the internet with Tailscale Funnel
# Start with:  sudo bash tailscale-funnel.sh
set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "Please start with: sudo bash tailscale-funnel.sh"
  exit 1
fi
if ! command -v tailscale >/dev/null; then
  echo "Tailscale is not installed."
  exit 1
fi
if ! tailscale status >/dev/null 2>&1; then
  echo "Tailscale is not logged in. Run:  sudo tailscale up"
  echo "Then start this script again."
  exit 1
fi

echo "===== 1/2 Updating nginx for HTTPS ====="
cat > /etc/nginx/sites-available/mychat <<'CONF'
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
CONF
nginx -t
systemctl reload nginx

echo "===== 2/2 Starting Tailscale Funnel ====="
echo "If a link appears: open it in your browser, allow Funnel, then come back here."
tailscale funnel --bg --https=443 8088
echo ""
tailscale funnel status
