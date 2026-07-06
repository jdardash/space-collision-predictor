# Contributing

Thanks for your interest in contributing to the Space-Domain Awareness Collision Predictor!

## Getting Started

```bash
git clone https://github.com/jdardash/space-collision-predictor.git
cd space-collision-predictor
python -m venv .venv && source .venv/bin/activate  # .venv\Scripts\activate on Windows
pip install -e ".[dev]"
```

## Development Workflow

1. Create a branch from `master` for your change
2. Write code — follow existing style (ruff enforced)
3. Add or update tests as needed
4. Run the verification suite before submitting:

```bash
pytest tests/ -x --tb=short     # Tests (fail-fast)
mypy src/sda --ignore-missing-imports  # Type checking
ruff check src/ tests/          # Linting
```

## Pull Requests

- Keep PRs focused — one feature or fix per PR
- Include a clear description of *what* and *why*
- Ensure all tests pass and coverage doesn't regress

## Safety-Critical Code

Changes to risk classification thresholds (`conjunction.py:classify_risk`) or propagation constants require extra review. These are safety-critical paths — please flag them explicitly in your PR description.

## Reporting Issues

Open an issue at [GitHub Issues](https://github.com/jdardash/space-collision-predictor/issues) with:
- What you expected vs. what happened
- Steps to reproduce
- Python version and OS

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Please be respectful.
