![](./assets/screenshot.png)

# Moth Radio

## What this is

Moth Radio is a single-listener artist recommendation workbench. It is for getting better recommendations from your own listening history without friends, follows, shared playlists, public profiles, or other social-platform features.

It imports a Last.fm CSV export into DuckDB, builds long-term and recent taste regions, and optionally enriches the local catalog with cache-first Last.fm, MusicBrainz, and ListenBrainz data. Recommendation runs consume persisted local evidence rather than live APIs. Each run records its immutable query context, evidence snapshot, admission decisions, feature values, ranking scores, and slate-policy effects so results can be inspected, exported, replayed, and compared.

The current CLI produces general artist discovery and rediscovery lists. Its source-aware pipeline is designed to accept additional persisted evidence without comparing unrelated providers' raw scores. Acquisition may use the network when you explicitly run enrichment; recommendation generation, evaluation, export, and TUI browsing use local data.

The Python package and command-line tool are named `scrobble-recs`. Your listening history, recommendation runs, feedback, evidence, and API cache stay on your machine.

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
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
scrobble-recs --help
```

PowerShell activation:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
scrobble-recs --help
```

You can use `python -m scrobble_recs` instead if the installed `scrobble-recs` command is not on your `PATH`.

### 2. Create and configure a local project

```bash
scrobble-recs init --config ./config.json
```

`init` refuses to overwrite an existing file; add `--force` only when you intentionally want to replace it. Set `input.lastfm_csv` in `config.json` to your Last.fm CSV export. The generated file documents local paths, API settings, evidence thresholds, and recommendation settings.

You only need `LASTFM_API_KEY` for Last.fm enrichment. MusicBrainz does not require an API key, but you must set `apis.musicbrainz.enabled` to `true` before using its commands and should configure an identifying user agent.

### 3. Import your history

```bash
scrobble-recs import --config ./config.json
scrobble-recs doctor --config ./config.json
```

Import creates local normalized artists, tracks, scrobbles, and diagnostics. `doctor` checks the configuration, database, import quality, cache, and API-key state without requiring network access.

### 4. Enrich persisted evidence (optional, recommended)

Plan first if you want to see the amount of work without making network calls. Last.fm enrichment is cache-first and resumable; it persists similarity edges, artist tags, and tag top-artist data. `--depth 2` also enriches the hop-one frontier and can be bounded with `--max-frontier-artists`.

```bash
export LASTFM_API_KEY=your-key
scrobble-recs enrich-plan --config ./config.json --source lastfm
scrobble-recs enrich --config ./config.json --source lastfm
# Optional broader graph acquisition:
scrobble-recs enrich --config ./config.json --source lastfm --depth 2 --max-frontier-artists 250
```

PowerShell:

```powershell
$env:LASTFM_API_KEY = "your-key"
scrobble-recs enrich-plan --config ./config.json --source lastfm
scrobble-recs enrich --config ./config.json --source lastfm
```

After enabling MusicBrainz in `config.json`, resolve ambiguous local artist identities separately:

```bash
scrobble-recs resolve --config ./config.json --source musicbrainz
```

`enrich --source musicbrainz` currently runs the same bounded identity-resolution workflow. MBIDs remain evidence, not automatic proof that two artists are identical; ambiguous results stay separate.

### 5. Generate and inspect a run

```bash
scrobble-recs recommend --config ./config.json --limit 100
scrobble-recs export --config ./config.json --output recs.csv --format csv
scrobble-recs tui --config ./config.json
```

`recommend` reads the current local evidence snapshot and persists the run, including considered candidates and inspectable evidence/admission/ranking/policy data. It does not call provider APIs.

`export` and the Textual TUI read that persisted run; neither makes live API calls. When a selected recommendation has a persisted Last.fm URL, the TUI provides an **Open on Last.fm** link that opens the artist page in your default browser.

## Operational commands

Use these workflows once the quick start is working.

### Control the listening window and mode

```bash
scrobble-recs recommend --config ./config.json --since 2024-01-01 --until 2025-01-01
scrobble-recs recommend --config ./config.json --mode explore --limit 100
scrobble-recs recommend --config ./config.json --mode rediscovery --limit 100
scrobble-recs recommend --config ./config.json --mode "tag:post-punk,gothic rock"
scrobble-recs recommend --config ./config.json --scorer walk --limit 100
```

Available modes include 

- `balanced`
- `explore`
- `safe`
- `rediscovery`
- `recent`
- `old`
- `anti-obvious`

`--since` and `--until` accept ISO dates or timestamps. `--scorer` accepts `legacy` or `walk`. Recommendation runs retain the query window, evidence snapshot, source-aware admission record, missing signals, raw and calibrated components, policy effects, and serendipity evidence.

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

scrobble-recs enrich-plan --config ./config.json --source musicbrainz
scrobble-recs enrich-status --config ./config.json --source musicbrainz
scrobble-recs enrich-retry --config ./config.json --source musicbrainz

scrobble-recs cache --config ./config.json
scrobble-recs cache-inspect --config ./config.json --source lastfm
scrobble-recs cache-expire --config ./config.json --older-than-days 180
```

`enrich-plan` is local-only. Use `enrich-status` and `enrich-retry` to inspect or reset queued work. Cache commands inspect or expire persisted HTTP responses; `cache-expire` defaults to 180 days and accepts an optional `--source` filter. None of these commands runs as part of recommendation generation.

### Measure optional ListenBrainz evidence

Set `apis.listenbrainz.enabled` to `true` to acquire public aggregate
[ListenBrainz artist-radio data](https://listenbrainz.readthedocs.io/en/latest/users/api/core.html).
Only local seeds with a resolved MusicBrainz artist MBID and identity confidence of at least
0.9 are requested. Provisional and ambiguous identities are skipped. No ListenBrainz account,
username, token, or local listening-history upload is required.

```bash
scrobble-recs enrich-plan --config ./config.json --source listenbrainz
scrobble-recs enrich --config ./config.json --source listenbrainz
scrobble-recs enrich-status --config ./config.json --source listenbrainz
scrobble-recs enrich-retry --config ./config.json --source listenbrainz
scrobble-recs evaluate --config ./config.json --split-date 2024-01-03
scrobble-recs evaluate --config ./config.json --split-date 2024-01-03 --ablate-source listenbrainz
```

ListenBrainz is disabled by default. Its settings default to `mode: "easy"` (`easy`, `medium`,
`hard`), `max_similar_artists: 100`, `max_recordings_per_artist: 3`, `pop_begin: 0`, and
`pop_end: 100`. Limits must be positive and popularity bounds must satisfy
`0 <= pop_begin <= pop_end <= 100`. Requests use `base_url: "https://api.listenbrainz.org/1/"`,
`rate_limit_per_second: 1.0`, `cache_ttl_days: 30`, `max_retries: 2`, and
`timeout_seconds: 20.0`. `stale_cache_on_error: true` permits the last successful response
following exhausted transport, HTTP 429, or server-error retries. Retries wait 1, 2, ... seconds;
other HTTP client errors fail immediately. Remove the obsolete `dump_dir` and
`use_recommendation_api` fields from older configurations.

The local queue uses `artist-radio`; explicit reruns refresh completed work through the cache
and replace memberships for the same seed and request settings. Outstanding queued/running work
must drain before a new refresh cycle starts, so bounded calls eventually process every seed.
Skipped seeds are requeued when planning finds them eligible again. A new synchronous `enrich`
invocation recovers interrupted `running` rows before planning; planning itself leaves active work
untouched. Failed work remains failed until `enrich-retry`. Candidate MBIDs are matched before names;
a conflicting name preserves separate identities with an inspectable warning. Observations preserve provider mode, response
position, bounded recording MBIDs/listen counts, cache key, fetch time, and provider payload hash.
Response position and listen counts are provenance, with no similarity score or source rank.

`candidate_generation_enabled` remains `false`: acquisition adds measurable unranked evidence
without changing production admission, scoring, or calibration. Leave it false until a separately
evaluated fusion challenger is available. Evaluation reports distinct candidate and future-hit
counts per source, unique additions, and the evidence snapshot ID. ListenBrainz coverage measures
its unranked memberships from the run's seeds before the historical cutoff, including candidates
outside the production pool. Source ablation excludes ListenBrainz from those measurements and
retrieval without deleting stored evidence; paired reports over unchanged local evidence have
the same snapshot ID. Freeze enrichment between comparisons. Last.fm-only recommendation output
is preserved when ListenBrainz is disabled, unavailable, or collected with candidate generation off.

### Acquire optional MusicBrainz catalog facts

With `apis.musicbrainz.enabled: true`, `enrich --source musicbrainz` first runs the existing
identity resolver, then acquires aliases and artist relationships for selected local seeds with
`resolved_musicbrainz` status, confidence at least 0.9, valid MBIDs, and no identity conflicts.
`resolve --source musicbrainz` remains identity-only.

```bash
scrobble-recs enrich-plan --config ./config.json --source musicbrainz
scrobble-recs enrich --config ./config.json --source musicbrainz
scrobble-recs enrich-status --config ./config.json --source musicbrainz
scrobble-recs enrich-retry --config ./config.json --source musicbrainz
```

Catalog lookups request `aliases+artist-rels` and use a separate cache identity from resolution.
Fresh cache hits and stale successful fallbacks retain the original fetch time and a hash of the
canonical cached JSON payload. Failed refreshes preserve previous aliases and relationships.
Aliases are scoped to each artist and locale; a missing locale is stored as an empty string.
Related artists are matched only by exact MBID. Shared names and aliases never merge identities;
conflicting names or multiple local MBID matches remain visible in evidence warnings.

`enrichment.seed_artist_limit`, `candidate_artist_limit`, and `queue_batch_size` bound catalog
acquisition. Discovered targets are never recursively requested. Catalog queue work uses the
`artist-catalog` endpoint: pending and recovered work drains before completed refreshes, newly safe
skipped work resumes, and failures require explicit `enrich-retry`. Planning preserves active
requests; a new synchronous worker run recovers interrupted work and returns its unfinished attempt.
Progress and summary counts include requested artists, aliases, relationships, new identities,
and ambiguous relationships.

Recommendation use is separately controlled by `recommendation.catalog_evidence.enabled`, which
is `false` by default. With MusicBrainz enabled and this policy enabled, the initial
`allowed_relationship_types` are `member of band`, `collaboration`, `supporting musician`, and
`instrumental supporting musician`. Other artist relationships remain stored for inspection but
are excluded before candidate introduction and family agreement. The current allow-list is checked
each run, so policy changes do not require reacquisition. Catalog facts provide membership,
explanations, and independent-family reinforcement; their raw score and source rank remain null.
The existing `discovery_evidence.catalog_relationships_enabled` switch applies only to direct
catalog admission in artist contexts; artist querying is not exposed by this CLI yet.

Catalog facts and aliases are supplied by [MusicBrainz](https://musicbrainz.org/).
Recommendation generation and TUI browsing consume persisted local data with no HTTP calls.

### Export a persisted run

```bash
scrobble-recs export --config ./config.json --output recs.csv --format csv
scrobble-recs export --config ./config.json --output recs.jsonl --format jsonl
scrobble-recs export --config ./config.json --output recs.jsonl --format jsonl --run-id RUN_ID --include-candidates
```

Without `--run-id`, export uses the latest persisted run. CSV contains stable scalar query/version fields; JSONL also contains the full query context and nested explanation data. `--include-candidates` includes `candidate_only` rows, not every rejected/excluded diagnostic candidate. JSONL output is strict JSON, including when stored diagnostic data is malformed.

### Browse in the TUI

```bash
scrobble-recs tui --config ./config.json
```

The TUI browses persisted runs. Use in-app help for navigation and control guidance. It records feedback locally and never initiates live API calls while you browse.

### Give feedback

Generally prefer the TUI, but the CLI can record the same local feedback. `--tag` is repeatable, `--value` changes the strength multiplier, and `--note` stores an optional private note.

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
scrobble-recs evaluate --config ./config.json --split-date 2023-01-01 --scorer walk
scrobble-recs evaluate --config ./config.json --split-date 2023-01-01 --ablate serendipity
scrobble-recs evaluate \
  --config ./config.json \
  --train-until 2022-12-31 \
  --test-from 2023-01-01 \
  --test-until 2023-04-01
```

`--split-date` is one shared boundary: training ends there and the outcome horizon begins there. Otherwise provide both `--train-until` and `--test-from`. Without `--test-until`, Moth Radio evaluates the next 90 days. `--scorer` selects `legacy` or `walk`; `--ablate` can disable one supported weight or toggle. Evaluation uses listening, feedback, and provider evidence available at the historical cutoff.

Compare the two scoring engines over exactly the same split:

```bash
scrobble-recs compare \
  --config ./config.json \
  --split-date 2023-01-01 \
  --scorers legacy,walk
```

`compare` accepts the same explicit train/test window options as `evaluate`. Its default scorer list is `legacy,walk`.

Replay several historical cutoffs with a shared outcome horizon:

```bash
scrobble-recs evaluate-rolling \
  --config ./config.json \
  --cutoff 2023-01-01 \
  --cutoff 2023-04-01 \
  --horizon-days 90
```

`--cutoff` is required and repeatable. Cutoffs accept dates or ISO timestamps, normalize to UTC, and are sorted and deduplicated. Each window trains strictly before its cutoff and evaluates first listens in the following `--horizon-days` days (90 by default; must be positive). The command prints each window followed by unweighted mean recall@50, recall@100, ndcg@50, and identity resolution coverage. Each replay persists a local recommendation run and uses that window's historical listening and feedback cutoff.

The Python evaluation results also include candidate and future-hit counts for `lastfm` (seed similarity evidence) and `lastfm_tag` (tag evidence). A candidate with both kinds of evidence counts in both groups, including candidates excluded from the final recommendations. Legacy `candidate_sources` stores seed IDs, so these diagnostics use the persisted seed/tag evidence counts. Identity rates cover all candidate artists: `resolved_musicbrainz` counts as resolved; `ambiguous_musicbrainz` and `provisional_mbid_conflict` count as ambiguous. Provisional MBIDs alone do not establish resolution. Empty candidate populations report zero rates.

## Development

Moth Radio is Python 3.11+, DuckDB, Typer, Textual, Pydantic, and pytest. Install the development extra first. Keep work local-first, deterministic, and inspectable; tests must not call real APIs or depend on a network connection.

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy src
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
