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

## Milestone 3

This slice adds the first persisted recommender:

- Recomputes local listening weights, knownness, and rediscovery scores.
- Generates recommendations from persisted Last.fm similarity and tag enrichment.
- Penalizes known and obvious top-tag artists.
- Persists recommendation runs with inspectable JSON explanations.
- Exports persisted runs to CSV or JSONL.

```bash
scrobble-recs recommend --config config.example.json --limit 500
scrobble-recs export --config config.example.json --format csv --output recs.csv
scrobble-recs export --config config.example.json --format jsonl --output recs.jsonl
```

`recommend` does not call live APIs. Run `enrich --source lastfm` first to populate the local graph.

## Recommender Quality Pass

`recommend` supports mode-specific ranking from local data:

```bash
scrobble-recs recommend --config config.example.json --mode balanced --limit 500
scrobble-recs recommend --config config.example.json --mode explore --limit 500
scrobble-recs recommend --config config.example.json --mode safe --limit 500
scrobble-recs recommend --config config.example.json --mode rediscovery --limit 500
scrobble-recs recommend --config config.example.json --mode recent --limit 500
scrobble-recs recommend --config config.example.json --mode old --limit 500
scrobble-recs recommend --config config.example.json --mode anti-obvious --limit 500
scrobble-recs recommend --config config.example.json --mode "tag:post-punk,gothic rock" --limit 500
```

The recommender now selects seeds from multiple local taste regions, applies
mode-specific knownness and obviousness behavior, hides the most obvious
high-affinity top-tag artists unless they are rediscovery candidates, and
reranks the persisted list for diversity. It still reads only local DuckDB data
during `recommend`; run `enrich --source lastfm` first when the local graph needs
new Last.fm similarity or tag data.
