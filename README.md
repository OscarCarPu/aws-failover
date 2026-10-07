# home-lab-failover

SAM stack (`eu-south-2`) for the home lab.

| Doc                            | What                                    |
|--------------------------------|-----------------------------------------|
| [Lambdas](docs/Lambdas.md)     | Watchdog health check every 5 minutes   |
| [Cloudflare](docs/Cloudflare.md) | Alt tunnel and the DNS flip              |
| [S3](docs/S3.md)               | gv-api backup bucket and uploader user  |
| [ECR](docs/Ecr.md)             | gv-api and gv-web images, deploy user   |
| [EC2](docs/Ec2.md)             | Failover instance                       |

## Usage

```bash
make test       # unit tests
make local-run  # build and invoke the watchdog locally
make deploy     # sync ec2/ to S3, build and deploy (stack: home-lab-failover)
make ec2-sync   # sync ec2/ to S3 only
make ec2-secrets NAME=<name> [FILE=<path>]  # SecureString /home-lab-failover/ec2/<name>
make hooks      # run the tests before every commit
```
