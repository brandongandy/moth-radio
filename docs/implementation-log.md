# Implementation Log

## 2026-06-02: Milestone 1 import and inspect

Changed:

- Added Python project scaffold and CLI entry point.
- Added JSON config loading with path expansion and environment-variable interpolation.
- Added DuckDB schema for raw scrobbles, clean scrobbles, provisional artists, and artist listening facts.
- Added Last.fm CSV import with exact column validation.
- Added normalized artist, album, and track keys.
- Added exact duplicate marking without deleting raw or clean rows.
- Added `import` and `doctor` commands.
- Added deterministic tests and fixture data.

Verified:

- `python -m pytest` -> 25 passed in 1.79s
- `python -m ruff check .` -> All checks passed
- `python -m ruff format --check .` -> 13 files already formatted
- `python -m mypy src` -> Success, no issues found in 9 source files
- `scrobble-recs import --config config.example.json` -> Imported 6 scrobbles
- `scrobble-recs doctor --config config.example.json` -> Config: ok; Database: ok; CSV: ok; API keys: not required for import/doctor
