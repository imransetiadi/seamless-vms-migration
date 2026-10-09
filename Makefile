# Let's use bash!
SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c

# --- Configuration Variables ---
# Reuse container content if possible. Set to 'false' to always rebuild.
USE_CACHE ?= true
USE_CONTAINER ?= true

# Container and Python Configuration
CONTAINER_ENGINE ?= podman
# Pre-built builder image (see builder/Containerfile and the build-builder.yml
# workflow). It ships the toolchain (python3.12 + devel, gcc, git, make) so the
# check-python-version / install-deps steps become no-ops and builds are faster.
# Set BUILDER_NAMESPACE to match your quay namespace, or override CONTAINER_IMAGE
# entirely to fall back to the plain base, e.g.:
#   make build CONTAINER_IMAGE=quay.io/centos/centos:stream10
BUILDER_NAMESPACE ?= os-migrate
CONTAINER_IMAGE  ?= quay.io/$(BUILDER_NAMESPACE)/os-migrate-builder:latest
CONTAINER_NAME   ?= os-migrate
PYTHON_VERSION   ?= 3.12

# Directory and Mount Structure
COLLECTION_ROOT         := $(CURDIR)
CONTAINER_COLLECTION_ROOT := /ansible_collections/os_migrate/os_migrate
MOUNT_PATH                := $(COLLECTION_ROOT):$(CONTAINER_COLLECTION_ROOT)
VENV_DIR                  := $(CONTAINER_COLLECTION_ROOT)/.venv

# --- Core Logic for Container Creation ---

# Check if the container already exists and strip any whitespace/newlines
CONTAINER_EXISTS = $(strip $(shell $(CONTAINER_ENGINE) ps -a -q -f name=$(CONTAINER_NAME)))


# --- Dynamic Variable Setup ---

# Check if SELinux is enforcing to set container security options
GETENFORCE_CMD := $(shell command -v getenforce 2>/dev/null)
SELINUX_ENFORCING := $(shell $(GETENFORCE_CMD) 2>/dev/null | grep -q "Enforcing" && echo "yes" || echo "no")

ifeq ($(SELINUX_ENFORCING),yes)
    SECURITY_OPT := --security-opt label=disable
else
    SECURITY_OPT :=
endif

# Extract collection metadata from galaxy.yml
GALAXY_YML := $(COLLECTION_ROOT)/galaxy.yml
ifneq ($(wildcard $(GALAXY_YML)),)
    COLLECTION_NAMESPACE := $(shell grep -E "^namespace:" $(GALAXY_YML) | sed 's/namespace: *//g')
    COLLECTION_NAME      := $(shell grep -E "^name:" $(GALAXY_YML) | sed 's/name: *//g')
    COLLECTION_VERSION   := $(shell grep -E "^version:" $(GALAXY_YML) | sed 's/version: *//g')
    COLLECTION_TARBALL   := $(COLLECTION_NAMESPACE)-$(COLLECTION_NAME)-$(COLLECTION_VERSION).tar.gz
else
    COLLECTION_TARBALL   :=
endif

# Collection install path (project-local, excluded from build via build_ignore)
COLLECTIONS_PATH       := $(CONTAINER_COLLECTION_ROOT)/.ansible/collections
COLLECTION_INSTALL_DIR := $(COLLECTIONS_PATH)/ansible_collections/$(COLLECTION_NAMESPACE)/$(COLLECTION_NAME)

# --- Core Logic for Container Creation ---

# Check if the container already exists
CONTAINER_EXISTS = $(shell $(CONTAINER_ENGINE) ps -a -q -f name=$(CONTAINER_NAME))

# Determine if we need to create a container based on cache settings and existence.
# Default to 0 (don't create).
CREATE_CONTAINER := 0
ifeq ($(USE_CACHE),true)
    # If using cache, only create if the container doesn't exist.
    ifeq ($(CONTAINER_EXISTS),)
        CREATE_CONTAINER := 1
    endif
else
    # If not using cache, always create a fresh container.
    CREATE_CONTAINER := 1
endif

# --- Phony Targets Definition ---
.PHONY: all help build clean-build tests test-ansible-lint test-ansible-sanity test-ansible-units \
        create-centos-container clean-centos-container check-root check-python-version \
        create-venv install-deps install generate-auth-files

# --- Main User-Facing Targets ---

# Default target when running `make`
all: build

# Help screen
help:
	@echo "Available targets:"
	@echo "  help                  - Display this help message"
	@echo "  build                 - Build the Ansible collection tarball"
	@echo "  clean-build           - Remove the built collection tarball"
	@echo "  install               - Build and install the collection and all dependencies in the container"
	@echo "  tests                 - Launch all available tests (lint, sanity, units)"
	@echo "  test-ansible-lint     - Launch ansible-lint tests"
	@echo "  test-ansible-sanity   - Launch ansible-sanity tests"
	@echo "  test-ansible-units    - Launch ansible-test unit tests"
	@echo "  test-e2e-tenant       - Launch e2e tenant tests"
	@echo "  test-e2e-admin        - Launch e2e admin tests"
	@echo "  generate-auth-files   - Generate auth files for e2e tests"
	@echo "  seamless-help         - Seamless Migrate stack on Colima/Compose (make seamless-help)"
	@echo ""
	@echo "  create-centos-container - Create the CentOS container (if needed)"
	@echo "  clean-centos-container  - Stop and remove the CentOS container"
	@echo "  create-venv           - Create the Python virtual environment in the container"
	@echo "  install-deps          - Install Python dependencies from requirements files"
	@echo "  vendor-import         - Import the OpenStack upstream collection as a git submodule"
	@echo "  vendor-links          - Create symlinks for vendored modules and module_utils"
	@echo "  vendor-clean          - Remove symlinks for vendored modules and module_utils"
	@echo ""
	@echo "Customizable variables:"
	@echo "  USE_CACHE        - Reuse container if it exists (default: $(USE_CACHE))"
	@echo "  CONTAINER_ENGINE - Container runtime (default: $(CONTAINER_ENGINE))"
	@echo "  CONTAINER_IMAGE  - Container image (default: $(CONTAINER_IMAGE))"
	@echo "  PYTHON_VERSION   - Python version to use in the container (default: $(PYTHON_VERSION))"

# --- Vendor install ---
VENDOR_DIR         := plugins/modules/_vendor
UPSTREAM_REPO      := $(VENDOR_DIR)/openstack.cloud
UPSTREAM_MODULES   := $(UPSTREAM_REPO)/plugins/modules
UPSTREAM_UTILS     := $(UPSTREAM_REPO)/plugins/module_utils
# Symlink targets must be relative to the link location (plugins/modules/ or plugins/module_utils/).
VENDOR_MODULE_LINK_SRC := _vendor/openstack.cloud/plugins/modules
VENDOR_UTIL_LINK_SRC   := ../modules/_vendor/openstack.cloud/plugins/module_utils

# Latest stable release:
OS_CLOUD_VERSION   ?= 2.6.0

# List of required modules from vendor openstack.cloud collection.
VENDORED_MODULES := auth compute_flavor compute_flavor_info floating_ip identity_domain identity_role \
	identity_user identity_user_info image image_info keypair network networks_info port project \
	project_info role_assignment router security_group security_group_rule server \
	server_action server_info server_volume subnet subnets_info volume volume_info

VENDORED_MODULE_UTILS := openstack ironic

# Sanity test excludes for vendored OpenStack modules and upstream sources.
VENDORED_SANITY_EXCLUDES := \
	--exclude plugins/modules/_vendor/ \
	$(foreach mod,$(VENDORED_MODULES),--exclude plugins/modules/$(mod).py ) \
	$(foreach util,$(VENDORED_MODULE_UTILS),--exclude plugins/module_utils/$(util).py )

# Ansible-lint CLI excludes for the same vendored content (exclude_paths in
# .ansible-lint does not reliably skip Python module files in ansible-lint 24+).
ANSIBLE_LINT_VENDORED_EXCLUDES := \
	--exclude .ansible/collections/ \
	$(foreach mod,$(VENDORED_MODULES),--exclude plugins/modules/$(mod).py ) \
	$(foreach util,$(VENDORED_MODULE_UTILS),--exclude plugins/module_utils/$(util).py )

.PHONY: vendor-import vendor-links vendor-clean

# Import vendor openstack.cloud collection.
vendor-import: vendor-clean
	@echo "--- Initializing OpenStack upstream submodule at version $(OS_CLOUD_VERSION) ---"
	@mkdir -p $(VENDOR_DIR)
	@# Check for an actual git repo, not just the directory: an uninitialized
	@# submodule leaves the path present-but-empty, which would make a plain
	@# "[ ! -d ]" guard skip setup and then run git against the parent repo.
	@if [ ! -e "$(UPSTREAM_REPO)/.git" ]; then \
		if git config --file .gitmodules --get submodule.$(UPSTREAM_REPO).url >/dev/null 2>&1; then \
			echo "Initializing registered submodule..."; \
			git submodule update --init $(UPSTREAM_REPO); \
		else \
			echo "Adding submodule..."; \
			git submodule add https://github.com/openstack/ansible-collections-openstack.git $(UPSTREAM_REPO); \
		fi \
	fi

	@echo "Checking out tag $(OS_CLOUD_VERSION)..."
	@cd $(UPSTREAM_REPO) && \
		git fetch --tags && \
		git checkout $(OS_CLOUD_VERSION)

# Link vendored modules and module_utils.
vendor-links: vendor-import
	@echo "--- Creating symlinks for vendored modules ---"
	@# Symlink Modules
	@for mod in $(VENDORED_MODULES); do \
		ln -sf $(VENDOR_MODULE_LINK_SRC)/$$mod.py plugins/modules/$$mod.py; \
		echo "Linked module: os_migrate.os_migrate.$$mod"; \
	done
	@# Fix for the specific 'openstack' namespace import
	@echo "--- Fixing openstack namespace imports in modules ---"
	@for mod in $(VENDORED_MODULES); do \
		sed -i 's/ansible_collections\.openstack\.cloud\.plugins\.module_utils\.openstack/ansible_collections.os_migrate.os_migrate.plugins.module_utils.openstack/g' plugins/modules/$$mod.py; \
	done
	@# Symlink Module Utils
	@echo "--- Linking upstream module_utils ---"
	@for util in $(VENDORED_MODULE_UTILS); do \
		ln -sf $(VENDOR_UTIL_LINK_SRC)/$$util.py plugins/module_utils/$$util.py; \
		echo "Linked module_util: os_migrate.os_migrate.$$util"; \
	done

# Clean vender symlinks and module_utils.
vendor-clean:
	@echo "--- Removing vendored symlinks ---"
	@for mod in $(VENDORED_MODULES); do rm -f plugins/modules/$$mod.py; done
	@for util in $(VENDORED_MODULE_UTILS); do rm -f plugins/module_utils/$$util.py; done
	@rm -rf plugins/module_utils/openstack/cloud


# --- Build Targets ---

build: check-root clean-build install-deps
	@echo "--- Building Ansible collection: $(COLLECTION_TARBALL) ---"
	@$(CONTAINER_ENGINE) exec -w $(CONTAINER_COLLECTION_ROOT) $(CONTAINER_NAME) bash -c '\
		make vendor-links && \
		source $(VENV_DIR)/bin/activate && \
		pip install ansible-core && \
		ansible-galaxy collection build --force'


clean-build:
	@echo "--- Cleaning built collection ---"
	@if [ -n "$(COLLECTION_TARBALL)" ]; then \
		if [ -f "$(COLLECTION_TARBALL)" ]; then \
			echo "Removing $(COLLECTION_TARBALL)"; \
			rm -f "$(COLLECTION_TARBALL)"; \
		fi \
	else \
		echo "Skipping removal, collection tarball name not determined from galaxy.yml."; \
	fi

# --- Container and Environment Setup Targets ---

# Define a reusable function to verify we're in the right directory
define verify_collection_root
    @if [ ! -f "$(COLLECTION_ROOT)/galaxy.yml" ]; then \
        echo "Error: Must be run from the Ansible collection root directory (missing galaxy.yml)."; \
        exit 1; \
    fi
endef

check-root:
	$(call verify_collection_root)

create-centos-container:
ifeq ($(CREATE_CONTAINER),1)
	@# This recipe runs only if a new container is needed.
	@# First, check with a shell 'if' if the old one must be cleaned up.
	@if [ "$(USE_CACHE)" = "false" ] && [ -n "$(CONTAINER_EXISTS)" ]; then \
		echo "--- Stale container found and USE_CACHE is false. Cleaning it first. ---"; \
		$(MAKE) clean-centos-container; \
	fi
	@echo "--- Spawning new container: $(CONTAINER_NAME) ---"
	@$(CONTAINER_ENGINE) run --pull always -q --rm -d --name $(CONTAINER_NAME) -v $(MOUNT_PATH) \
		$(SECURITY_OPT) $(CONTAINER_IMAGE) sleep infinity
else
	@# This recipe runs if CREATE_CONTAINER is 0
	@echo "--- Using cached container ---"
endif

clean-centos-container:
	@echo "--- Removing container '$(CONTAINER_NAME)' if it exists ---"
	@$(CONTAINER_ENGINE) rm -f $(CONTAINER_NAME)

check-python-version: create-centos-container
	@echo "--- Checking for Python $(PYTHON_VERSION) in container ---"
	@$(CONTAINER_ENGINE) exec $(CONTAINER_NAME) bash -c '\
	if [[ ! -x "$$(command -v pip$(PYTHON_VERSION))" ]]; then \
		echo "Installing Python $(PYTHON_VERSION) development packages..."; \
		dnf upgrade --refresh -y --skip-broken --nobest && \
		dnf -y install gcc python$(PYTHON_VERSION) python$(PYTHON_VERSION)-devel >/dev/null || \
			(echo "Error: packages are unavailable." && exit 1); \
	fi'

create-venv: check-python-version
	@echo "--- Ensuring venv exists at $(VENV_DIR) ---"
	@$(CONTAINER_ENGINE) exec -w $(CONTAINER_COLLECTION_ROOT) $(CONTAINER_NAME) bash -c '\
		if [[ ! -d "$(VENV_DIR)" ]]; then \
			echo "Creating venv..."; \
			python$(PYTHON_VERSION) -m venv $(VENV_DIR); \
		fi'

install-deps: create-venv
	@echo "--- Installing/updating Python dependencies ---"
	@$(CONTAINER_ENGINE) exec -w $(CONTAINER_COLLECTION_ROOT) $(CONTAINER_NAME) bash -c '\
		dnf install -y git; \
		source $(VENV_DIR)/bin/activate; \
		pip install --root-user-action ignore -q --upgrade pip; \
		pip install --root-user-action ignore -q -r requirements.txt; \
		pip install --root-user-action ignore -q -r requirements-tests.txt'

install: build
	@echo "--- Installing collection $(COLLECTION_TARBALL) into the container ---"
	@$(CONTAINER_ENGINE) exec -w $(CONTAINER_COLLECTION_ROOT) $(CONTAINER_NAME) bash -c '\
		source $(VENV_DIR)/bin/activate; \
		rm -rf "$(COLLECTION_INSTALL_DIR)"; \
		export ANSIBLE_COLLECTIONS_PATH="$(COLLECTIONS_PATH)"; \
		pip install ansible-core && \
		ansible-galaxy collection install $(COLLECTION_TARBALL) \
		  --collections-path "$(COLLECTIONS_PATH)" --force-with-deps'

# --- Test Targets ---

tests: test-ansible-lint test-ansible-sanity test-ansible-units

test-ansible-lint: install-deps
	@echo "--- Launching ansible-lint ---"
	@$(CONTAINER_ENGINE) exec -w $(CONTAINER_COLLECTION_ROOT) $(CONTAINER_NAME) bash -c '\
		make vendor-links && \
		source $(VENV_DIR)/bin/activate && \
		ansible-lint $(ANSIBLE_LINT_VENDORED_EXCLUDES)'
	@if [[ $(USE_CACHE) == false ]]; then $(MAKE) clean-centos-container; fi

test-ansible-sanity: install-deps
	@echo "--- Running Ansible sanity tests ---"
	@$(CONTAINER_ENGINE) exec -w $(CONTAINER_COLLECTION_ROOT) $(CONTAINER_NAME) bash -c '\
		make vendor-links && \
		source $(VENV_DIR)/bin/activate && \
		ansible-test sanity --python $(PYTHON_VERSION) --requirements \
		  $(VENDORED_SANITY_EXCLUDES)'
	@if [[ $(USE_CACHE) == false ]]; then $(MAKE) clean-centos-container; fi

test-ansible-units: install-deps
	@echo "--- Running Ansible unit tests ---"
	@$(CONTAINER_ENGINE) exec -w $(CONTAINER_COLLECTION_ROOT) $(CONTAINER_NAME) bash -c '\
		make vendor-links && \
		source $(VENV_DIR)/bin/activate && \
		ansible-test units --python $(PYTHON_VERSION) --local'
	@if [[ $(USE_CACHE) == false ]]; then $(MAKE) clean-centos-container; fi


# --- E2E Test Auth Generation ---

generate-auth-files: install-deps install
	@echo "--- Generating auth files ---"
	@echo "SRC_CLOUD: $(SRC_CLOUD)"
	@echo "DST_CLOUD: $(DST_CLOUD)"
	@if [ -z "$(SRC_CLOUD)" ] || [ -z "$(DST_CLOUD)" ]; then \
		echo "Error: SRC_CLOUD and DST_CLOUD variables must be set"; \
		exit 1; \
	fi
	@$(CONTAINER_ENGINE) exec -w $(CONTAINER_COLLECTION_ROOT) $(CONTAINER_NAME) bash -c '\
		source "$(VENV_DIR)/bin/activate" && \
		pip install --root-user-action ignore -q shyaml && \
		dnf -y install util-linux openssh-clients && \
		./scripts/auth-from-clouds.sh --config "$(CONTAINER_COLLECTION_ROOT)/tests/clouds.yml" --src "$(SRC_CLOUD)" --dst "$(DST_CLOUD)" | tee "$(CONTAINER_COLLECTION_ROOT)/tests/auth_tenant.yml"'


test-e2e-tenant: install-deps install generate-auth-files
	@echo "--- Running e2e tenant tests ---"
	@$(CONTAINER_ENGINE) exec -w $(CONTAINER_COLLECTION_ROOT) $(CONTAINER_NAME) bash -c '\
		if [ ! -f "$(CONTAINER_COLLECTION_ROOT)/tests/auth_tenant.yml" ]; then \
			echo "Error: auth_tenant.yml not found. Please run auth generation first."; \
			exit 1; \
		fi && \
		source $(VENV_DIR)/bin/activate && \
		cd tests/e2e; \
		export ANSIBLE_COLLECTIONS_PATH="$(COLLECTIONS_PATH)"; \
		ansible-playbook \
			-v \
			-i $(CONTAINER_COLLECTION_ROOT)/inventory/localhost.yml \
			-e os_migrate_tests_tmp_dir=$(CONTAINER_COLLECTION_ROOT)/tests/e2e/tmp \
			-e os_migrate_data_dir=$(CONTAINER_COLLECTION_ROOT)/tests/e2e/tmp/data \
			-e os_migrate_conversion_host_key=$(CONTAINER_COLLECTION_ROOT)/tests/e2e/tmpdata/conversion/ssh.key \
			-e @$(CONTAINER_COLLECTION_ROOT)/tests/auth_tenant.yml \
			-e @$(CONTAINER_COLLECTION_ROOT)/tests/e2e/tasks/tenant/scenario_variables.yml \
			$(OS_MIGRATE_E2E_TEST_ARGS) test_as_tenant.yml'

# --- Docs Targets ---

docs: docs-diagrams
	@echo "--- Generate documentation ---"
	@$(CONTAINER_ENGINE) exec -w $(CONTAINER_COLLECTION_ROOT) $(CONTAINER_NAME) bash -c '\
		dnf -y install git-core && \
		source $(VENV_DIR)/bin/activate && \
		pip install --root-user-action ignore -r requirements-docs.txt && \
		./scripts/docs-build.sh'

docs-diagrams: install
	@echo "--- Generate diagrams ---"
	@$(CONTAINER_ENGINE) exec -w $(CONTAINER_COLLECTION_ROOT) $(CONTAINER_NAME) bash -c '\
		dnf config-manager --set-enabled crb && \
		dnf install -y epel-release && \
		dnf install -y plantuml graphviz && \
		plantuml -progress -SDpi=150 -output render ./docs/src/images/plantuml/*.plantuml && \
		echo'

# =============================================================================
# Seamless Migrate: local stack on a dedicated Colima profile (SDD 17.1)
# =============================================================================
# Every docker command is pinned to the Docker context of the Colima profile
# "seamless" (colima-seamless). The developer's default profile and contexts are
# never used or modified. Secrets live in deploy/compose/.env and tokens.yaml,
# generated by scripts/compose-init.sh (git-ignored).

SEAMLESS_COLIMA_PROFILE ?= seamless
SEAMLESS_COLIMA_CPU     ?= 4
SEAMLESS_COLIMA_MEMORY  ?= 6
SEAMLESS_COLIMA_DISK    ?= 40
SEAMLESS_DOCKER_CONTEXT ?= colima-$(SEAMLESS_COLIMA_PROFILE)
SEAMLESS_COMPOSE_DIR    := $(CURDIR)/deploy/compose
SEAMLESS_COMPOSE_FILE   := $(SEAMLESS_COMPOSE_DIR)/compose.yaml
SEAMLESS_ENV_FILE       := $(SEAMLESS_COMPOSE_DIR)/.env
SEAMLESS_TOKENS_FILE    := $(SEAMLESS_COMPOSE_DIR)/tokens.yaml
# optional: restrict logs to one service, e.g. make seamless-logs SEAMLESS_SERVICE=seamless
SEAMLESS_SERVICE        ?=
# optional: an extra Compose file merged over compose.yaml (e.g. a git-ignored compose.local.yaml that mounts clouds.yaml)
SEAMLESS_EXTRA_COMPOSE_FILE ?=

# Compose project name (also pinned by `name:` in compose.yaml); -p stops an exported COMPOSE_PROJECT_NAME from redirecting
# the Make targets to another project's networks and volumes.
SEAMLESS_PROJECT        ?= seamless

# Variables that deploy/compose/.env owns. Compose lets an exported shell variable win over --env-file, so a stray
# POSTGRES_PASSWORD or TYPESAFE_API_KEY in the developer's shell would silently replace the generated value (and a
# different DB password makes the control plane fail against the existing pgdata volume). The wrapper unsets them;
# every other variable (SEAMLESS_HOST_PORT, SEAMLESS_DEMO_SPEED, ...) can still be overridden from the shell.
SEAMLESS_ENV_OWNED = POSTGRES_PASSWORD JEV_MCP_AUTH_TOKEN TYPESAFE_API_KEY COMPOSE_PROFILES SEAMLESS_JEV_MODE SEAMLESS_MEMORY_URL SEAMLESS_MEMORY_SECRET
SEAMLESS_UNSET_ENV = $(foreach v,$(SEAMLESS_ENV_OWNED),-u $(v))

# Container engine: docker (default; Colima context colima-seamless, or any Docker host with
# SEAMLESS_DOCKER_CONTEXT=default) or podman (`podman compose`, which runs docker-compose when it is installed —
# recommended — or podman-compose). Example: make seamless-demo SEAMLESS_ENGINE=podman
SEAMLESS_ENGINE ?= docker
SEAMLESS_HOST_PORT ?= 8080
ifeq ($(SEAMLESS_ENGINE),podman)
SEAMLESS_DOCKER       = podman
SEAMLESS_COMPOSE_CMD  = podman compose
# podman-compose has no --wait: the targets poll /api/v1/health instead
SEAMLESS_UP_WAIT      =
else
# DOCKER_HOST would override the context, so it is unset for these commands.
SEAMLESS_DOCKER       = env -u DOCKER_HOST DOCKER_CONTEXT=$(SEAMLESS_DOCKER_CONTEXT) docker --context $(SEAMLESS_DOCKER_CONTEXT)
SEAMLESS_COMPOSE_CMD  = $(SEAMLESS_DOCKER) compose
SEAMLESS_UP_WAIT      = --wait --wait-timeout 300
endif
SEAMLESS_COMPOSE_BASE = $(SEAMLESS_COMPOSE_CMD) -p $(SEAMLESS_PROJECT) -f $(SEAMLESS_COMPOSE_FILE)$(if $(SEAMLESS_EXTRA_COMPOSE_FILE), -f $(SEAMLESS_EXTRA_COMPOSE_FILE))
# wait until the control plane answers (both engines; Docker's --wait already covers the healthchecks)
SEAMLESS_WAIT_HEALTH  = for i in $$(seq 1 150); do curl -fsS -o /dev/null http://127.0.0.1:$(SEAMLESS_HOST_PORT)/api/v1/health && break; \
                        [ $$i = 150 ] && { echo "the control plane did not become healthy in 300 s: make seamless-logs"; exit 1; }; sleep 2; done
SEAMLESS_COMPOSE      = env $(SEAMLESS_UNSET_ENV) $(SEAMLESS_COMPOSE_BASE) --env-file $(SEAMLESS_ENV_FILE)
# parse-only placeholders for down/reset when .env is gone (nothing is started with them)
SEAMLESS_COMPOSE_NOENV = env POSTGRES_PASSWORD=unused JEV_MCP_AUTH_TOKEN=unused $(SEAMLESS_COMPOSE_BASE)

.PHONY: seamless-help seamless-colima-up seamless-check-context seamless-check-env seamless-init \
        seamless-up seamless-demo seamless-down seamless-ps seamless-logs seamless-reset \
        seamless-test seamless-check dashboard-build

seamless-help:
	@echo "Seamless Migrate stack (engine '$(SEAMLESS_ENGINE)'; Docker: Colima profile '$(SEAMLESS_COLIMA_PROFILE)', context '$(SEAMLESS_DOCKER_CONTEXT)'):"
	@echo "  engines: SEAMLESS_ENGINE=docker (default) | podman;  any Docker host: SEAMLESS_DOCKER_CONTEXT=default"
	@echo "  seamless-colima-up  - start the dedicated Colima profile ($(SEAMLESS_COLIMA_CPU) CPU / $(SEAMLESS_COLIMA_MEMORY) GiB / $(SEAMLESS_COLIMA_DISK) GiB) without changing your active Docker context"
	@echo "  seamless-init       - generate deploy/compose/.env and tokens.yaml (prints the admin token once)"
	@echo "  seamless-up         - build and start postgres, seamless (and jev when COMPOSE_PROFILES=ai)"
	@echo "  seamless-demo       - same, with simulated providers and executor (SEAMLESS_DEMO=true)"
	@echo "  seamless-down       - stop and remove the containers (volumes are kept)"
	@echo "  seamless-ps         - show container status"
	@echo "  seamless-logs       - follow logs (SEAMLESS_SERVICE=seamless|postgres|jev to filter)"
	@echo "  seamless-reset      - DELETE containers and volumes (requires CONFIRM=yes)"
	@echo "  seamless-test       - run the control-plane test suite (cd seamless && .venv/bin/pytest -q)"
	@echo "  dashboard-build     - build the dashboard (cd dashboard && npm ci && npm run build)"
	@echo "  seamless-check      - every CI check that runs locally: tests, ruff, collection tests, scans, dashboard"

# Start (or create) the dedicated Colima profile; idempotent. Colima activates the context of the profile it
# starts by default (--activate, default true), which would switch the developer's current Docker context:
# --activate=false keeps it, and every command of this Makefile addresses colima-seamless explicitly anyway.
seamless-colima-up:
	@command -v colima >/dev/null 2>&1 || { echo "colima not found: brew install colima docker docker-compose"; exit 1; }
	colima start $(SEAMLESS_COLIMA_PROFILE) --activate=false --cpu $(SEAMLESS_COLIMA_CPU) --memory $(SEAMLESS_COLIMA_MEMORY) --disk $(SEAMLESS_COLIMA_DISK)
	@$(SEAMLESS_DOCKER) info --format 'context $(SEAMLESS_DOCKER_CONTEXT): Docker {{.ServerVersion}}, {{.NCPU}} CPU, {{.MemTotal}} bytes RAM'
	@echo "active Docker context is still: $$(docker context show)"

seamless-check-context:
ifeq ($(SEAMLESS_ENGINE),podman)
	@command -v podman >/dev/null 2>&1 || { echo "podman not found: https://podman.io/docs/installation"; exit 1; }
	@podman info >/dev/null 2>&1 || { echo "podman is not reachable (macOS/Windows: podman machine start)"; exit 1; }
	@podman compose version >/dev/null 2>&1 || { echo "podman compose needs docker-compose (recommended) or podman-compose installed"; exit 1; }
else
	@docker context inspect $(SEAMLESS_DOCKER_CONTEXT) >/dev/null 2>&1 || { echo "Docker context '$(SEAMLESS_DOCKER_CONTEXT)' not found. Run: make seamless-colima-up"; exit 1; }
	@$(SEAMLESS_DOCKER) info >/dev/null 2>&1 || { echo "Colima profile '$(SEAMLESS_COLIMA_PROFILE)' is not running. Run: make seamless-colima-up"; exit 1; }
endif

seamless-check-env:
	@[ -f "$(SEAMLESS_ENV_FILE)" ] && [ -f "$(SEAMLESS_TOKENS_FILE)" ] || { echo "Missing deploy/compose/.env or tokens.yaml. Run: make seamless-init (prints the admin token once)"; exit 1; }

seamless-init:
	@scripts/compose-init.sh

seamless-up: seamless-check-context seamless-check-env
	$(SEAMLESS_COMPOSE) up -d --build $(SEAMLESS_UP_WAIT)
	@$(SEAMLESS_WAIT_HEALTH)
	@echo "Seamless Migrate: http://127.0.0.1:$(SEAMLESS_HOST_PORT)/   health: /api/v1/health"

# Same stack with simulated providers and executor against PostgreSQL (SDD 7.4, 15.1).
seamless-demo: seamless-check-context seamless-check-env
	SEAMLESS_DEMO=true $(SEAMLESS_COMPOSE) up -d --build $(SEAMLESS_UP_WAIT)
	@$(SEAMLESS_WAIT_HEALTH)
	@echo "Seamless Migrate (demo): http://127.0.0.1:$(SEAMLESS_HOST_PORT)/   sign in with your admin token"

# --profile '*' also stops the optional jev sidecar when COMPOSE_PROFILES is empty.
# Works without .env (placeholder values are only needed to parse the file).
seamless-down: seamless-check-context
	@if [ -f "$(SEAMLESS_ENV_FILE)" ]; then \
		$(SEAMLESS_COMPOSE) --profile '*' down --remove-orphans; \
	else \
		$(SEAMLESS_COMPOSE_NOENV) --profile '*' down --remove-orphans; \
	fi

seamless-ps: seamless-check-context seamless-check-env
	$(SEAMLESS_COMPOSE) --profile '*' ps

seamless-logs: seamless-check-context seamless-check-env
	$(SEAMLESS_COMPOSE) --profile '*' logs -f --tail=200 $(SEAMLESS_SERVICE)

# Destructive: removes the pgdata, seamless-data and seamless-secrets volumes (all plans, events, run
# directories and the provider credentials entered in the dashboard).
seamless-reset: seamless-check-context
	@[ "$(CONFIRM)" = "yes" ] || { echo "This DELETES the PostgreSQL and data volumes of the seamless stack. Re-run with CONFIRM=yes"; exit 1; }
	@if [ -f "$(SEAMLESS_ENV_FILE)" ]; then \
		$(SEAMLESS_COMPOSE) --profile '*' down --volumes --remove-orphans; \
	else \
		$(SEAMLESS_COMPOSE_NOENV) --profile '*' down --volumes --remove-orphans; \
	fi

# Control-plane tests (SQLite always; PostgreSQL too when SEAMLESS_TEST_PG_URL is exported).
seamless-test:
	@[ -x seamless/.venv/bin/pytest ] || { echo "Create the venv first: cd seamless && python3 -m venv .venv && .venv/bin/pip install -e '.[dev,jev]'"; exit 1; }
	cd seamless && .venv/bin/pytest -q

# Everything CI runs that can run locally (QASuite §13, .github/workflows/ci.yml), in one go.
seamless-check: seamless-test
	cd seamless && .venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
	@seamless/.venv/bin/python -c "import ansible, yaml, openstack" 2>/dev/null \
	 || { echo "The collection tests need the 'collection' extra: cd seamless && .venv/bin/pip install -e '.[dev,jev,collection]'"; exit 1; }
	@mkdir -p .cache/colltree/ansible_collections/os_migrate \
	 && ln -sfn "$(CURDIR)" .cache/colltree/ansible_collections/os_migrate/os_migrate
	cd .cache/colltree/ansible_collections/os_migrate/os_migrate && \
	  PYTHONPATH="$(CURDIR)/.cache/colltree" ANSIBLE_COLLECTIONS_PATH="$(CURDIR)/.cache/colltree" \
	  "$(CURDIR)/seamless/.venv/bin/python" -m pytest -q tests/unit/test_blocksync.py \
	    tests/unit/test_warm_migration.py tests/unit/test_warm_destination.py tests/unit/test_warm_playbooks.py \
	    tests/unit/test_role_modules_resolve.py
	@command -v gitleaks >/dev/null && gitleaks dir --redact --no-banner . || echo "gitleaks not installed: skipped (S-17)"
	@command -v actionlint >/dev/null && actionlint || echo "actionlint not installed: skipped"
	@command -v shellcheck >/dev/null && shellcheck -S warning scripts/*.sh tests/e2e/*.sh || echo "shellcheck not installed: skipped"
	cd dashboard && npm run typecheck && npm run lint && npm test && npm run build

dashboard-build:
	@command -v npm >/dev/null 2>&1 || { echo "npm (Node 22) is required"; exit 1; }
	cd dashboard && if [ -f package-lock.json ]; then npm ci; else npm install; fi && npm run build
