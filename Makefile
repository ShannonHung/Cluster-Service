# ─── Cluster Service Makefile ────────────────────────────────────────────────
# 前置條件：uv 已安裝 (curl -LsSf https://astral.sh/uv/install.sh | sh)

UV     := uv
PYTHON := $(UV) run python
PYTEST := $(UV) run pytest

.DEFAULT_GOAL := help

# ─── Help ────────────────────────────────────────────────────────────────────
.PHONY: help
help:
	@echo ""
	@echo "  Cluster Service — 常用指令"
	@echo ""
	@echo "  make install      安裝所有依賴（含 dev）到 .venv/"
	@echo "  make start        啟動伺服器（使用 .env，不帶 APP_ENV）"
	@echo "  make dev          啟動開發伺服器（使用 .env + .env.dev，熱重載）"
	@echo "  make prod         啟動生產伺服器（使用 .env + .env.prod）"
	@echo ""
	@echo "  make test         執行全部測試 (不含 e2e)"
	@echo "  make test-e2e     執行 e2e 測試 (需要 k8s 叢集)"
	@echo "  make test-unit    只執行 unit tests"
	@echo "  make test-int     只執行 integration tests"
	@echo "  make test-cov     執行全部測試並顯示覆蓋率"
	@echo ""
	@echo "  make hash p=<密碼>  快速 hash 一個密碼"
	@echo "  make check-users [f=<檔案>]  檢查 users 檔（帳號 + access policy）格式"
	@echo "  make clean        刪除 .venv、快取"
	@echo ""

# ─── Setup ───────────────────────────────────────────────────────────────────
.PHONY: install
install:
	$(UV) sync --group dev

# ─── Run ─────────────────────────────────────────────────────────────────────
# start: 只讀 .env（不設 APP_ENV，適合生產部署只有單一 .env 的情境）
.PHONY: start
start:
	$(UV) run uvicorn app.main:app --host 0.0.0.0 --port 8000

# dev: 讀 .env + .env.dev（.env.dev 的值會覆蓋 .env）
.PHONY: dev
dev:
	APP_ENV=dev $(UV) run uvicorn app.main:app --reload --port 8000

# prod: 讀 .env + .env.prod（.env.prod 的值會覆蓋 .env）
.PHONY: prod
prod:
	APP_ENV=prod $(UV) run uvicorn app.main:app --host 0.0.0.0 --port 8000

# ─── Test ────────────────────────────────────────────────────────────────────
.PHONY: test
test:
	APP_ENV=test $(PYTEST) tests/ -v -m 'not e2e'

.PHONY: test-e2e
test-e2e:
	APP_ENV=test $(PYTEST) tests/e2e/ -v -m e2e

.PHONY: test-unit
test-unit:
	APP_ENV=test $(PYTEST) tests/unit/ -v

.PHONY: test-int
test-int:
	APP_ENV=test $(PYTEST) tests/integration/ -v

.PHONY: test-cov
test-cov:
	APP_ENV=test $(PYTEST) tests/ -v --tb=short \
		--cov=app --cov-report=term-missing

# ─── Utils ───────────────────────────────────────────────────────────────────
# 用法：make hash p=yourpassword
.PHONY: hash
hash:
	@$(PYTHON) -c \
		"import bcrypt; print(bcrypt.hashpw('$(p)'.encode(), bcrypt.gensalt()).decode())"

# Validate a users file (accounts + access policy) before deploying it.
# Usage: make check-users              (data/users.json)
#        make check-users f=path.json
.PHONY: check-users
check-users:
	@APP_ENV=$${APP_ENV:-dev} $(PYTHON) -m app.check_users $(or $(f),data/users.json)

.PHONY: clean
clean:
	rm -rf .venv .pytest_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "清理完成。"
