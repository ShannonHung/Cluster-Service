# CI Pipeline — cluster-service

## Overview

Add a GitHub Actions CI pipeline to `cluster-service` that mirrors the existing `deploy-service` setup: run the full test suite on every PR to main and every push to main.

## Trigger

- `pull_request` targeting `main`
- `push` to `main`

## Job

Single job `test` on `ubuntu-latest`:

1. Checkout code (`actions/checkout@v4`)
2. Install uv with cache enabled (`astral-sh/setup-uv@v3`)
3. Install dependencies including dev group (`uv sync --group dev`)
4. Run tests (`make test` → `APP_ENV=test uv run pytest tests/ -v`)

## File Location

`.github/workflows/test.yml` inside the `cluster-service` repo.

## Differences from deploy-service

- No Redis service container needed (cluster-service has no Redis dependency)
- No e2e skip annotation needed (e2e tests in cluster-service require a real Kubernetes cluster; they are naturally skipped unless the environment provides kubeconfig)

## Out of Scope

- Coverage reporting
- Docker image build
- Deployment steps
