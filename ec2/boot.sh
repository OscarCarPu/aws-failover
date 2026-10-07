#!/bin/bash
set -euo pipefail

cd /opt/gv

get() {
  aws ssm get-parameter --with-decryption --name "/home-lab-failover/ec2/$1" \
    --query Parameter.Value --output text
}

(umask 077; get api-env > api.env; get web-env > web.env)

rm -rf backups
mkdir -p backups/hourly backups/daily
chown -R 1000:1000 backups

aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY"

docker compose down --volumes --remove-orphans
docker compose pull --quiet
docker image prune --force
docker compose up --detach --wait db

keys=$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix gv-db/ --query 'Contents[].Key' --output text)
key=$(tr '\t' '\n' <<<"$keys" | { grep -E '/gv-db-[0-9]{8}T[0-9]{6}Z\.sql\.gz$' || true; } \
  | awk -F/ '{print $NF, $0}' | sort | tail -n 1 | cut -d' ' -f2)
if [ -z "$key" ]; then
  echo "no dump in s3://$BUCKET/gv-db/" >&2
  exit 3
fi

echo "restoring $key"
aws s3 cp "s3://$BUCKET/$key" - | gunzip \
  | docker compose exec -T db sh -c 'psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

docker compose up --detach --wait --wait-timeout 180
systemctl start --no-block gv-tunnel.service
