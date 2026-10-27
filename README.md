![](./assets/screenshot.png)

# Moth Radio

## What this is

Moth Radio is a single-listener artist recommendation workbench. It is for getting better recommendations from your own listening history without friends, follows, shared playlists, public profiles, or other social-platform features.

It imports a Last.fm CSV export into DuckDB, builds long-term and recent taste regions, and optionally enriches the local catalog with cache-first Last.fm, MusicBrainz, and ListenBrainz data. Recommendation runs consume persisted local evidence rather than live APIs. Each run records its immutable query context, evidence snapshot, admission decisions, feature values, ranking scores, and slate-policy effects so results can be inspected, exported, replayed, and compared.

The current CLI produces general discovery, artist-conditioned continuation, and rediscovery lists. Its source-aware pipeline is designed to accept additional persisted evidence without comparing unrelated providers' raw scores. Acquisition may use the network when you explicitly run enrichment; recommendation generation, evaluation, export, and TUI browsing use local data.

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

`enrich --source musicbrainz` resolves identities, then acquires bounded catalog facts and aliases. MBIDs remain evidence, not automatic proof that two artists are identical; ambiguous results stay separate.

### 5. Generate and inspect a run

```bash
scrobble-recs recommend --config ./config.json --limit 100
scrobble-recs export --config ./config.json --output recs.csv --format csv
scrobble-recs tui --config ./config.json
```

To explore an artist's neighborhood using the same persisted-data pipeline:

```bash
scrobble-recs recommend --config ./config.json --artist "The Cure" --limit 25
scrobble-recs recommend --config ./config.json --artist 42 --mode explore
```

`--artist` resolves an exact local MusicBrainz artist MBID first, then a positive numeric artist ID,
then a normalized exact canonical name. Ambiguous matches list IDs and names; missing matches fail
without constructing a network client. Aliases and fuzzy names are not lookup alternatives. The CLI
prints the target ID, canonical name, and relationship state from the persisted query context.

Artist retrieval first bounds the neighborhood to target-origin Last.fm edges and walks, shared
semantic facets, enabled target-origin ListenBrainz memberships, and explicitly enabled catalog
relationships. Personal taste and feedback rerank only that pool. Global chart placement or personal
positive feedback alone cannot introduce an unrelated artist. Sparse neighborhoods return fewer
results. Safe known neighbors are labeled `known`, unheard neighbors `new`. The default control uses the
existing scorer and its knownness, trial-listen, fatigue, and diversity policies; deeply known
neighbors receive the existing knownness preference reduction. Modes,
including `rediscovery`, remain available. Hides, cooldowns, explicit negative artist feedback, and
identities below the safety threshold remain excluded. Provider membership scores/ranks stay null.

The immutable `artist-relationship-v1` snapshot counts duplicate-free scrobbles in the requested
half-open history window, strictly before the as-of/history/feedback cutoff. It records counts,
unique nonempty track/release keys, UTC active days/months, first/last listen, span, feedback, and
prior persisted run appearances (`exposure_source=run_appearance`). Missing track keys count as
individual singleton replay groups and stay in the play denominator. Recency-weighted plays sum
the existing `exp(-days/400)` profile decay. Recent trend compares actual available recent
(up to 90 days) and earlier daily rates: both zero gives 0; recent evidence without an earlier
rate gives finite 1. Release keys are local album keys, not claims of resolved catalog identity.

Initial configurable `recommendation.artist_relationship` thresholds are `sampled_max_plays=5`,
`established_min_plays=20`, `established_min_active_months=3`, and `dormant_days=730`. Zero plays is
`unheard`; both established thresholds plus at least 730 inactive days is `dormant_favorite`;
both established thresholds otherwise is `established_favorite`; all other heard histories remain
conservatively `sampled`. The explicit low-depth sampled band is persisted separately. Short history
windows can therefore omit a dormant relationship; extend `--since` when that history is relevant.

The default `artist-control-v1` profile uses the selected existing deterministic scorer (`legacy`
or `walk`) and its configured component weights, mode multipliers, calibration, identity scaling,
serendipity, and slate policy over the explicit target pool. Walk retrieval uses the existing
`walk.max_hops` bound. Relationship state is recorded but does not silently activate new numerical
controls. Unranked radio/catalog membership alone receives no positive relevance prior; it can
remain a retrieved diagnostic candidate with zero relevance and no selected result. Finite semantic
or other existing scoring evidence can still support that neighbor. Invalid Last.fm similarity rows
are omitted consistently from scoring, features, and provenance; run diagnostics retain their IDs
and string-valued rejection reasons without serializing NaN/Infinity.

An experimental, reversible profile is available through
`recommendation.artist_challenger.enabled=true`, which is **false by default**. The validated,
versioned `artist-challenger-v1` configuration contains all additional tunables: target/personal/
existing relevance weights (initially 0.75/0.15/0.10, nonnegative and summing to one), structural
membership prior (0.25), hop-proximity scale (1.0, divided by `hop+1`), known-neighbor reduction
(0.12), and per-state radius/popularity/obviousness controls. Initial unheard/sampled/established/
dormant radii are 1/1/2/2, bounded by `walk.max_hops`; popularity preferences are +0.10/0/-0.10/-0.05
and obviousness weights are 0.05/0.10/0.20/0.15. These values are unvalidated challenger settings,
not production defaults. Use an explicitly configured file for local evaluation, for example:

```bash
scrobble-recs evaluate-rolling --config ./artist-challenger.json --task artist \
  --cutoff 2024-01-01 --horizon-days 90
```

Each run persists the active control/challenger profile, actual settings, existing scorer weights,
and target/personal/existing relevance contributions. Popularity, novelty, and other slate effects
remain outside relevance: candidate policy JSON records each signed adjustment in final-score units,
including diversity, clipping and exclusion, so `relevance + sum(policy components) = final score`.
Challenger popularity/obviousness effects honor `obviousness.enabled`, and its additional known-neighbor
reduction honors `knownness.enabled`. Disabling the challenger restores the existing control;
activation is explicit and no promotion happens automatically. Changing the default requires the
comparison evidence specified by the design; the Task 8 fusion challenger below is a separate explicit opt-in. Exposure and full promotion workflows remain deferred.

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

`--since` and `--until` accept ISO dates or timestamps. `--scorer` accepts `legacy`, `walk`, or the explicit `fusion` challenger. Recommendation runs retain the query window, evidence snapshot, source-aware admission record, missing signals, raw and calibrated components, policy effects, and serendipity evidence.

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
catalog admission in artist contexts. Enable it as well as `catalog_evidence.enabled` to let
a target-origin catalog relationship introduce an artist-query candidate. Membership alone
does not supply a positive default relevance score.

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

`--split-date` is one shared boundary: training ends there and the outcome horizon begins there. Otherwise provide both `--train-until` and `--test-from`. Without `--test-until`, Moth Radio evaluates the next 90 days. `--scorer` selects `legacy`, `walk`, or `fusion`; `--ablate` can disable one supported weight or toggle. Evaluation uses listening, feedback, and provider evidence available at the historical cutoff.

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

Add `--task artist` for separate artist-conditioned replay (`--task general` is the default):

```bash
scrobble-recs evaluate-rolling --config ./config.json --task artist \
  --cutoff 2024-01-01 --cutoff 2024-04-01 --horizon-days 90
```

At each cutoff, eligible targets have at least `min_seed_scrobbles` duplicate-free plays inside
that cutoff's configured profile window. Targets run in ascending local artist-ID order, one
persisted run per target. Artist reports identify each target, report distinct target count and
run count, and average over target-window runs. Empty-target windows still count as windows but
create no runs; an entirely empty target population has zero means. General reports retain their
existing window denominator and metrics; artist means never include general runs. Artist results
also report local horizon-only graded adoption NDCG: a newly heard artist earns 1 for a first
listen, 2 for repeat plays, or 3 for listening on multiple UTC days, with gain `2**grade - 1`.
Duplicates, pre-cutoff known artists, and plays outside the horizon earn no adoption credit.
This retrospective result is supplementary: history does not establish discovery through the target.

Evaluation preserves the legacy all-row `recall_at_50`, `recall_at_100`, `ndcg_at_50`, and
`tag_diversity_at_20` fields. Separately named `selected_slate_recall_at_50`,
`selected_slate_ndcg_at_50`, and `selected_slate_tag_diversity_at_20` exclude `candidate_only`
rows before top-k. Single-window and rolling CLI output include these fields, and rolling
reports include their means over the same window/target denominator as the other metrics.

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

### Optional source-family fusion challenger

Generated config keeps `scoring.engine="legacy"` and `fusion.enabled=false`. To select the
reversible challenger for either general or artist recommendations, use `--scorer fusion` or
set both `recommendation.scoring.engine="fusion"` and `recommendation.fusion.enabled=true`.
Selecting fusion in a config without enabling it fails clearly. Task 7's numerical artist
challenger remains independently disabled; fusion uses the same target-bounded artist pool.

Fusion creates three query-local lists after the pool is fixed:

- `lastfm_similarity`: sum of each seed's effective IDF-adjusted weight times its persisted
  normalized edge score, divided by total effective seed weight;
- `lastfm_walk`: descending finite walk lift for reachable neighbors;
- `semantic`: descending cosine overlap of the query tag profile and candidate tag vector.

Each list breaks ties by artist ID. Weighted RRF is exactly `weight / (60 + rank)` by default;
confidence never multiplies it. All three initial source weights are `1.0`. Direct Last.fm
and its walk share the collaborative family; semantic is independent. Contributions sum within
each family before the initial cap of `1/61` per family. The caps are an explicit bounded
baseline, not measured tuned weights. ListenBrainz radio and MusicBrainz catalog facts remain
unranked memberships with zero RRF. They can support admission and independent-family agreement.
No catalog rank or ListenBrainz rank is invented.

Personal relevance uses the capped fusion sum (with the existing mode similarity multiplier),
existing weighted feedback and rediscovery, then existing identity scaling and serendipity.
Knownness, obviousness, trial-listen, fatigue, and diversity remain policy effects. Persisted
candidate evidence contains raw observations plus separate fusion ranks, uncapped source
contributions, capped family totals, and unranked membership. Relevance explanations name the raw
candidate contribution separately and reconcile identity scaling plus serendipity to persisted
pre-policy relevance; relevance plus the persisted policy adjustment equals the final score. Feature records include confidence,
family count, rank disagreement (`(max_rank-min_rank)/max_rank`, zero without ranks), source
freshness and missing/disabled/unavailable status.
Stale facts remain usable and are marked against provider cache TTLs; malformed rows are
quarantined individually with diagnostics. Ranked-source calibration versions are persisted.

```bash
scrobble-recs compare --config ./config.json --split-date 2024-01-01 \
  --scorers legacy,walk,fusion --ablate-source lastfm_walk --ablate-source semantic
scrobble-recs evaluate --config ./config.json --split-date 2024-01-01 \
  --scorer fusion --ablate-source listenbrainz
```

`compare` captures one temporary DuckDB export under a read transaction, checks a full content
hash (including legacy evidence and prior runs), and imports a fresh temporary database per
variant. Query targets, history/evidence cutoffs, and prior-run fatigue are identical. Comparison
runs are temporary and do not enter the user's persisted recommendation history. Equal snapshot
IDs alone do not prove a frozen database; the content checks and isolated copies provide that
extra assurance. Reports preserve existing metrics and add pool/eligible recall and coverage (coverage uses all persisted artist identities),
slate recall/NDCG, identity, novelty, tag diversity, popularity distribution (tag-chart affinity
proxy with missing share), provider concentration, independent-family share, runtime, and source
candidate counts. Runtime covers generation and metric calculation, excluding temporary database
materialization. Ablation rows report candidates added/removed when enabling that source,
relative to the full fusion pool. Source ablations change derived views, never stored evidence.
Fusion source counts use persisted retriever ranks/contributions and unranked memberships, including
walk-only candidates; older runs retain seed/tag-counter fallback. Last.fm direct, walk, and semantic
tag retrieval map to the same Last.fm provider. Ranked/provider source names are `lastfm_similarity`, `lastfm_walk`, `semantic`, `listenbrainz`,
and `musicbrainz`; direct local feedback membership is also inspectable.

**Promotion status: not promoted.** Blinded local judgments and complete guardrails do not yet
exist (`missing_blinded_evidence`, `incomplete_guardrails`). Deterministic offline fixtures verify
behavior and isolation; they are not quality or promotion evidence. Existing production defaults
remain unchanged and no weights are tuned from these fixtures.

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

For general discovery, this deliberately prevents tag-only recommendations. Artist-conditioned
queries can admit shared target semantic facets inside their explicit neighborhood.

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
