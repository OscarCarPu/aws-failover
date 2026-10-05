# ECR

Images EC2 pulls when it takes over (see [Ec2.md](Ec2.md)).

Repos `gv-api` and `gv-web` in `eu-south-2`.

## Push

Last step of each repo's `.gitea/workflows/deploy.yml`:

1. pushes `<repo>:latest`
2. deletes every other image, except the untagged manifests `latest`'s index references

Only the prod image is kept. No rollback image.

## Deploy user

IAM user `gv-deploy`, push/pull/delete on both repos. Its key is in the Gitea secrets `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`
