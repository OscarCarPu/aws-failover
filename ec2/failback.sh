#!/bin/bash
set -euo pipefail
set -a
. /etc/gv.env
set +a
cd /opt/gv

docker compose stop gv-web gv-api
docker compose run --rm --no-deps gv-api ./main backup

name=$(ls backups/hourly | grep -E '^gv-db-[0-9]{8}T[0-9]{6}Z\.sql\.gz$' | sort | tail -n 1)
put() {
  aws s3api put-object --bucket "$BUCKET" --key "gv-db/$1/$name" --body "backups/hourly/$name" \
    --if-none-match '*' > /dev/null
}

put hourly
day=${name:6:8}
if [ "$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "gv-db/daily/gv-db-$day" --query KeyCount --output text)" = 0 ]; then
  put daily
fi
echo "gv-db/hourly/$name"
