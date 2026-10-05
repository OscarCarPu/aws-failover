# home-lab-failover

SAM stack (`eu-south-2`) for the home lab.

| Doc                            | What                                    |
|--------------------------------|-----------------------------------------|
| [Lambdas](docs/Lambdas.md)     | Watchdog health check every 5 minutes   |
| [S3](docs/S3.md)               | gv-api backup bucket and uploader user  |
| [ECR](docs/Ecr.md)             | gv-api and gv-web images, deploy user   |
| [EC2](docs/Ec2.md)             | Failover instance                       |

## Usage

```bash
make test       # unit tests
make local-run  # build and invoke the watchdog locally
make deploy     # build and deploy (stack: home-lab-failover)
make hooks      # run the tests before every commit
```
