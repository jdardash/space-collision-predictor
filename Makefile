.PHONY: install dev test cov lint serve docker clean benchmark

install:                ## Install production dependencies
	pip install -e .

dev:                    ## Install with dev dependencies
	pip install -e ".[dev]"

test:                   ## Run tests (fail-fast)
	pytest tests/ -x --tb=short

cov:                    ## Run tests with coverage report
	pytest tests/ --cov=src/sda --cov-report=term-missing --cov-report=html

lint:                   ## Lint with ruff and type-check with mypy
	ruff check src/ tests/
	mypy src/sda

serve:                  ## Start the FastAPI dev server
	python -m sda.api

benchmark:              ## Run propagation benchmark
	python benchmarks/propagation_benchmark.py

docker:                 ## Build and run with Docker
	docker compose up --build

clean:                  ## Remove build artifacts
	rm -rf .pytest_cache htmlcov .coverage *.egg-info dist build
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

help:                   ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
