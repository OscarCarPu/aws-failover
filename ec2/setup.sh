#!/bin/bash
set -euxo pipefail

compose_version=v5.6.0
cloudflared_version=2026.10.0

dnf install -y docker
dnf install -y "https://github.com/cloudflare/cloudflared/releases/download/$cloudflared_version/cloudflared-linux-x86_64.rpm"
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
  "https://github.com/docker/compose/releases/download/$compose_version/docker-compose-linux-x86_64"
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
systemctl enable --now docker

cp /opt/gv/gv-boot.service /opt/gv/gv-tunnel.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable gv-boot.service
systemctl start --no-block gv-boot.service
