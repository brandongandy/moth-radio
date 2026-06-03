# scrobble-recs

`scrobble-recs` is a local-first Python application for importing a Last.fm CSV export and building inspectable artist recommendation data in DuckDB.

## Milestone 1

This slice can:

- Validate the Last.fm CSV column contract.
- Import raw scrobbles into DuckDB.
- Normalize artist, album, and track keys.
- Mark exact duplicate scrobbles without deleting them.
- Create provisional local artist entities.
- Build `artist_listening_fact`.
- Show import statistics through the CLI.

No API keys or network access are required.

## Commands

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src
```

```bash
scrobble-recs import --config config.example.json
scrobble-recs doctor --config config.example.json
```

## Verification

Milestone 1 was verified with:

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src
scrobble-recs import --config config.example.json
scrobble-recs doctor --config config.example.json
```
