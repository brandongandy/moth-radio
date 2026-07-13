# Moth Radio

Moth Radio is a local-first artist discovery tool for people who want better
recommendations from their own listening history. It imports a Last.fm CSV
export into a local DuckDB database, builds a taste profile, and creates
inspectable recommendation runs you can browse, export, evaluate, and refine.

The package and command-line tool are named `scrobble-recs`.

Your listening history, recommendation runs, feedback, and API cache stay on
your machine. Moth Radio can enrich its local graph from Last.fm when you ask
it to, but imports, persisted recommendations, exports, the TUI, feedback, and
evaluation do not require a live service.

## Algorithms

Moth Radio is designed to make its recommendations explainable rather than
mysterious. Every recommendation run records its inputs, eligibility decision,
score components, and reranking evidence in DuckDB.

### A bounded listening profile

Recommendations start from a configurable listening-history window: 12 months
by default. Use `--since` and `--until` when you want an explicit half-open
window (`[since, until)`). This keeps recent taste useful without treating your
entire history as one permanent preference.

### Direct discovery evidence

Tags alone cannot admit an artist. A discovery candidate must either have
support from at least two distinct seed artists or have one strong seed edge
reinforced by tag affinity. This prevents broad tag charts from overwhelming
the local graph.

### Calibrated relevance and penalties

Positive Last.fm similarity, tag affinity, and feedback components are
calibrated across the eligible candidates in a run before they are combined.
Knownness, obviousness, fatigue, and negative feedback remain penalties. The
result is a ranking based on relative strength in this run, not an accidental
comparison of incompatible raw scales.

### Serendipity without randomness

Moth Radio builds a small graph from the selected seeds. Candidates that connect
otherwise separate taste regions can receive a bounded serendipity bonus, while
deeply clustered candidates receive less. A candidate still needs direct
evidence before it can receive that bonus.

### Feedback and evaluation

`hide_artist` is a permanent local exclusion. Other recommendation votes have
a configurable cooldown (30 days by default), so recently saved, rejected, or
otherwise rated artists do not immediately return.

The `evaluate` command uses a pre-cutoff historical view for seeds, local
history, feedback, cooldowns, and recommendation fatigue. Outcomes are first
listens in a later half-open test horizon. Its metrics include precision@50,
candidate coverage, novelty, and cooldown/repeat rate.

## Quick start

### 1. Install

```bash
python -m pip install -e ".[dev]"
python -m scrobble_recs --help
```

If the installed `scrobble-recs` command is on your `PATH`, you can use it in
place of `python -m scrobble_recs` throughout this guide.

### 2. Create and configure a local project

```bash
scrobble-recs init --config ./config.json
```

Set `input.lastfm_csv` in `config.json` to your Last.fm CSV export. The example
configuration documents the available local paths and recommendation settings;
use it as a reference when editing the generated file.

You only need `LASTFM_API_KEY` when you choose to enrich the local graph from
Last.fm. Do not put a real key in a committed config file.

### 3. Import your history

```bash
scrobble-recs import --config ./config.json
scrobble-recs doctor --config ./config.json
```

Import creates local normalized artists, tracks, scrobbles, and diagnostics.
`doctor` checks the configuration, database, import quality, cache, and API-key
state without requiring network access.

### 4. Enrich the local graph (optional, recommended)

```bash
export LASTFM_API_KEY=your-key
scrobble-recs enrich --config ./config.json --source lastfm
```

PowerShell:

```powershell
$env:LASTFM_API_KEY = "your-key"
scrobble-recs enrich --config ./config.json --source lastfm
```

Enrichment is cache-first and resumable. It adds persisted similarity edges,
artist tags, and tag top-artist data; recommendation generation itself remains
local.

### 5. Generate and inspect a run

```bash
scrobble-recs recommend --config ./config.json --limit 100
scrobble-recs export --config ./config.json --format csv --output recs.csv
scrobble-recs tui --config ./config.json
```

`recommend` persists a run. `export` and the Textual TUI read that persisted
run; neither makes live API calls.

## Operational commands

Use these workflows once the quick start is working.

### Control the listening window and mode

```bash
scrobble-recs recommend --config ./config.json --since 2024-01-01 --until 2025-01-01
scrobble-recs recommend --config ./config.json --mode explore --limit 100
scrobble-recs recommend --config ./config.json --mode rediscovery --limit 100
scrobble-recs recommend --config ./config.json --mode "tag:post-punk,gothic rock"
```

Available modes include `balanced`, `explore`, `safe`, `rediscovery`, `recent`,
`old`, and `anti-obvious`. Recommendation explanations retain the raw and
calibrated score components, quality-gate reasons, history window, and
serendipity evidence.

### Inspect and rebuild your local profile

```bash
scrobble-recs build-profile --config ./config.json
scrobble-recs profile-tags --config ./config.json
```

This refreshes local listening/profile data and shows the weighted tags that
currently shape your taste profile.

### Plan and manage enrichment

```bash
scrobble-recs enrich-plan --config ./config.json --source lastfm
scrobble-recs enrich-status --config ./config.json --source lastfm
scrobble-recs enrich-retry --config ./config.json --source lastfm

scrobble-recs cache --config ./config.json
scrobble-recs cache-inspect --config ./config.json --source lastfm
scrobble-recs cache-expire --config ./config.json --older-than-days 180
```

Use these commands to inspect queued work, retry failures, and keep the local
API cache healthy without treating network calls as part of the recommendation
loop.

### Export a persisted run

```bash
scrobble-recs export --config ./config.json --format csv --output recs.csv
scrobble-recs export --config ./config.json --format jsonl --output recs.jsonl
scrobble-recs export --config ./config.json --run-id RUN_ID --include-candidates
```

Exports include history-window, quality-gate, serendipity, and explanation
fields. JSONL output is strict JSON, including when stored diagnostic data is
malformed.

### Browse in the TUI

```bash
scrobble-recs tui --config ./config.json
```

The TUI browses persisted runs. Use `j`/`k` to move, `?` for help, `e` to show
score components, `/` to search, and `R` to switch runs. It records feedback
locally and never initiates live API calls while you browse.

### Give feedback

```bash
scrobble-recs feedback \
  --config ./config.json \
  --run-id RUN_ID \
  --artist-id 42 \
  --action more_like_this \
  --tag post-punk
```

Useful actions include `save`, `reject`, `already_know`, `too_obvious`,
`wrong_genre`, `more_like_this`, `less_like_this`, `hide_artist`, `hide_tag`,
and `boost_tag`. Feedback reranks the selected persisted run by default; pass
`--no-rerank` to record it only.

### Evaluate historical quality

```bash
scrobble-recs evaluate --config ./config.json --split-date 2023-01-01
scrobble-recs evaluate \
  --config ./config.json \
  --train-until 2022-12-31 \
  --test-from 2023-01-01 \
  --test-until 2023-04-01
```

`--split-date` is one shared boundary: training ends there and the outcome
horizon begins there. Without `--test-until`, Moth Radio evaluates the next 90
days. Evaluation uses only the state available before the training cutoff.

## Development

Moth Radio is Python, DuckDB, Typer, Textual, Pydantic, and pytest. Keep work
local-first, deterministic, and inspectable. Tests must not call real APIs or
depend on a network connection.

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src
```

Use deterministic fixtures, temporary DuckDB databases, and behavioral tests.
Do not commit API keys or machine-specific paths.
