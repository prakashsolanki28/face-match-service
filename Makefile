.PHONY: dev test lint format audit run docker clean

# Python 3.11+ is required (patched FastAPI/Starlette/python-multipart need it).
PYTHON ?= $(shell command -v python3.13 || command -v python3.12 || command -v python3.11 || echo python3)

dev:            ## Create .venv and install with dev tools
	$(PYTHON) -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -e ".[dev]"

test:           ## Run the test suite
	.venv/bin/pytest -q

lint:           ## Check style and common bugs
	.venv/bin/ruff check . && .venv/bin/ruff format --check .

audit:          ## Check dependencies for known vulnerabilities
	.venv/bin/pip-audit --skip-editable

format:         ## Auto-fix style
	.venv/bin/ruff check --fix . && .venv/bin/ruff format .

run:            ## Run the service on http://127.0.0.1:8000
	.venv/bin/face-match serve

docker:         ## Build the Docker image
	docker build -t face-match .

clean:          ## Remove build output and caches (keeps .venv, data/ and models)
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache
	find . -name __pycache__ -not -path "./.venv/*" -prune -exec rm -rf {} +
