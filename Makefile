# Everything runs in Docker. `make help` lists the targets.
#
#   make build                      build the runtime image with buildx
#   make recalls RUN_ID=20260820    run one stage
#   make pipeline RUN_ID=20260820   run every stage, in order
#
# Credentials come from the environment: IRES_AUTHORIZATION_USER / IRES_AUTHORIZATION_KEY
# (and GCS_HMAC_KEY_ID / GCS_HMAC_SECRET_KEY for a gs:// DEST).

DOCKER        ?= docker
IMAGE         ?= ires-fetch
TAG           ?= latest
DEV_TAG       ?= dev
PLATFORM      ?= linux/amd64
BUILDX_FLAGS  ?=

# A stage writes to DEST inside the container; OUT is what backs /data on the host: a
# directory (bind mount) or a Docker volume name, for a daemon that cannot see this tree.
RUN_ID        ?= $(shell date -u +%Y%m%dT%H%M%S)
# Pin it: `?=` is recursive, so the `date` above would re-run once per stage and each
# stage would land under a different run_id. A command-line RUN_ID still wins.
RUN_ID        := $(RUN_ID)
OUT           ?= $(CURDIR)/out
DEST          ?= /data
MAX_WORKERS   ?= 4
LOGGING_LEVEL ?= INFO
LIMIT         ?=
ROWS          ?=
BATCH_SIZE    ?=
# `FORCE=1` refetches ids the target partition already holds, instead of resuming past
# them. Runs append either way, so a forced stage writes those ids a second time.
FORCE         ?=

# The by-id stages read the product / event ids of this run's RECALLS parquet.
RECALLS_SOURCE ?= $(DEST)/raw/api_responses/recalls/run_id=$(RUN_ID)

# One target per endpoint, named after `IresRequestTypes`; `pipeline` runs them in order.
STAGES    := recalls product-types event product event-products code-info \
             product-history event-product-history press-release-urls
ID_STAGES := event product event-products code-info product-history \
             event-product-history press-release-urls

request_type = $(shell echo '$(1)' | tr 'a-z-' 'A-Z_')

BUILD = $(DOCKER) buildx build --platform $(PLATFORM) --load \
        --build-arg UID=$(shell id -u) --build-arg GID=$(shell id -g) $(BUILDX_FLAGS)

RUN = $(DOCKER) run --rm --init \
      -e IRES_AUTHORIZATION_USER -e IRES_AUTHORIZATION_KEY \
      -e GCS_HMAC_KEY_ID -e GCS_HMAC_SECRET_KEY \
      -v '$(OUT):/data' $(IMAGE):$(TAG)

FETCH_FLAGS = --run-id $(RUN_ID) --dest $(DEST) --max-workers $(MAX_WORKERS) \
              --logging-level $(LOGGING_LEVEL) \
              $(if $(LIMIT),--limit $(LIMIT)) $(if $(ROWS),--rows $(ROWS)) \
              $(if $(BATCH_SIZE),--batch-size $(BATCH_SIZE)) $(if $(FORCE),--force)

.DEFAULT_GOAL := help

# The stages share one RUN_ID and the by-id ones depend on `recalls`: never run them in
# parallel, whatever -j says.
.NOTPARALLEL:

.PHONY: help build build-dev lock test lint shell clean pipeline $(STAGES) check-credentials

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  %-24s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo
	@echo "  Stages (in pipeline order): $(STAGES)"
	@echo "  Variables: RUN_ID OUT DEST MAX_WORKERS LIMIT ROWS BATCH_SIZE FORCE LOGGING_LEVEL IMAGE TAG PLATFORM"

build: ## Build the runtime image
	$(BUILD) --target runtime --tag $(IMAGE):$(TAG) .

build-dev: ## Build the dev image (tests, linters)
	$(BUILD) --target dev --tag $(IMAGE):$(DEV_TAG) .

lock: ## Regenerate uv.lock in Docker
	$(DOCKER) buildx build --platform $(PLATFORM) $(BUILDX_FLAGS) --target lockfile \
	    --output type=local,dest=. .

test: build-dev ## Run pytest in the dev image
	$(DOCKER) run --rm $(IMAGE):$(DEV_TAG) pytest

lint: build-dev ## Run ruff and sqlfluff in the dev image
	$(DOCKER) run --rm $(IMAGE):$(DEV_TAG) ruff check .
	$(DOCKER) run --rm $(IMAGE):$(DEV_TAG) ruff format --check .
	$(DOCKER) run --rm $(IMAGE):$(DEV_TAG) sqlfluff lint src/ires_fetch/sql

shell: build ## Open a shell in the runtime image, with OUT at /data
	$(DOCKER) run --rm -it --entrypoint bash -v '$(OUT):/data' $(IMAGE):$(TAG)

clean: ## Remove the images and the local output directory
	-$(DOCKER) image rm $(IMAGE):$(TAG) $(IMAGE):$(DEV_TAG)
	rm -rf '$(CURDIR)/out'

check-credentials:
	@test -n "$$IRES_AUTHORIZATION_USER" || { echo 'IRES_AUTHORIZATION_USER is not set' >&2; exit 1; }
	@test -n "$$IRES_AUTHORIZATION_KEY" || { echo 'IRES_AUTHORIZATION_KEY is not set' >&2; exit 1; }

# Create the bind-mount directory first: the daemon would create it root-owned.
$(STAGES): build check-credentials
	@case '$(OUT)' in /*) mkdir -p '$(OUT)';; esac
	$(RUN) $(FETCH_FLAGS) --request-type $(call request_type,$@) \
	    $(if $(filter $@,$(ID_STAGES)),--ids-source '$(RECALLS_SOURCE)')

recalls: ## Stage: POST /recalls/, paged (run first)
product-types: ## Stage: GET /search/producttypes
event: ## Stage: GET /recalls/event/{eventid}
product: ## Stage: GET /recalls/product/{productid}
event-products: ## Stage: GET /recalls/eventproducts/{eventid}
code-info: ## Stage: GET /search/codeinfo/{productid}
product-history: ## Stage: GET /search/producthistory/{productid}
event-product-history: ## Stage: GET /search/eventproducthistory/{eventid}
press-release-urls: ## Stage: GET /search/pressreleaseurls/{eventid}

# A re-run under the same RUN_ID picks the by-id stages up where they stopped: each one
# skips the ids its partition already holds.
pipeline: $(STAGES) ## Run every stage sequentially under one RUN_ID
