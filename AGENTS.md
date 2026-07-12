# AGENTS.md

## Project

This repository implements `scrobble-recs`, a local-first Python application for generating artist recommendations from a Last.fm CSV export.

The application uses:

- Python
- DuckDB
- Textual for the TUI
- Typer for the CLI
- Pydantic for config validation
- pytest for tests

## Core constraints

- Keep the app local-first.
- Do not require network access for tests.
- Do not call real APIs in tests.
- Do not use Spotify as a required dependency.
- Treat MBIDs in the CSV as hints, not guaranteed truth.
- Prefer small, testable vertical slices over large unverified implementations.
- Keep API clients cache-first.
- TUI interactions must operate on persisted local recommendation runs, not live API calls.
- When executing an approved multi-task plan, keep future-task findings in their assigned task. Do not move them earlier solely because intermediate code could be observed; note them and resume the approved order. Only change task order when explicitly authorized.

## Commands

Use these commands when available:

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src
```

If a tool is not configured yet, add the minimal config needed or document why it is skipped.

## Done means

A task is complete only when:

1. The requested behavior is implemented.
2. Tests exist for new logic.
3. The test suite passes.
4. The README or docs are updated if behavior changed.
5. No real API keys or local user paths are committed.
6. The implementation log is updated with what changed and how it was verified.

## Testing rules

* Use fixtures under `tests/fixtures`.
* Use deterministic data.
* Mock Last.fm, MusicBrainz, and ListenBrainz clients.
* Never depend on live API responses in CI.
* DuckDB tests should use temporary database files.

## Style

* Keep functions small.
* Keep database schema changes in migration files.
* Prefer explicit data classes or Pydantic models at module boundaries.
* Avoid hiding core recommender math behind opaque abstractions.
* Explanations should be inspectable JSON, not only formatted strings.
