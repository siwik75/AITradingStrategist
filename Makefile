.PHONY: bootstrap dev test lint typecheck docker-build kustomize-preview clean

# Bootstrap local development environment
bootstrap:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"
	cp -n .env.example .env || true
	@echo ""
	@echo "Bootstrap complete."
	@echo "Activate the virtualenv:  source .venv/bin/activate"
	@echo "Run the smoke test:       make test"

# Start the server in development mode
dev:
	.venv/bin/python main.py --mode server

# Run all tests
test:
	.venv/bin/pytest tests/ -v --tb=short

# Run linters
lint:
	.venv/bin/ruff check .
	.venv/bin/black --check .

# Run type checker
typecheck:
	.venv/bin/mypy agents/ tools/ memory/ config/ workflows/ main.py \
		--ignore-missing-imports --no-strict-optional

# Build Docker image
docker-build:
	docker build -t trading-intelligence-agent:local .

# Preview Kustomize dev overlay (requires kubectl with kustomize support)
kustomize-preview:
	kubectl kustomize manifests/overlays/dev

# Clean generated artifacts
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	find . -name "*.egg-info" -type d -exec rm -rf {} + 2>/dev/null || true
