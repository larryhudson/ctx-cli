# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Python project using uv for dependency management. Source code lives in `src/work_context/`.

## Development Commands

```bash
# Install dependencies
uv sync

# Run the application
uv run python main.py

# Linting and formatting
uv run ruff format .          # Format code
uv run ruff check .           # Lint check
uv run ruff check --fix .     # Lint with auto-fix

# Type checking
uv run ty check               # Check all files
uv run ty check src/file.py   # Check specific file

# Pre-commit hooks (using prek)
prek install                  # Install hooks
prek run --all-files          # Run all checks
```

## Automated Checks

A Claude Code hook runs after every Edit/Write on Python files:
1. Auto-formats with ruff (silent)
2. Reports lint errors (no auto-fix to preserve unused imports during multi-file edits)
3. Reports type errors from ty

Fix any reported errors before proceeding.

## Code Style

- Line length: 100 characters
- Python 3.12+ features encouraged (pyupgrade enforced)
- Use `pathlib.Path` over `os.path` (PTH rules)
- No print statements in production code (T20 rules)
- Import sorting: stdlib → third-party → first-party (`work_context`)
