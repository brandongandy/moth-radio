![](./assets/screenshot.png)

# Moth Radio

Moth Radio is a local-first artist discovery tool for people who want better recommendations from their own listening history. 

It imports a Last.fm CSV export into a local DuckDB database, builds a taste profile, and generates recommendations you can browse, export, evaluate, and refine.

The package and command-line tool are named `scrobble-recs`.

Your listening history, recommendation runs, feedback, and API cache stay on your machine. Moth Radio can enrich its local graph from Last.fm and MusicBrainz when you ask it to, but all other aspects of the tool do not require a live service.

**AI Disclosure:** This is a purpose-built tool for my own uses, designed by me and written entirely by AI. DeepSeek and Codex were used in the development of this tool.

## Algorithms

Moth Radio is a local, graph-based recommender. In essence, it works like this:

Listening history → seed artists → graph/tag candidates → eligibility gate → calibrated scoring → novelty penalties/bonuses → diversity rerank → persisted run

When a recommendation is generated, the inputs, eligibility decision, score components, and reranking evidence are also persisted so you can see the criteria behind the recommendation.

See the Appendix for a full overview of the recommendation algorithm.

## Quick start

### 0. Get your Last.fm History

You need a CSV export of your Last.fm scrobble history. Use a tool like [this one](https://lastfm.ghan.nl/export/) to obtain the file. 

### 1. Install

```bash
python -m pip install -e ".[dev]"
python -m scrobble_recs --help
```

If the installed `scrobble-recs` command is on your `PATH`, you can use it in place of `python -m scrobble_recs` throughout this guide.

### 2. Create and configure a local project

```bash
scrobble-recs init --config ./config.json
```

Set `input.lastfm_csv` in `config.json` to your Last.fm CSV export. The example configuration documents the available local paths and recommendation settings; use it as a reference when editing the generated file.

You only need `LASTFM_API_KEY` when you choose to enrich the local graph from Last.fm.

### 3. Import your history

```bash
scrobble-recs import --config ./config.json
scrobble-recs doctor --config ./config.json
```

Import creates local normalized artists, tracks, scrobbles, and diagnostics. `doctor` checks the configuration, database, import quality, cache, and API-key state without requiring network access.

### 4. Enrich the local graph (optional, recommended)

Enrichment is cache-first and resumable. It adds persisted similarity edges, artist tags, and tag top-artist data; recommendation generation itself remains local.

```bash
export LASTFM_API_KEY=your-key
scrobble-recs enrich --config ./config.json --source lastfm
```

PowerShell:

```powershell
$env:LASTFM_API_KEY = "your-key"
scrobble-recs enrich --config ./config.json --source lastfm
```

### 5. Generate and inspect a run

```bash
scrobble-recs recommend --config ./config.json --limit 100
scrobble-recs export --config ./config.json --format csv --output recs.csv
scrobble-recs tui --config ./config.json
```

`recommend` persists a run. 

`export` and the Textual TUI read that persisted run; neither makes live API calls. When a selected recommendation has a persisted Last.fm URL, the TUI provides an **Open on Last.fm** link that opens the artist page in your default browser.

## Operational commands

Use these workflows once the quick start is working.

### Control the listening window and mode

```bash
scrobble-recs recommend --config ./config.json --since 2024-01-01 --until 2025-01-01
scrobble-recs recommend --config ./config.json --mode explore --limit 100
scrobble-recs recommend --config ./config.json --mode rediscovery --limit 100
scrobble-recs recommend --config ./config.json --mode "tag:post-punk,gothic rock"
```

Available modes include 

- `balanced`
- `explore`
- `safe`
- `rediscovery`
- `recent`
- `old`
- `anti-obvious`

Recommendation explanations retain the raw and calibrated score components, quality-gate reasons, history window, and serendipity evidence.

### Inspect and rebuild your local profile

```bash
scrobble-recs build-profile --config ./config.json
scrobble-recs profile-tags --config ./config.json
```

This refreshes local listening/profile data and shows the weighted tags that currently shape your taste profile.

### Plan and manage enrichment

```bash
scrobble-recs enrich-plan --config ./config.json --source lastfm
scrobble-recs enrich-status --config ./config.json --source lastfm
scrobble-recs enrich-retry --config ./config.json --source lastfm

scrobble-recs cache --config ./config.json
scrobble-recs cache-inspect --config ./config.json --source lastfm
scrobble-recs cache-expire --config ./config.json --older-than-days 180
```

Use these commands to inspect queued work, retry failures, and keep the local API cache healthy without treating network calls as part of the recommendation loop.

### Export a persisted run

```bash
scrobble-recs export --config ./config.json --format csv --output recs.csv
scrobble-recs export --config ./config.json --format jsonl --output recs.jsonl
scrobble-recs export --config ./config.json --run-id RUN_ID --include-candidates
```

Exports include history-window, quality-gate, serendipity, and explanation fields. JSONL output is strict JSON, including when stored diagnostic data is malformed.

### Browse in the TUI

```bash
scrobble-recs tui --config ./config.json
```

The TUI browses persisted runs. Use in-app help for navigation and control guidance. It records feedback locally and never initiates live API calls while you browse.

### Give feedback

Generally prefer the TUI for this, but if you want to, you can call the CLI directly for feedback.

```bash
scrobble-recs feedback \
  --config ./config.json \
  --run-id RUN_ID \
  --artist-id 42 \
  --action more_like_this \
  --tag post-punk
```

Useful actions include `save`, `reject`, `already_know`, `too_obvious`, `wrong_genre`, `more_like_this`, `less_like_this`, `hide_artist`, `hide_tag`, and `boost_tag`. 

Feedback reranks the selected persisted run by default; pass `--no-rerank` to record it only.

### Evaluate historical quality

```bash
scrobble-recs evaluate --config ./config.json --split-date 2023-01-01
scrobble-recs evaluate \
  --config ./config.json \
  --train-until 2022-12-31 \
  --test-from 2023-01-01 \
  --test-until 2023-04-01
```

`--split-date` is one shared boundary: training ends there and the outcome horizon begins there. Without `--test-until`, Moth Radio evaluates the next 90 days. Evaluation uses only the state available before the training cutoff.

## Development

Moth Radio is Python, DuckDB, Typer, Textual, Pydantic, and pytest. Keep work local-first, deterministic, and inspectable. Tests must not call real APIs or depend on a network connection. 

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src
```

Use deterministic fixtures, temporary DuckDB databases, and behavioral tests. Do not commit API keys or machine-specific paths.

## Appendix: Recommendation Algorithm

Moth Radio is a local, graph-based recommender. Each `recommend` run computes candidates and ranks them from your listening history plus the persisted Last.fm graph.

```text
Listening history → seed artists → graph/tag candidates → eligibility gate
→ calibrated scoring → novelty penalties/bonuses → diversity rerank → persisted run
```

### 1. Seed selection

The default balanced mode builds a mix of seed artists from your recent listening, lifetime favorites, old obsessions, and underexplored artists. A seed's listening weight combines:

- `log(1 + play count)`
- track diversity
- number of active listening months
- a recency decay

It normally uses a 12-month history window. Modes change the mix: `explore` favors underexplored artists, `safe` favors lifetime favorites, `rediscovery` favors dormant former favorites, and so on.

### 2. Candidate generation

Candidates are the union of:

- artists reached by persisted Last.fm “similar artist” edges from seeds;
- artists in persisted top-artist lists for tags connected to your seeds.

Normal discovery excludes artists you have already played, seed artists themselves, hidden artists, and artists in the recent feedback cooldown.

### 3. Discovery evidence gate

A candidate must have direct graph evidence. With the current defaults, it passes if either:

- at least two distinct seeds have an edge to it of at least `0.05`; or
- one strong seed edge of at least `0.30` is reinforced by tag similarity of at least `0.05`.

This deliberately prevents tag-only recommendations.

### 4. Scoring

For eligible candidates, the main signals are:

- **Last.fm similarity:** weighted average of the candidate's edge strengths from your seeds.
- **Tag similarity:** cosine similarity between your seed-derived tag profile and the candidate's tags.
- **Feedback:** direct artist feedback, feedback-derived tag preferences, and similarity-neighborhood effects.
- **Rediscovery:** only in rediscovery mode; favors artists with 1–20 plays, dormant for at least two years, and not highly known.

Positive similarity/tag/feedback values are then percentile-calibrated within the candidate set, so the run cares more about relative strength than raw Last.fm scale.

With the default config, the base score is:

```text
0.35 × calibrated Last.fm similarity
+ 0.25 × calibrated tag similarity
+ 0.10 × rediscovery
+ 0.10 × feedback
```

Then it subtracts:

- excessive knownness above the mode's threshold;
- obviousness: artists ranked highly in your strongest tags;
- recommendation fatigue from the last five runs;
- a small balanced-mode penalty for shallow prior listening.

It multiplies the result by identity confidence, then adds a serendipity bonus.

### 5. Serendipity

A candidate gets up to `0.15` extra when it connects several of your seeds that are themselves weakly connected, or are in separate connected components. In plain terms: it rewards a credible bridge between different parts of your taste, rather than merely another artist in an already dense cluster.

### 6. Diversity rerank

The final selection uses a greedy MMR-style reranker:

```text
rerank score = λ × candidate score
             − (1 − λ) × similarity to already selected results
```

With `λ = 0.75`, relevance dominates but repetition is penalized. It also caps a primary tag at four results in the first 20 recommendations.

One important implementation detail: `personal_cluster_fit` and `metadata_graph` still exist in config but are not currently contributors to the ranking formula. They are legacy/inert weights, not active algorithms.