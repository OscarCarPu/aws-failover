.PHONY: help build local-run test deploy

help:
	@echo "make local-run  build and invoke the watchdog locally"
	@echo "make test       run unit tests"
	@echo "make deploy     build and deploy to AWS"

build:
	sam build

local-run: build
	sam local invoke --docker-network host

test:
	python -m pytest tests/unit -v

deploy: build
	sam deploy
