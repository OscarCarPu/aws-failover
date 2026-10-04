# home-lab-failover

SAM stack (`eu-south-2`) for the home lab:

- **Watchdog**: Lambda that checks gv-api's health endpoint every 5 minutes.
- **Backups**: S3 bucket for gv-api database dumps, plus an IAM user that can only upload new objects.

## Watchdog

`watchdog/app.py` runs on an EventBridge Scheduler rule (`rate(5 minutes)`). It sends a `GET` to `$HOME_LAB_API_URL/health` (default `https://gv-api.lab-ocp.com`) with a 5 s timeout. It logs one line to CloudWatch with a timestamp:

- `HOMELAB HEALTHY on <time>`: the endpoint returned 200.
- `HOMELAB UNHEALTHY on <time>`: the request failed or timed out.

It only logs. No alerts or failover actions are wired up yet.

## Usage

```bash
make test       # unit tests
make local-run  # build and invoke the watchdog locally
make deploy     # build and deploy (stack: home-lab-failover)
```

## Backups

### Retention

| Prefix          | Default | Parameter          |
|-----------------|---------|--------------------|
| `gv-db/hourly/` | 2 days  | `BackupHourlyDays` |
| `gv-db/daily/`  | 30 days | `BackupDailyDays`  |

The bucket has a `Retain` deletion policy. `sam delete` leaves the bucket and its backups in place, so delete it by hand if you need to.

### Access key for gv-api

CloudFormation doesn't create the key. Create it once after the first deploy:

```bash
out() { aws cloudformation describe-stacks --stack-name home-lab-failover --region eu-south-2 \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

aws iam create-access-key --user-name "$(out BackupUploaderName)"
out BackupBucketName
```

In gv-api's `.env`, put the bucket name in `BACKUP_S3_BUCKET` and the key pair in its AWS credential variables.
