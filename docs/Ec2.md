# EC2

Failover instance `gv-failover`: t3.small, AL2023, 8 GB gp3. Kept stopped. No inbound ports, access through Session Manager.

Don't deploy during a failover: a newer AMI replaces the instance.

## Boot

`ec2/` is synced to `s3://<backup bucket>/ec2/` by `make deploy`.

1. first boot only: `setup.sh` installs docker, compose, cloudflared and the units
2. every start, `gv-boot.service` runs `boot.sh`: empties the DB, restores the newest dump from `gv-db/`, starts `compose.yaml`
3. `gv-tunnel.service` starts only after a successful restore

Failback: `failback.sh` (Run Command) stops the apps and uploads a dump to `gv-db/hourly/`, and to `daily/` if that day has none.

## Secrets

SecureStrings under `/home-lab-failover/ec2/`, created by hand:

```bash
make ec2-secrets NAME=api-env FILE=ec2/api.env  # from api.env.example
make ec2-secrets NAME=web-env FILE=ec2/web.env  # from web.env.example
make ec2-secrets NAME=tunnel-token              # prompts
```

## Cloudflare

Alt tunnel with the same ingress as home for `gv` and `gv-api`, set through the API so their DNS records stay on home. Failover flips both CNAMEs to `<alt tunnel id>.cfargotunnel.com`.

`gv-health.lab-ocp.com` always reaches home (`/health` only).
