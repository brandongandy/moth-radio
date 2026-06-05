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

The Python package and CLI are named `scrobble-recs`.

## How To Use It

Install the project with development dependencies:

```bash
python -m pip install -e ".[dev]"
```

If PowerShell says `scrobble-recs` is not recognized after installation, the
console script was installed but its directory is not visible to the current
shell. With `pyenv-win`, regenerate shims:

```powershell
pyenv rehash
scrobble-recs --help
```

You can also run the CLI through the active Python interpreter without relying
on the console-script PATH:

```bash
python -m scrobble_recs --help
```

Edit `config.example.json` or create your own config file with paths for:

- `app.db_path`: the DuckDB database file.
- `app.cache_dir`: local cache storage.
- `app.log_path`: the command log file.
- `input.lastfm_csv`: your Last.fm CSV export.

Import scrobbles:

```bash
scrobble-recs import --config config.example.json
```

The import command prints the configured log path before it starts, shows a
terminal spinner while the CSV import is running, and writes import lifecycle
messages to `app.log_path`.

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
`LASTFM_API_KEY`; cached responses are reused when they are still fresh. The
enrich command warns before long runs and shows phase/item progress for seed
similarity, seed tags, and tag top-artist enrichment while it works.

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

## Recommendations

`recommend` generates a persisted artist recommendation run from local DuckDB
data:

- Recomputes local listening weights, knownness, and rediscovery scores.
- Uses persisted Last.fm similarity edges and tag enrichment as candidate
  evidence.
- Penalizes artists that are already well known or obvious top artists for
  high-affinity tags.
- Applies local feedback from prior recommendation runs.
- Persists inspectable JSON explanations for each recommendation.
- Exports persisted runs to CSV or JSONL.

The command shows phase progress while refreshing local scores, selecting seeds,
building profiles, loading candidates, scoring, reranking, building
explanations, and persisting the run.

```bash
scrobble-recs recommend --config config.example.json --limit 500
scrobble-recs export --config config.example.json --format csv --output recs.csv
scrobble-recs export --config config.example.json --format jsonl --output recs.jsonl
```

`recommend` does not call live APIs. Run `enrich --source lastfm` first to populate the local graph.

## TUI

Launch the local Textual interface against persisted recommendation runs:

```bash
scrobble-recs tui --config config.example.json
```

The TUI reads local DuckDB recommendation data. It does not call live APIs while
you browse. Use `recommend` first to create a run, then use the TUI to browse
ranked artists, inspect explanation details and score breakdowns, switch runs,
apply basic filters, and record feedback with keybindings.

Useful keys:

- `j` / `k`: move through recommendations.
- `e`: show or hide debug score components.
- `/`: toggle the simple search filter.
- `f`: toggle a tag filter from visible rows.
- `R`: switch to another persisted run.
- `s`, `r`, `a`, `o`, `w`, `m`, `l`, `h`: record feedback for the selected artist.

Feedback is stored locally and reranks the current persisted run without live API
calls.

## Recommendation Algorithm

The recommender starts from multiple local taste regions rather than only the
highest-played artists. Seed artists come from lifetime listening, recent
listening, older favorites, underexplored artists, and feedback boosts. Those
seeds pull candidates from the persisted Last.fm similarity graph and tag
tables.

Each candidate receives component scores for Last.fm similarity, tag fit,
rediscovery value, and feedback. Knownness suppresses artists that already have
substantial local listening history, while rediscovery gives lightly played or
dormant artists room to surface. Obviousness uses tag top-artist ranks to
penalize broad "you probably already know this" recommendations without
hard-excluding every popular artist. A diversity reranker keeps the top of the
list from collapsing into one tag or similarity neighborhood.

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

Mode-specific settings adjust seed weighting, knownness tolerance, obviousness
behavior, and diversity pressure. `recommend` reads only local DuckDB data; run
`enrich --source lastfm` when the local graph needs refreshed Last.fm similarity
or tag data.

## Feedback Loop

Feedback is stored locally in DuckDB and can rerank an existing recommendation
run without calling live APIs:

```bash
scrobble-recs feedback \
  --config config.example.json \
  --run-id 00000000-0000-0000-0000-000000000001 \
  --artist-id 42 \
  --action more_like_this \
  --tag post-punk
```

Supported actions are `save`, `reject`, `already_know`, `too_obvious`,
`wrong_genre`, `more_like_this`, `less_like_this`, `hide_artist`, `hide_tag`,
`boost_tag`, `rediscovery_good`, `rediscovery_bad`, `opened_link`,
`played_externally`, and `note`.

By default the command records the event and reranks that persisted run. Use
`--no-rerank` to record feedback only. `recommend` consumes feedback as artist
boosts/suppression, tag boosts/suppression, and lightweight
similarity-neighborhood adjustments.
