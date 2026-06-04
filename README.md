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

## Milestone 2

This slice adds cache-first Last.fm enrichment:

- Select seed artists from persisted local listening facts.
- Fetch Last.fm similar artists and artist tags.
- Fetch Last.fm top artists for discovered tags.
- Persist every raw Last.fm response in `api_cache`.
- Persist normalized similarity, artist tag, and tag top artist rows.
- Resume without repeating normalized enrichment work.

Tests use mocked clients and mocked HTTP. CI must not call the live Last.fm API.

```bash
scrobble-recs enrich --config config.example.json --source lastfm
```

Set `LASTFM_API_KEY` in the environment before running live enrichment.

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
