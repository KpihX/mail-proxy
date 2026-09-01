PKG_NAME      := mail-proxy
PKG_DIR_NAME  := mail_proxy
PKG_DIR       := src/$(PKG_DIR_NAME)
VERSION       := $(shell grep -m 1 version pyproject.toml | tr -s ' ' | tr -d '"' | tr -d "'" | cut -d= -f2 | xargs)

# System Paths
REAL_USER := $(if $(SUDO_USER),$(SUDO_USER),$(USER))
REAL_HOME := $(shell getent passwd $(REAL_USER) | cut -d: -f6)
BIN_DIR   := $(REAL_HOME)/.local/bin

# Tooling
UV     := $(shell command -v uv 2>/dev/null || echo uv)
PYTHON := $(UV) run python
PYTEST := $(PYTHON) -m pytest

PY_FILES := $(shell $(UV) run python -c 'from pathlib import Path; print(" ".join(map(str, Path("$(PKG_DIR)").rglob("*.py"))))')

.PHONY: help check smoke uv-install uv-link uv-uninstall uv-build uv-publish git-push push release git-install-hooks

help: ## Show help
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*##"}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ─── Quality ───

check: smoke ## Run all checks (ruff lint+fix + ruff format + py_compile + pyright + pytest + smoke)
	@$(UV) run ruff check --fix $(PKG_DIR)/
	@$(UV) run ruff format $(PKG_DIR)/
	@$(PYTHON) -m py_compile $(PY_FILES)
	@$(UV) run pyright $(PKG_DIR)/
	@$(PYTHON) -m pytest tests/ -v

smoke: ## Smoke test — CLI + registry integrity (mail-proxy do --help)
	@$(UV) run mail-proxy do --help > /dev/null 2>&1 || (echo "❌ CLI smoke test failed"; exit 1)
	@$(PYTHON) -c "from mail_proxy.actions.registry import REGISTRY as R; assert len(R)==30, len(R); assert len(R)==len(set(R))"
	@echo "✅ CLI smoke test passed"

# ─── Install / Uninstall (uv tool) ───

uv-install: ## Install via uv tool
	@$(UV) tool install . --force
	@echo "✅ $(PKG_NAME) installed"

uv-link: ## Install editable (dev)
	@$(UV) tool install --editable . --force
	@echo "✅ $(PKG_NAME) linked (editable)"

uv-uninstall: ## Uninstall uv tool
	@$(UV) tool uninstall $(PKG_NAME) 2>/dev/null || true
	@echo "✅ $(PKG_NAME) uninstalled"

# ─── Build / Publish ───

uv-build: ## Build Python sdist and wheel
	@echo "🏗️  Building Python package v$(VERSION)..."
	@$(UV) build --clear

uv-publish: uv-build ## Publish to PyPI
	@echo "🚀 Publishing v$(VERSION) to PyPI..."
	@$(UV) publish

# ─── Git ───

git-push: ## Push to both gitlab and github
	@git push github master
	@git push gitlab master
	@echo "✅ Pushed to github + gitlab"

push: git-push ## Alias for git-push

git-install-hooks: ## Install pre-commit hook
	@echo "#!/bin/sh\nmake check" > .git/hooks/pre-commit
	@chmod +x .git/hooks/pre-commit
	@echo "✅ Pre-commit hook installed"

# ─── Release ───

release: check git-push uv-publish ## Full release: check → push → publish
