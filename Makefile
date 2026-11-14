.PHONY: dev test lint format run docker clean

dev:            ## Create .venv and install with dev tools
	python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -e ".[dev]"

test:           ## Run the test suite
	.venv/bin/pytest -q

lint:           ## Check style and common bugs
	.venv/bin/ruff check . && .venv/bin/ruff format --check .

format:         ## Auto-fix style
	.venv/bin/ruff check --fix . && .venv/bin/ruff format .

run:            ## Run the service on http://127.0.0.1:8000
	.venv/bin/face-match serve

docker:         ## Build the Docker image
	docker build -t face-match .

clean:
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache
