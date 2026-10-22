# home-lab-failover

SAM stack (`eu-south-2`) that keeps the home lab's `gv` services reachable when home goes down.

A Lambda (the watchdog) checks the home lab every 5 minutes. If it stays down, it starts a stopped EC2 instance, which restores the latest database backup from S3 and brings up an alternative Cloudflare tunnel, and then flips the DNS records to it. When home is healthy again, it hands the data back, flips the DNS home and stops the instance.

```
NORMAL -> STARTING -> FAILED_OVER -> FAILING_BACK -> PENDING_MERGE -> HOME_READY -> NORMAL
```

The full state machine is in [Lambdas](docs/Lambdas.md).

## What is deployed

- **Watchdog Lambda** (`watchdog/`): EventBridge-scheduled health check and state machine, with its state in SSM Parameter Store
- **EC2 instance** (`ec2/`): `gv-failover`, kept stopped until needed
- **S3 bucket**: `gv-api` database backups, plus the `ec2/` boot files
- **ECR repositories**: `gv-api` and `gv-web` images, with a deploy user
- **IAM** roles and users for each of the above

| Doc                            | What                                    |
|--------------------------------|-----------------------------------------|
| [Lambdas](docs/Lambdas.md)     | Watchdog health check every 5 minutes   |
| [Cloudflare](docs/Cloudflare.md) | Alt tunnel and the DNS flip              |
| [S3](docs/S3.md)               | gv-api backup bucket and uploader user  |
| [ECR](docs/Ecr.md)             | gv-api and gv-web images, deploy user   |
| [EC2](docs/Ec2.md)             | Failover instance                       |

## Layout

```
watchdog/    Lambda code (app.py handler, clients.py AWS/Cloudflare/home clients)
ec2/         boot, failback and setup scripts, systemd units, compose file
docs/        one document per component
tests/unit/  state machine tests
template.yaml  SAM template
```

## Requirements

AWS CLI and SAM CLI with credentials for `eu-south-2`, Docker (for `make local-run`), Python with `tests/requirements.txt` installed, and a `.env` filled from `.env.example`.

## Usage

```bash
make test       # unit tests
make local-run  # build and invoke the watchdog locally
make deploy     # sync ec2/ to S3, build and deploy (stack: home-lab-failover)
make ec2-sync   # sync ec2/ to S3 only
make ec2-secrets NAME=<name> [FILE=<path>]  # SecureString /home-lab-failover/ec2/<name>
make hooks      # run the tests before every commit
```

## Disclaimer

Parts of this project were written with the help of [Claude Code](https://claude.com/claude-code), an AI coding assistant. The code has been reviewed and tested by the author, but review it before running it against your own AWS account.
