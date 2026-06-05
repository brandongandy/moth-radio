# Codex Handoff Spec: scrobble-recs v0.2
# Last.fm Enrichment + Smarter Hybrid Ranking

## Read first

Before coding, read:

- `AGENTS.md`
- `README.md`
- `docs/spec.md`
- `docs/plan.md`
- `docs/implementation-log.md`
- existing migrations under `src/scrobble_recs/sql/` or equivalent
- existing tests under `tests/`

If repo file names differ, adapt to the existing structure rather than inventing a parallel structure.

## Version goal

Implement v0.2 of `scrobble-recs`.

v0.2 turns the app from a local scrobble analyzer / basic scoring skeleton into a cache-first recommendation engine that can generate real external artist candidates using Last.fm enrichment.

The release should add:

1. cache-first Last.fm API client
2. enrichment queue
3. Last.fm artist similarity graph
4. artist tag enrichment
5. tag top-artist enrichment
6. user tag profile
7. obviousness penalties
8. external candidate generation
9. hybrid ranking
10. feedback-aware reranking
11. recommendation explanation JSON
12. enrichment/cache inspection tooling
13. historical evaluation harness
14. TUI evidence and score-breakdown panels

## Existing app constraints

Preserve these constraints:

- Python application.
- DuckDB as local database.
- Textual TUI.
- Typer CLI.
- JSON config file.
- Local-first behavior after enrichment.
- No network access required for tests.
- No live API calls from TUI.
- No Spotify dependency.
- Missing MBIDs must not break import, enrichment, recommendation, or TUI.
- Existing v0.1 behavior must keep working.
- API responses must be cached.
- Raw API JSON must be stored before normalized parsing.
- Duplicate scrobbles must be marked, not deleted.
- Previously listened artists must be penalized by knownness, not blindly excluded.

## Out of scope for v0.2

Do not implement these in v0.2:

- full MusicBrainz identity-resolution review UI
- ListenBrainz dump import
- collaborative filtering model training
- audio-feature extraction
- Spotify API integration
- web app
- streaming playback
- exact Auralist algorithms
- track-level recommender as the main product

You may add stubs/config fields for future ListenBrainz or MusicBrainz work, but do not make them required.

## Source APIs for v0.2

Implement these Last.fm methods:

```text
artist.getSimilar
artist.getTopTags
tag.getTopArtists
```

Add optional support for this method if needed for URL/stats enrichment, but do not make it necessary for v0.2 recommendation generation:

```text
artist.getInfo
```

Do not implement these yet unless trivial and fully tested:

```text
track.getSimilar
track.getTopTags
album.getInfo
```

## v0.2 user-facing commands

Add or update CLI commands:

```bash
scrobble-recs enrich plan --source lastfm --config config.json
scrobble-recs enrich run --source lastfm --limit 1000 --config config.json
scrobble-recs enrich status --source lastfm --config config.json
scrobble-recs enrich retry-failed --source lastfm --config config.json

scrobble-recs cache stats --config config.json
scrobble-recs cache inspect --source lastfm --config config.json
scrobble-recs cache expire --source lastfm --older-than 180d --config config.json

scrobble-recs build-profile --config config.json
scrobble-recs profile tags --config config.json

scrobble-recs recommend --mode balanced --limit 500 --config config.json
scrobble-recs recommend --mode explore --limit 500 --config config.json
scrobble-recs recommend --mode rediscovery --limit 500 --config config.json

scrobble-recs evaluate --config config.json --split-date 2023-01-01
scrobble-recs evaluate --config config.json --train-until 2022-12-31 --test-from 2023-01-01

scrobble-recs tui --config config.json
```

If the command hierarchy already exists differently, preserve the existing style while supporting equivalent behavior.

## v0.2 config additions

Extend the JSON config schema without breaking older configs.

Example:

```json
{
  "apis": {
    "lastfm": {
      "enabled": true,
      "api_key": "${LASTFM_API_KEY}",
      "base_url": "https://ws.audioscrobbler.com/2.0/",
      "rate_limit_per_second": 2.0,
      "cache_ttl_days": 180,
      "timeout_seconds": 20,
      "max_retries": 3,
      "stale_cache_on_error": true
    },
    "musicbrainz": {
      "enabled": false,
      "base_url": "https://musicbrainz.org/ws/2/",
      "user_agent": "scrobble-recs/0.2.0 user@example.com",
      "rate_limit_per_second": 1.0
    },
    "listenbrainz": {
      "enabled": false,
      "dump_dir": null,
      "use_recommendation_api": false
    }
  },
  "enrichment": {
    "seed_artist_limit": 250,
    "candidate_artist_limit": 5000,
    "similar_artists_per_seed": 100,
    "top_tags_per_artist": 20,
    "tag_top_artists_limit": 100,
    "max_queue_attempts": 3,
    "queue_batch_size": 1000
  },
  "recommendation": {
    "default_mode": "balanced",
    "run_limit": 500,
    "min_seed_scrobbles": 3,
    "max_knownness_default": 0.65,
    "rediscovery_enabled": true,
    "weights": {
      "lastfm_similarity": 0.40,
      "tag_similarity": 0.25,
      "cluster_fit": 0.15,
      "rediscovery": 0.10,
      "feedback": 0.10
    },
    "obviousness": {
      "enabled": true,
      "top_n_per_tag_hide": 3,
      "top_n_per_tag_penalty": 10,
      "tag_top_artists_limit": 100,
      "penalty_weight": 0.25
    },
    "knownness": {
      "enabled": true,
      "threshold": 0.65,
      "penalty_weight": 0.40
    },
    "diversity": {
      "enabled": true,
      "lambda": 0.75,
      "max_same_primary_tag_in_top_20": 4
    }
  },
  "tui": {
    "show_score_breakdown": true,
    "show_evidence_panel": true,
    "show_cache_status": true
  }
}
```

Config requirements:

- Environment interpolation must work for `${LASTFM_API_KEY}`.
- `doctor` should warn if Last.fm is enabled but no key is available.
- Import/stats/TUI over existing data must not require Last.fm key.
- Recommendation should run in degraded mode if enrichment is incomplete.
- Tests must not require real environment variables.

## Database migrations

Add migrations for the following tables if they do not already exist.

Use project migration conventions. Do not paste these blindly if the project already has naming/versioning patterns; adapt them.

```sql
CREATE TABLE IF NOT EXISTS api_cache (
    cache_key VARCHAR PRIMARY KEY,
    source VARCHAR NOT NULL,
    endpoint VARCHAR NOT NULL,
    request_params JSON NOT NULL,
    response_status INTEGER,
    response_json JSON,
    fetched_at TIMESTAMP,
    expires_at TIMESTAMP,
    error_message VARCHAR
);

CREATE TABLE IF NOT EXISTS enrichment_queue (
    queue_id UUID PRIMARY KEY,
    created_at TIMESTAMP DEFAULT current_timestamp,
    source VARCHAR NOT NULL,
    endpoint VARCHAR NOT NULL,
    entity_type VARCHAR NOT NULL,
    entity_id UBIGINT,
    priority DOUBLE DEFAULT 0,
    status VARCHAR DEFAULT 'queued',
    attempts INTEGER DEFAULT 0,
    last_attempt_at TIMESTAMP,
    error_message VARCHAR
);

CREATE TABLE IF NOT EXISTS artist_similarity_edge (
    source_artist_id UBIGINT,
    target_artist_id UBIGINT,
    source VARCHAR,
    source_score DOUBLE,
    normalized_score DOUBLE,
    evidence JSON,
    fetched_at TIMESTAMP,
    PRIMARY KEY (source_artist_id, target_artist_id, source)
);

CREATE TABLE IF NOT EXISTS artist_tag (
    artist_id UBIGINT,
    tag VARCHAR,
    source VARCHAR,
    tag_count DOUBLE,
    normalized_weight DOUBLE,
    fetched_at TIMESTAMP,
    PRIMARY KEY (artist_id, tag, source)
);

CREATE TABLE IF NOT EXISTS tag_top_artist (
    tag VARCHAR,
    artist_id UBIGINT,
    source VARCHAR,
    tag_rank INTEGER,
    tag_count DOUBLE,
    fetched_at TIMESTAMP,
    PRIMARY KEY (tag, artist_id, source)
);

CREATE TABLE IF NOT EXISTS tag_profile (
    tag VARCHAR,
    source VARCHAR,
    user_weight DOUBLE,
    artist_count BIGINT,
    top_seed_artists JSON,
    updated_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (tag, source)
);

CREATE TABLE IF NOT EXISTS recommendation_candidate (
    run_id UUID,
    artist_id UBIGINT,
    candidate_sources JSON,
    max_source_score DOUBLE,
    appeared_from_seed_count INTEGER,
    appeared_from_tag_count INTEGER,
    created_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (run_id, artist_id)
);
```

If not already present from v0.1, ensure these exist:

```sql
CREATE TABLE IF NOT EXISTS recommendation_run (
    run_id UUID PRIMARY KEY,
    created_at TIMESTAMP DEFAULT current_timestamp,
    config_hash VARCHAR,
    mode VARCHAR,
    seed_summary JSON,
    scoring_weights JSON,
    notes VARCHAR
);

CREATE TABLE IF NOT EXISTS recommendation (
    run_id UUID,
    rank INTEGER,
    artist_id UBIGINT,
    final_score DOUBLE,
    raw_score DOUBLE,
    lastfm_similarity_score DOUBLE,
    tag_similarity_score DOUBLE,
    cluster_fit_score DOUBLE,
    rediscovery_score DOUBLE,
    metadata_graph_score DOUBLE,
    feedback_score DOUBLE,
    knownness DOUBLE,
    obviousness DOUBLE,
    diversity_penalty DOUBLE,
    explanation JSON,
    created_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (run_id, artist_id)
);

CREATE TABLE IF NOT EXISTS feedback_event (
    feedback_id UUID PRIMARY KEY,
    created_at TIMESTAMP DEFAULT current_timestamp,
    run_id UUID,
    artist_id UBIGINT,
    action VARCHAR,
    value DOUBLE,
    context JSON,
    note VARCHAR
);
```

## Suggested package layout additions

Adapt to existing layout.

```text
src/scrobble_recs/
  apis/
    __init__.py
    base.py
    cache.py
    rate_limit.py
    lastfm.py

  enrichment/
    __init__.py
    queue.py
    planner.py
    runner.py
    parse_lastfm.py
    status.py

  models/
    profile.py
    tags.py
    candidates.py
    scoring.py
    knownness.py
    obviousness.py
    diversity.py
    feedback.py
    explain.py
    evaluate.py

  tui/
    screens/
      recommendations.py
      detail.py
      filters.py
      tags.py
      feedback.py
      doctor.py
    widgets/
      score_breakdown.py
      evidence_panel.py
      cache_status.py

  sql/
    00x_v02_api_cache.sql
    00x_v02_enrichment.sql
    00x_v02_recommendation_candidates.sql
```

Do not over-abstract prematurely. Keep scoring math easy to inspect.

## Last.fm client requirements

Implement a cache-first Last.fm client with these public methods:

```python
class LastFmClient:
    def artist_get_similar(
        self,
        artist: str | None = None,
        mbid: str | None = None,
        limit: int = 100,
        autocorrect: bool = False,
    ) -> dict: ...

    def artist_get_top_tags(
        self,
        artist: str | None = None,
        mbid: str | None = None,
        autocorrect: bool = False,
    ) -> dict: ...

    def tag_get_top_artists(
        self,
        tag: str,
        limit: int = 100,
        page: int = 1,
    ) -> dict: ...

    def artist_get_info(
        self,
        artist: str | None = None,
        mbid: str | None = None,
        autocorrect: bool = False,
    ) -> dict: ...
```

Rules:

- Accept either artist name or MBID when Last.fm supports both.
- Prefer MBID if present and trusted enough.
- Fall back to artist name.
- Always request JSON format.
- Use `httpx`.
- Use explicit timeout.
- Use rate limiter.
- Use retry policy.
- Use stale cache on transient failures when configured.
- Store raw response JSON before normalized parsing.
- Do not expose API key in logs, cache keys, exceptions, or test snapshots.
- Tests must use mocked HTTP responses.

## Cache key requirements

Build cache keys as:

```text
{source}:{endpoint}:{sha256(canonical_json_params)}
```

Canonical JSON means:

- sort keys
- remove API key
- remove empty/null params that do not change semantics
- stable encoding
- include endpoint and method
- include API version if useful

Example:

```python
def make_cache_key(source: str, endpoint: str, params: Mapping[str, Any]) -> str:
    safe_params = {
        key: value
        for key, value in params.items()
        if key.lower() not in {"api_key", "token", "authorization"}
        and value is not None
        and value != ""
    }
    encoded = json.dumps(safe_params, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"{source}:{endpoint}:{digest}"
```

## API cache behavior

Flow:

```text
1. Build canonical cache key.
2. Look up api_cache.
3. If fresh and response_status is 2xx, return cached JSON.
4. If stale and network is unavailable/transient error occurs, return stale JSON if config allows.
5. Otherwise make rate-limited HTTP request.
6. Write response or error to api_cache.
7. Return parsed JSON or raise typed API error.
```

Status handling:

```text
2xx:
  store response_json and parse

429:
  retry with backoff, then keep stale cache if available

5xx:
  retry with backoff, then keep stale cache if available

4xx:
  store error, do not retry aggressively

invalid JSON:
  store error_message, fail gracefully

missing API key:
  do not attempt request; show actionable doctor/enrich error
```

## Enrichment queue requirements

The queue exists so the app does not try to enrich every artist in a 363k-scrobble library.

Add planning logic:

```text
artist.getSimilar:
  queue selected seed artists

artist.getTopTags:
  queue selected seed artists
  queue candidates produced by artist.getSimilar

tag.getTopArtists:
  queue high-affinity user tags only
```

Queue priority:

```text
priority = seed_weight * endpoint_weight
```

Endpoint weights:

```text
artist.getSimilar: 1.00
artist.getTopTags: 0.80
tag.getTopArtists: 0.60
artist.getInfo: 0.20, if implemented
```

Status values:

```text
queued
running
complete
failed
skipped
```

`enrich plan` should not call the network. It should populate or update the queue and print a summary.

Example output:

```text
Last.fm enrichment plan

artist.getSimilar:
  queued: 250
  cached fresh: 193
  missing: 57

artist.getTopTags:
  queued: 900
  cached fresh: 311
  missing: 589

tag.getTopArtists:
  queued: 80
  cached fresh: 14
  missing: 66
```

`enrich run --limit N` should process at most N queued items.

`enrich status` should print:

```text
queued
running
complete
failed
skipped
cache hit rate
last successful fetch
top failed endpoints
artists missing getSimilar
artists missing getTopTags
tags missing tag.getTopArtists
```

## Parsing Last.fm responses

### artist.getSimilar

Parse into `artist_similarity_edge`.

Input shape may resemble:

```json
{
  "similarartists": {
    "artist": [
      {
        "name": "Candidate Artist",
        "mbid": "candidate-mbid",
        "match": "0.842",
        "url": "https://www.last.fm/music/Candidate+Artist",
        "image": []
      }
    ],
    "@attr": {
      "artist": "Seed Artist"
    }
  }
}
```

Parsing requirements:

- Resolve or create target artist entity.
- Store returned MBID when present as hint.
- Preserve returned Last.fm name.
- Store source score as float.
- Normalize score to 0..1.
- Store evidence JSON including:
  - seed artist id
  - seed artist name
  - returned name
  - returned MBID
  - Last.fm URL
  - rank
  - endpoint

Evidence example:

```json
{
  "endpoint": "artist.getSimilar",
  "source_artist_name": "Seed Artist",
  "target_artist_name": "Candidate Artist",
  "target_mbid": "candidate-mbid",
  "lastfm_url": "https://www.last.fm/music/Candidate+Artist",
  "rank": 1
}
```

### artist.getTopTags

Parse into `artist_tag`.

Input shape may resemble:

```json
{
  "toptags": {
    "tag": [
      {
        "name": "post-punk",
        "count": 100,
        "url": "https://www.last.fm/tag/post-punk"
      }
    ],
    "@attr": {
      "artist": "Seed Artist"
    }
  }
}
```

Parsing requirements:

- Normalize tag names:
  - lowercase
  - trim
  - collapse whitespace
  - preserve meaningful punctuation such as hyphen
- Ignore empty tags.
- Convert count to float.
- Normalize per artist so strongest tag has weight 1.0.
- Store source `lastfm_artist_getTopTags`.

### tag.getTopArtists

Parse into `tag_top_artist`.

Input shape may resemble:

```json
{
  "topartists": {
    "artist": [
      {
        "name": "Artist A",
        "mbid": "artist-a-mbid",
        "url": "https://www.last.fm/music/Artist+A"
      }
    ],
    "@attr": {
      "tag": "post-punk",
      "page": "1",
      "perPage": "100"
    }
  }
}
```

Parsing requirements:

- Resolve/create artist entity.
- Store rank starting at 1.
- Use pagination rank if page > 1.
- Store source `lastfm_tag_getTopArtists`.
- Keep tag as normalized tag string.
- Store count if available; otherwise NULL.

## Artist entity behavior for external candidates

When Last.fm returns an artist not present in the scrobble export:

- create a new artist row
- set `canonical_name` from Last.fm name
- set `lastfm_name`
- set `musicbrainz_artist_mbid` if returned
- set `resolution_status = 'external_lastfm'`
- set `resolution_confidence = 0.70` by default if MBID absent
- set `resolution_confidence = 0.85` if MBID present
- `artist_listening_fact` should show zero local plays or no row; scoring must handle both

Do not drop candidates because they lack MBIDs.

## Seed artist selection

Do not use only top artists.

Build seed pools from local listening facts:

```text
lifetime seeds:
  top by lifetime_weight

recent seeds:
  top by recent_weight

old obsession seeds:
  high historical play count, low recent plays, older last_played_at

underexplored seeds:
  moderate affinity, low play count, multiple tracks or positive feedback

manual feedback seeds:
  artists with save or more_like_this feedback
```

Default allocation:

```text
lifetime: 100
recent: 75
old_obsession: 50
underexplored: 50
manual_feedback: 25
```

Deduplicate artists across pools. If a pool has fewer than its allocation, allow other pools to fill.

Seed summary should be persisted in `recommendation_run.seed_summary`.

Seed summary example:

```json
{
  "lifetime": 100,
  "recent": 75,
  "old_obsession": 42,
  "underexplored": 50,
  "manual_feedback": 3,
  "total_distinct": 233
}
```

## Artist weights

Use or update v0.1 formulas.

Lifetime weight:

```text
lifetime_weight =
    log1p(play_count)
  * (1.0 + 0.15 * log1p(track_count))
  * (1.0 + 0.10 * log1p(active_months))
```

Recent weight:

```text
days_since_last_play = today - last_played_at
recency_weight = exp(-days_since_last_play / 365)
recent_weight = lifetime_weight * (0.35 + 0.65 * recency_weight)
```

Knownness:

```text
knownness =
    min(1.0, log1p(play_count) / log1p(50))
  * min(1.0, active_months / 6)
  * (0.40 + 0.60 * exp(-days_since_last_play / 1460))
```

External candidates with no local plays:

```text
knownness = 0.0
```

Artists with one old scrobble must remain eligible.

Rediscovery score:

```text
rediscovery_score =
  positive if:
    play_count between 1 and 20
    last_played_at older than 2 years
    candidate is similar to recent/lifetime profile

  near zero if:
    no plays

  negative if:
    rejected or marked already_know
```

## User tag profile

Build `tag_profile` from listened artists and `artist_tag`.

For each listened artist/tag pair:

```text
contribution =
  artist_weight
  * artist_tag.normalized_weight
  * tag_specificity
```

Tag specificity:

```text
tag_specificity = 1 / log(2 + number_of_user_artists_with_tag)
```

Purpose:

- reduce generic tags such as `rock`, `alternative`, `indie`, `seen live`
- preserve more specific taste tags such as `coldwave`, `minimal synth`, `zeuhl`, `jangle pop`

Add a configurable tag denylist / weaklist:

```json
{
  "tag_profile": {
    "weak_tags": [
      "seen live",
      "favorites",
      "favourite",
      "favorite",
      "spotify",
      "lastfm"
    ],
    "generic_tag_penalty": 0.25
  }
}
```

Do not hard-delete weak tags; lower their weight.

`profile tags` should print:

```text
tag
user_weight
artist_count
top_seed_artists
```

## Candidate generation

Generate candidates from three v0.2 sources:

```text
1. Last.fm similar artists from seed artists
2. top artists for high-affinity tags
3. local rediscovery artists
```

### Last.fm similar candidates

For each seed artist with `artist_similarity_edge` rows:

```text
candidate_source = lastfm_artist_similarity
candidate evidence includes seed artist and similarity
```

Score:

```text
lastfm_similarity_score(candidate) =
  sum_over_seeds(
    seed_weight(seed)
    * normalized_lastfm_match(seed, candidate)
  )
```

Normalize final candidate scores to 0..1 across candidate set.

### Tag expansion candidates

For high-affinity tags from `tag_profile`:

```text
candidate_source = lastfm_tag_top_artists
candidate evidence includes tag and tag rank
```

Rank decay:

```text
rank_decay(rank) = 1 / log2(2 + rank)
```

Score:

```text
tag_expansion_score(candidate) =
  sum_over_tags(
    user_tag_weight(tag)
    * rank_decay(candidate_tag_rank)
  )
```

### Rediscovery candidates

From local artists:

```text
play_count between 1 and 20
last_played_at older than 2 years
knownness < threshold
not hidden/rejected
```

Rediscovery source should be visible in explanation.

## Obviousness model

Implement the user’s “avoid top 10 per genre/tag” goal as penalty plus optional hard-hide for rank 1–3.

Do not hard-ban all top 10 artists. Use penalty, because tag labels are noisy.

For each candidate:

```text
candidate_tag_obviousness =
  max_over_tags(
    user_tag_weight(tag)
    * rank_penalty(candidate_rank_for_tag)
  )
```

Rank penalty:

```text
rank 1-3:     1.00
rank 4-10:    0.75
rank 11-25:   0.40
rank 26-100:  0.15
rank >100:    0.00
unknown:      0.00
```

Final obviousness penalty:

```text
obviousness_penalty =
  config.recommendation.obviousness.penalty_weight
  * candidate_tag_obviousness
```

Default hard-hide behavior:

```text
if candidate rank is 1-3 for a high-affinity tag:
  hide unless:
    rediscovery mode is active
    or artist has positive feedback
    or user config disables hard-hide
```

Persist `recommendation.obviousness` as the unweighted obviousness value.

Explanation should say which tag/rank caused the penalty.

Example:

```json
{
  "obviousness": {
    "value": 0.75,
    "penalty": 0.1875,
    "causes": [
      {
        "tag": "classic rock",
        "rank": 4,
        "user_tag_weight": 0.82,
        "rank_penalty": 0.75
      }
    ]
  }
}
```

## Hybrid scoring

Compute these components for each candidate:

```text
lastfm_similarity_score
tag_similarity_score
cluster_fit_score
rediscovery_score
feedback_score
knownness
obviousness
knownness_penalty
obviousness_penalty
diversity_penalty
```

v0.2 raw score:

```text
raw_score =
    0.40 * lastfm_similarity_score
  + 0.25 * tag_similarity_score
  + 0.15 * cluster_fit_score
  + 0.10 * rediscovery_score
  + 0.10 * feedback_score
```

Knownness penalty:

```text
knownness_penalty =
  knownness_penalty_weight
  * max(0, knownness - knownness_threshold)
```

Final score before diversity:

```text
pre_diversity_score =
    raw_score
  - knownness_penalty
  - obviousness_penalty
```

Diversity reranking happens after scoring.

Store final values in `recommendation`.

## Cluster fit score

For v0.2, implement a simple tag-vector cluster fit.

If clustering infrastructure already exists, reuse it. Otherwise:

1. create user tag vector from `tag_profile`
2. create candidate tag vector from `artist_tag`
3. compute cosine similarity

```text
cluster_fit_score = cosine(user_tag_vector, candidate_tag_vector)
```

If candidate has no tags:

```text
cluster_fit_score = 0
```

This can be replaced in v0.3 by richer vectors/clusters.

## Tag similarity score

Use candidate artist tags.

```text
tag_similarity_score =
  cosine_similarity(user_tag_vector, candidate_artist_tag_vector)
```

If using both `tag_similarity_score` and `cluster_fit_score`, keep one as direct cosine and one as higher-level cluster coverage only if clusters exist.

If no cluster implementation exists in v0.2:

```text
cluster_fit_score = tag_similarity_score
```

But document this clearly and keep both columns for forward compatibility.

## Feedback-aware reranking

Feedback actions:

```text
save
reject
already_know
too_obvious
wrong_genre
more_like_this
less_like_this
hide_artist
hide_tag
boost_tag
```

Basic effects:

```text
save:
  +0.20 feedback score for artist
  +0.05 to similar artists
  +0.05 to top tags

more_like_this:
  add artist to manual seed pool
  +0.15 to similar artists
  +0.10 to tags

reject:
  hard suppress artist unless config says soft
  -0.25 to close neighbors

already_know:
  increase knownness suppression for artist

too_obvious:
  increase obviousness pressure for matching tag/rank buckets

wrong_genre:
  suppress source pathway that produced the candidate

less_like_this:
  reduce influence of artist’s tags and similarity neighborhood

hide_artist:
  exclude artist from future runs

hide_tag:
  suppress tag in profile and candidate scoring

boost_tag:
  increase tag profile weight
```

Feedback must affect the next recommendation run.

Do not require full model retraining after each TUI keypress. Record feedback immediately; apply on next rerank/regenerate.

## Diversity reranking

After scoring, apply maximal marginal relevance-like reranking.

Selection rule:

```text
select next candidate maximizing:

  lambda * pre_diversity_score(candidate)
  - (1 - lambda) * max_similarity(candidate, already_selected)
```

Default lambdas:

```text
balanced: 0.75
explore: 0.55
safe: 0.90
rediscovery: 0.80
```

Candidate-to-candidate similarity:

1. use tag cosine if both artists have tag vectors
2. else use Last.fm graph edge if available
3. else 0

Also enforce:

```text
No more than 4 artists with the same primary tag in top 20 by default.
```

Persist `diversity_penalty`.

## Recommendation modes

Implement at least:

```text
balanced
explore
rediscovery
```

Mode behavior:

```text
balanced:
  default weights

explore:
  stronger obviousness penalty
  lower diversity lambda
  lower knownness threshold
  more tag-expansion candidates
  more long-tail candidates

rediscovery:
  boost local old low-play candidates
  relax knownness threshold slightly for old artists
  do not hard-hide top 1-3 if the artist is a rediscovery candidate
```

Optional:

```text
safe
recent
old_taste
anti_obvious
```

Do not block v0.2 completion on optional modes.

## Recommendation explanation JSON

Every recommendation must have explainable evidence.

Persist in `recommendation.explanation`.

Example:

```json
{
  "summary": "Similar to several high-weight post-punk and coldwave seed artists, with low knownness and no top-tag obviousness penalty.",
  "classification": {
    "is_new": true,
    "is_rediscovery": false,
    "is_obvious": false
  },
  "score_components": {
    "raw_score": 0.812,
    "final_score": 0.744,
    "lastfm_similarity_score": 0.88,
    "tag_similarity_score": 0.76,
    "cluster_fit_score": 0.71,
    "rediscovery_score": 0.00,
    "feedback_score": 0.05,
    "knownness": 0.00,
    "obviousness": 0.00,
    "knownness_penalty": 0.00,
    "obviousness_penalty": 0.00,
    "diversity_penalty": 0.02
  },
  "seed_artists": [
    {
      "artist_id": 123,
      "artist": "Seed Artist A",
      "seed_weight": 0.91,
      "similarity": 0.82,
      "source": "lastfm_artist_getSimilar"
    }
  ],
  "tags": [
    {
      "tag": "post-punk",
      "user_weight": 0.63,
      "candidate_weight": 0.58,
      "candidate_rank_for_tag": 42
    }
  ],
  "candidate_sources": [
    {
      "source": "lastfm_artist_similarity",
      "seed_artist": "Seed Artist A",
      "score": 0.82
    }
  ],
  "penalties": {
    "knownness": {
      "value": 0.00,
      "penalty": 0.00
    },
    "obviousness": {
      "value": 0.00,
      "penalty": 0.00,
      "causes": []
    },
    "diversity": {
      "penalty": 0.02
    }
  },
  "cache": {
    "similarity_fetched_at": "2026-06-01T12:00:00",
    "tags_fetched_at": "2026-06-01T12:01:00"
  }
}
```

## TUI v0.2 additions

Do not rewrite the TUI. Add panels/widgets.

### Recommendations list columns

Default:

```text
rank
artist
score
primary_tags
knownness
obviousness
why_short
feedback_state
```

Debug:

```text
rank
artist
final_score
raw_score
lastfm_score
tag_score
cluster_score
rediscovery_score
feedback_score
knownness
obviousness
diversity_penalty
candidate_sources
```

### Detail screen additions

Add:

```text
score breakdown panel
evidence panel
cache/enrichment status
feedback history
```

Score breakdown should show:

```text
final score
raw score
lastfm similarity score
tag similarity score
cluster fit score
rediscovery score
feedback score
knownness penalty
obviousness penalty
diversity penalty
```

Evidence panel should show:

```text
similarity seeds
matching tags
tag ranks
candidate sources
cache freshness
feedback effects
```

### Feedback impact preview

When user marks feedback, show a short explanation.

Example for `too_obvious`:

```text
Marked as too obvious.
Future runs will increase obviousness pressure for:
- source: tag expansion
- tag: classic rock
- rank bucket: 1–10
```

### TUI hard rule

The TUI must never call Last.fm directly. It should read persisted tables only.

## Evaluation harness

Add command:

```bash
scrobble-recs evaluate --config config.json --split-date 2023-01-01
```

Also support explicit train/test:

```bash
scrobble-recs evaluate \
  --config config.json \
  --train-until 2022-12-31 \
  --test-from 2023-01-01
```

Evaluation behavior:

```text
1. Build profile using train period only.
2. Generate recommendations from train profile.
3. Compare recommended artists against test-period artists.
4. Report ranking and quality metrics.
```

Metrics:

```text
recall@50
recall@100
ndcg@50
knownness_distribution
obviousness_distribution
tag_diversity@20
tag_diversity@50
cluster_coverage@50
rediscovery_hit_rate
external_candidate_share
```

Minimum printed output:

```text
Evaluation

train_until: 2022-12-31
test_from: 2023-01-01

recall@50: 0.042
recall@100: 0.067
ndcg@50: 0.031
tag_diversity@20: 0.78
knownness median: 0.08
obviousness median: 0.12
rediscovery_hit_rate: 0.05
external_candidate_share: 0.83
```

Evaluation does not need to be perfect. It should catch regressions.

## Test requirements

All tests must pass without network access.

Use mocked Last.fm responses.

Add fixtures:

```text
tests/fixtures/lastfm_artist_get_similar_the_cure.json
tests/fixtures/lastfm_artist_get_top_tags_the_cure.json
tests/fixtures/lastfm_tag_get_top_artists_post_punk.json
tests/fixtures/tiny_lastfm_export.csv
```

### Suggested fake `artist.getSimilar` fixture

```json
{
  "similarartists": {
    "artist": [
      {
        "name": "The Chameleons",
        "mbid": "fake-mbid-chameleons",
        "match": "0.91",
        "url": "https://www.last.fm/music/The+Chameleons",
        "image": []
      },
      {
        "name": "Sad Lovers and Giants",
        "mbid": "",
        "match": "0.74",
        "url": "https://www.last.fm/music/Sad+Lovers+and+Giants",
        "image": []
      }
    ],
    "@attr": {
      "artist": "The Cure"
    }
  }
}
```

### Suggested fake `artist.getTopTags` fixture

```json
{
  "toptags": {
    "tag": [
      {
        "name": "post-punk",
        "count": 100,
        "url": "https://www.last.fm/tag/post-punk"
      },
      {
        "name": "new wave",
        "count": 82,
        "url": "https://www.last.fm/tag/new+wave"
      },
      {
        "name": "seen live",
        "count": 15,
        "url": "https://www.last.fm/tag/seen+live"
      }
    ],
    "@attr": {
      "artist": "The Cure"
    }
  }
}
```

### Suggested fake `tag.getTopArtists` fixture

```json
{
  "topartists": {
    "artist": [
      {
        "name": "Joy Division",
        "mbid": "fake-mbid-joy-division",
        "url": "https://www.last.fm/music/Joy+Division"
      },
      {
        "name": "The Cure",
        "mbid": "fake-mbid-cure",
        "url": "https://www.last.fm/music/The+Cure"
      },
      {
        "name": "The Chameleons",
        "mbid": "fake-mbid-chameleons",
        "url": "https://www.last.fm/music/The+Chameleons"
      },
      {
        "name": "Sad Lovers and Giants",
        "mbid": "",
        "url": "https://www.last.fm/music/Sad+Lovers+and+Giants"
      }
    ],
    "@attr": {
      "tag": "post-punk",
      "page": "1",
      "perPage": "100",
      "totalPages": "1",
      "total": "4"
    }
  }
}
```

### Unit tests

Add tests for:

```text
cache key removes API key
cache hit returns cached JSON without HTTP call
cache miss performs mocked HTTP call and writes cache
stale cache fallback works on 5xx
429 retry behavior does not spin forever
Last.fm similar parsing creates artist_similarity_edge
Last.fm top tags parsing creates artist_tag
Last.fm tag top artists parsing creates tag_top_artist
external artists are created without MBIDs
seed selection uses multiple pools
tag profile lowers weak/generic tags
knownness handles zero-play external candidates
knownness keeps one old scrobble eligible
obviousness penalizes rank 1-10 tag leaders
recommendations can include artists absent from original CSV
feedback affects next recommendation run
TUI feedback writes feedback_event
evaluation command prints required metrics
```

### Integration tests

Use temporary DuckDB file.

Flow:

```text
1. import tiny CSV
2. build profile
3. mock Last.fm enrichment
4. run enrich plan
5. run enrich run
6. generate recommendations
7. assert recommendation includes external candidate
8. assert explanation JSON has score components and evidence
9. add feedback
10. regenerate
11. assert ranking/score changes
```

## Acceptance criteria

v0.2 is complete when:

1. `doctor` still works without API keys for local-only commands.
2. `enrich plan --source lastfm` creates a queue without calling the network.
3. `enrich run --source lastfm --limit N` processes queued items using cache-first client.
4. Raw Last.fm JSON is stored in `api_cache`.
5. Parsed Last.fm similarity rows are stored in `artist_similarity_edge`.
6. Parsed artist tags are stored in `artist_tag`.
7. Parsed tag top artists are stored in `tag_top_artist`.
8. External candidate artists can be created even without MBIDs.
9. `build-profile` creates/updates `tag_profile`.
10. `recommend --mode balanced` creates recommendation runs with external candidates.
11. Known artists are penalized, not blindly removed.
12. One old scrobble can become a rediscovery candidate.
13. Obvious top-tag artists are penalized or hidden according to config.
14. Every recommendation has explanation JSON.
15. Feedback affects the next recommendation run.
16. TUI shows score breakdown and evidence for persisted recommendations.
17. TUI does not call Last.fm.
18. `cache stats` and `enrich status` report useful diagnostics.
19. `evaluate` reports recall, diversity, knownness, and obviousness metrics.
20. Tests pass without network access.
21. Docs and implementation log are updated.

## Verification commands

Run these before declaring completion:

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src
```

If ruff or mypy are not configured yet, either configure them minimally or document the skipped command in `docs/implementation-log.md`.

Also run a local smoke test using fixture data:

```bash
scrobble-recs doctor --config tests/fixtures/config.test.json
scrobble-recs import --config tests/fixtures/config.test.json
scrobble-recs build-profile --config tests/fixtures/config.test.json
scrobble-recs enrich plan --source lastfm --config tests/fixtures/config.test.json
scrobble-recs enrich run --source lastfm --limit 100 --config tests/fixtures/config.test.json
scrobble-recs recommend --mode balanced --limit 25 --config tests/fixtures/config.test.json
scrobble-recs evaluate --split-date 2021-01-03 --config tests/fixtures/config.test.json
```

Use mocked API mode or fixture-backed HTTP transport for smoke tests.

## Implementation log

Update `docs/implementation-log.md` with:

```markdown
## v0.2 implementation

### Added

- ...

### Changed

- ...

### Tests

- `python -m pytest`
- ...

### Known limitations

- ...

### Follow-up for v0.3

- ...
```

## Important implementation warnings

- Do not fetch Last.fm data during recommendation scoring.
- Do not fetch Last.fm data from the TUI.
- Do not store API keys in cache keys or logs.
- Do not rank by raw play count.
- Do not exclude all previously scrobbled artists.
- Do not treat blank MBID as failure.
- Do not mutate raw scrobble rows.
- Do not make Last.fm required for app startup.
- Do not implement ListenBrainz in v0.2.
- Do not claim v0.2 implements Auralist.