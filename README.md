# Moth Radio

## What Is This Project?

Moth Radio is a local-first music recommendation tool built from your Last.fm
listening history. It imports a Last.fm CSV export into DuckDB, normalizes the
listening data, and builds inspectable artist recommendation data without making
the app dependent on a remote service.

The project is designed around a few constraints:

- Your listening history and recommendation data stay local.
- API integrations are cache-first and resumable.
- Tests use deterministic fixtures and mocked clients, not live API calls.
- Recommendation evidence is stored in tables and JSON so results can be
  inspected instead of treated as a black box.

The Python package and CLI are currently named `scrobble-recs`.

## How To Use It

Install the project with development dependencies:

```bash
python -m pip install -e ".[dev]"
```

Edit `config.example.json` or create your own config file with paths for:

- `app.db_path`: the DuckDB database file.
- `app.cache_dir`: local cache storage.
- `input.lastfm_csv`: your Last.fm CSV export.

Import scrobbles:

```bash
scrobble-recs import --config config.example.json
```

Check local configuration and imported data:

```bash
scrobble-recs doctor --config config.example.json
```

Run Last.fm enrichment:

```bash
set LASTFM_API_KEY=your-key
scrobble-recs enrich --config config.example.json --source lastfm
```

Use PowerShell syntax on Windows if needed:

```powershell
$env:LASTFM_API_KEY = "your-key"
scrobble-recs enrich --config config.example.json --source lastfm
```

Import and doctor do not require API keys. Live Last.fm enrichment requires
`LASTFM_API_KEY`; cached responses are reused when they are still fresh.

## How To Contribute

Keep changes local-first, testable, and small enough to inspect. Do not add
required Spotify dependencies, live API calls in tests, committed API keys, or
machine-specific local paths.

Run the standard checks before committing:

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src
```

Testing guidelines:

- Use deterministic fixtures under `tests/fixtures`.
- Mock Last.fm, MusicBrainz, and ListenBrainz clients.
- Use temporary DuckDB files in database tests.
- Add behavioral tests for new logic; avoid tests that only prove a file or
  object exists.

When behavior changes, update the relevant docs and `docs/implementation-log.md`
with what changed and how it was verified.
