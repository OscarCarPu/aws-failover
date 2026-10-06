BUCKET = $(shell aws cloudformation describe-stacks --region eu-south-2 --stack-name home-lab-failover \
	--query "Stacks[0].Outputs[?OutputKey=='BackupBucketName'].OutputValue" --output text)

.PHONY: help build local-run test deploy ec2-sync ec2-secrets hooks

help:
	@echo "make local-run  build and invoke the watchdog locally"
	@echo "make test       run unit tests"
	@echo "make deploy     sync ec2/ to S3, build and deploy to AWS"
	@echo "make ec2-sync   sync ec2/ to S3"
	@echo "make ec2-secrets NAME=<name> [FILE=<path>]  SecureString /home-lab-failover/ec2/<name>, prompts without FILE"
	@echo "make hooks      run the tests before every commit"

build:
	sam build

local-run: build
	sam local invoke --docker-network host

test:
	python -m pytest tests/unit -v

deploy: build ec2-sync
	sam deploy

ec2-sync:
	aws s3 sync --region eu-south-2 --delete --exclude '*.env*' ec2/ s3://$(BUCKET)/ec2/

ec2-secrets:
	@test -n "$(NAME)" || { echo "usage: make ec2-secrets NAME=<name> [FILE=<path>]"; exit 1; }
ifdef FILE
	aws ssm put-parameter --region eu-south-2 --overwrite --type SecureString \
		--name /home-lab-failover/ec2/$(NAME) --value file://$(FILE)
else
	@printf '$(NAME): '; stty -echo; read v; stty echo; echo; \
		aws ssm put-parameter --region eu-south-2 --overwrite --type SecureString \
			--name /home-lab-failover/ec2/$(NAME) --value "$$v"
endif

hooks:
	git config core.hooksPath .githooks
