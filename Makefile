# Developer shortcuts. CI runs the same commands (.github/workflows/ci.yml).
.DEFAULT_GOAL := help
SHELL := /bin/bash

TF_ENVS   := staging pack-prod auto-prod
TF_STACKS := bootstrap org $(addprefix envs/,$(TF_ENVS))
IMAGE     ?= kairox/app:dev

.PHONY: help
help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-16s %s\n", $$1, $$2}'

.PHONY: sync
sync: ## Install the workspace (dev deps included)
	uv sync --frozen

.PHONY: lint
lint: ## ruff + mypy + import-linter
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy packages apps
	uv run lint-imports

.PHONY: test
test: ## pytest (DB tests need KX_TEST_PG_DSN, e.g. make up then KX_TEST_PG_DSN=postgresql://postgres:postgres@localhost:5432/postgres)
	uv run pytest

.PHONY: up
up: ## Start postgres + valkey
	docker compose up -d postgres valkey

.PHONY: up-all
up-all: ## Build and start migrate + api + worker as well
	docker compose up --build -d

.PHONY: down
down: ## Stop the stack and DROP local data
	docker compose down -v

.PHONY: image
image: ## Build the arm64 image locally (buildx)
	docker buildx build --platform linux/arm64 -t $(IMAGE) --load .

.PHONY: tf-fmt
tf-fmt: ## terraform fmt (writes)
	terraform -chdir=infra fmt -recursive

.PHONY: tf-validate
tf-validate: ## terraform init -backend=false + validate for every stack
	@set -e; for s in $(TF_STACKS); do \
	  echo "== $$s"; \
	  terraform -chdir=infra/$$s init -backend=false -input=false -no-color >/dev/null; \
	  terraform -chdir=infra/$$s validate -no-color; \
	done

.PHONY: tf-scan
tf-scan: ## checkov static scan of infra/
	uvx --from checkov==3.4.3 checkov -d infra --framework terraform --quiet --compact

.PHONY: lint-ci
lint-ci: ## actionlint + hadolint
	uvx --from actionlint-py actionlint
	uvx --from hadolint-bin hadolint Dockerfile
