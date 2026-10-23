SHELL := bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help
MAKEFLAGS += --no-builtin-rules

PYTHON ?= python3
VENV := .venv
VBIN := $(VENV)/bin

KWOK_VERSION := v0.8.0
KUBE_VERSION := v1.34.12
KUBECTL_VALIDATE_VERSION := v0.0.4

OS := $(shell uname -s | tr '[:upper:]' '[:lower:]')
ARCH := $(shell uname -m | sed -e 's/x86_64/amd64/' -e 's/aarch64/arm64/')
PLATFORM := $(OS)_$(ARCH)

# SHA-256 of each release artifact: GitHub asset digests (KWOK), dl.k8s.io .sha256 files (kubectl),
# checksums.txt (kubectl-validate).
KWOKCTL_SHA256_darwin_amd64 := 16bccb569c36ac6b124b2521cfe71c4e4133cf22159c82d4698a3412646e9789
KWOKCTL_SHA256_darwin_arm64 := 9323a59adb9c27854bd2a76652ad00258bcbb68acc40aba0dc21e362b67b7d6a
KWOKCTL_SHA256_linux_amd64  := d5743166c657283d6e827c7c4c20cacfd9009fd94830f06718ca7b2c7099461f
KWOKCTL_SHA256_linux_arm64  := 5a642b27d3e975f36040d215640cd90fb4a63cb6fca0ad5865a61bcd9c81051b

KWOK_SHA256_darwin_amd64 := 6141864e82cb1085734cafecf0fc604f5b630fc172e0c4cf01ec38a005556240
KWOK_SHA256_darwin_arm64 := 69a064ec98d37844d9d742b5b28ff57825659a983ccc3e208daa6948b94d64ba
KWOK_SHA256_linux_amd64  := f5a4385a2ae1b9dd7f8acae18e002a75c0ebba124ae9d7259aa8dadb50711f46
KWOK_SHA256_linux_arm64  := 628f7d277860f524a2a775c217adcc58828e7b59ab0933680315784f1b8b0e37

KUBECTL_SHA256_darwin_amd64 := cefed789392b4d1f75cb81ee803d89c2bad68ec35414df127779a5557da00516
KUBECTL_SHA256_darwin_arm64 := 0c37a655d8495312c90554b61b9ff8400077e743ae2dc07008a529667a750db9
KUBECTL_SHA256_linux_amd64  := 90b7b9058ffeb5c10710bb1c73f541eaf426fb1b4e87df435db669de9a2564a2
KUBECTL_SHA256_linux_arm64  := c819e3eb6a540d4f1e6aacaac85580bf0793bd1e7d05095a47407733ccf0cfb7

KUBECTL_VALIDATE_SHA256_darwin_amd64 := 185c4de8c4de3391c8049f0a94de3265428327b452324ea67db82429cd13939c
KUBECTL_VALIDATE_SHA256_darwin_arm64 := cbc01e3ea4482021d7c52afe7aa451b15b4d0cdcb1b8a5a6739f8760628ecc09
KUBECTL_VALIDATE_SHA256_linux_amd64  := d2a8a3136988724465249ec3666ceea43ed1f9c371e9b9cc5707d10e31fc9c0c
KUBECTL_VALIDATE_SHA256_linux_arm64  := 4b5a8e7b97a72220ef44dece6242efa24aa9155fd3cbf5417615e599f2c59b8a

KWOK_URL := https://github.com/kubernetes-sigs/kwok/releases/download/$(KWOK_VERSION)
KUBECTL_URL := https://dl.k8s.io/release/$(KUBE_VERSION)/bin/$(OS)/$(ARCH)/kubectl
KUBECTL_VALIDATE_URL := https://github.com/kubernetes-sigs/kubectl-validate/releases/download/$(KUBECTL_VALIDATE_VERSION)/kubectl-validate_$(OS)_$(ARCH).tar.gz

.PHONY: help tools cluster-up cluster-down schemas install-dev lint fmt test-unit test-e2e test package clean

help: ## List targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "%-12s %s\n", $$1, $$2}'

tools: ## Download pinned kwokctl, kwok, kubectl and kubectl-validate into bin/
	@scripts/install-tool.sh kwokctl "$(KWOK_URL)/kwokctl-$(OS)-$(ARCH)" "$(KWOKCTL_SHA256_$(PLATFORM))"
	@scripts/install-tool.sh kwok "$(KWOK_URL)/kwok-$(OS)-$(ARCH)" "$(KWOK_SHA256_$(PLATFORM))"
	@scripts/install-tool.sh kubectl "$(KUBECTL_URL)" "$(KUBECTL_SHA256_$(PLATFORM))"
	@scripts/install-tool.sh kubectl-validate "$(KUBECTL_VALIDATE_URL)" "$(KUBECTL_VALIDATE_SHA256_$(PLATFORM))" kubectl-validate

cluster-up: tools ## Create or reconcile the KWOK cluster; kubeconfig in .deploysim/
	@KWOK_VERSION=$(KWOK_VERSION) KWOK_KUBE_VERSION=$(KUBE_VERSION) scripts/cluster-up.sh

cluster-down: ## Delete the KWOK cluster
	@scripts/cluster-down.sh

schemas: ## Export OpenAPI schemas from the running cluster into schemas/
	@scripts/export-schemas.sh schemas

install-dev: ## Create .venv with deploysim and dev tools
	$(PYTHON) -m venv $(VENV)
	$(VBIN)/pip install --quiet --upgrade pip
	$(VBIN)/pip install --quiet -e '.[dev]'

lint: ## ruff, mypy --strict, shellcheck
	$(VBIN)/ruff check .
	$(VBIN)/ruff format --check .
	$(VBIN)/mypy
	$(VBIN)/shellcheck scripts/*.sh

fmt: ## Apply ruff fixes and formatting
	$(VBIN)/ruff check --fix .
	$(VBIN)/ruff format .

test-unit: ## Unit tests; no cluster needed
	$(VBIN)/pytest tests/unit

test-e2e: ## End-to-end tests; needs make cluster-up
	$(VBIN)/pytest -m e2e tests/e2e

test: test-unit test-e2e ## All tests

package: ## Build wheel and sdist into dist/
	$(VBIN)/python -m build

clean: ## Remove build, test and lint caches
	rm -rf build dist ./*.egg-info .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -name __pycache__ -type d -not -path './$(VENV)/*' -prune -exec rm -rf {} +
