.PHONY: install test lint format run dev check docker-build docker-run

PYTHON ?= python3
IMAGE  ?= immich-album-tagger:latest

install:            ## create .venv and install with dev tools
	$(PYTHON) -m venv .venv
	.venv/bin/pip install -e '.[dev]'

test:               ## run the test suite
	.venv/bin/pytest -q

lint:               ## static checks
	.venv/bin/ruff check src tests scripts
	.venv/bin/ruff format --check src tests scripts

format:             ## auto-format
	.venv/bin/ruff format src tests scripts
	.venv/bin/ruff check --fix src tests scripts

run:                ## run the UI against your real Immich (reads .env)
	set -a; . ./.env; set +a; .venv/bin/immich-album-tagger serve

check:              ## test connections to Immich and the LLM (reads .env)
	set -a; . ./.env; set +a; .venv/bin/immich-album-tagger check

dev:                ## run the UI against a fake Immich + fake LLM
	.venv/bin/python scripts/dev_server.py

docker-build:       ## build the image
	docker build -t $(IMAGE) .

docker-run:         ## run the image with ./config and .env
	docker run --rm -it --env-file .env -p 8080:8080 -v $(PWD)/config:/config:ro \
	  -v immich-album-tagger-data:/data $(IMAGE)
