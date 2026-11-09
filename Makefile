# ---------------------------------------------------------------------------
# Makefile — the common tasks, in one place.
#
#   make install-dev   install the package with the dev extras
#   make test          run the whole test suite
#   make lint          run ruff and mypy
#   make run           start the API locally
#   make docker        build and start the whole stack
# ---------------------------------------------------------------------------

.DEFAULT_GOAL := help
.PHONY: help install install-dev test test-unit test-integration coverage \
        lint format typecheck run seed migrate labels demo bench \
        build up down logs clean

PYTHON ?= python3
PIP    ?= $(PYTHON) -m pip

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install the package
	$(PIP) install .

install-dev: ## Install the package with the development extras
	$(PIP) install -e ".[dev]"

test: ## Run the whole test suite
	$(PYTHON) -m pytest tests -q

test-unit: ## Run the unit tests only
	$(PYTHON) -m pytest tests/unit -q

test-integration: ## Run the integration tests only (skipped without DB)
	$(PYTHON) -m pytest tests/integration -q -m integration

coverage: ## Run the tests with the coverage report
	$(PYTHON) -m pytest tests --cov=src/tagmind --cov-report=term-missing --cov-report=html

lint: ## Run ruff
	ruff check src tests

format: ## Format the code with ruff
	ruff format src tests
	ruff check --fix src tests

typecheck: ## Run mypy
	mypy src/tagmind

run: ## Start the API locally
	$(PYTHON) -m tagmind.cli serve --reload

migrate: ## Apply the database migrations
	$(PYTHON) -m tagmind.cli migrate

labels: ## Import the 12-layer label taxonomy seed
	$(PYTHON) -m tagmind.cli seed-labels

seed: ## Load the synthetic dataset
	$(PYTHON) -m tagmind.cli seed --contents 1000 --users 100 --days 30

demo: ## Run the end-to-end demo (label + recommend + evaluate)
	$(PYTHON) -m tagmind.cli demo

bench: ## Run the offline recommendation benchmark (three baselines)
	$(PYTHON) -m tagmind.cli bench

build: ## Build the container image
	docker build -t tagmind:latest .

up: ## Start the whole stack
	docker compose up -d --build

down: ## Stop the stack
	docker compose down

logs: ## Follow the container logs
	docker compose logs -f api worker

clean: ## Remove the build and test artefacts
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache .mypy_cache htmlcov coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
