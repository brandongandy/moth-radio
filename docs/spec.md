# Seed software spec: local Last.fm recommendation engine

## 1. Working name

`scrobble-recs`

A local TUI application that imports a Last.fm scrobble CSV, enriches artists/tracks from free music APIs, builds a personal taste model, generates non-obvious artist recommendations, and learns from user feedback.

## 2. Primary goal

Given a CSV export:

```csv
uts,utc_time,artist,artist_mbid,album,album_mbid,track,track_mbid
```

produce ranked artist recommendations that are:

1. Similar enough to the user’s historical taste to be plausible.
2. Not just all-time top artists.
3. Capable of rediscovering artists the user barely touched years ago.
4. Explainable.
5. Tunable through TUI feedback.

## 3. Non-goals for v0.1

Do **not** build these first:

1. A full streaming app.
2. A public web service.
3. A song-level Pandora clone.
4. A real-time Last.fm scrobbler.
5. A full collaborative-filtering model trained from scratch on day one.
6. A recommender that requires Spotify.

The v0.1 should be local, inspectable, and mostly deterministic.

## 4. Recommended stack

Use **Python + DuckDB + Textual**.

DuckDB is a good fit because it is a local analytical SQL database with native Python support, reads CSV/Parquet/JSON well, and handles aggregation-heavy workloads cleanly. ([DuckDB][1])

Textual is a good fit for the TUI because it is a Python UI framework that runs in the terminal, supports structured widgets, and can be launched like a normal CLI app. ([Textual Documentation][2])

Recommended dependencies:

```toml
[project]
dependencies = [
  "duckdb>=1.0",
  "pandas>=2.0",
  "polars>=1.0",
  "textual>=0.80",
  "httpx>=0.27",
  "pydantic>=2.0",
  "typer>=0.12",
  "rich>=13.0",
  "rapidfuzz>=3.0",
  "platformdirs>=4.0",
  "python-dotenv>=1.0"
]

[project.optional-dependencies]
ml = [
  "scipy>=1.11",
  "scikit-learn>=1.4",
  "implicit>=0.7",
  "lightfm>=1.17"
]
```

For v0.1, `implicit` and `lightfm` are optional. The `implicit` library implements common implicit-feedback recommendation algorithms such as ALS, BPR, logistic matrix factorization, and item-item nearest-neighbor models. ([benfred.github.io][3]) LightFM is useful later because it supports both interaction data and item/user metadata in matrix-factorization models. ([making.lyst.com][4])

## 5. External data sources

### Last.fm API

Use Last.fm as the main v0.1 candidate generator.

Required methods:

```text
artist.getSimilar
artist.getTopTags
tag.getTopArtists
track.getSimilar    optional later
track.getTopTags    optional later
```

`artist.getSimilar` accepts either an artist name or MBID, requires a Last.fm API key, and does not require user authentication. ([Last.fm][5]) `tag.getTopArtists` returns artists for a tag ordered by tag count, which makes it useful for the “avoid obvious top genre artists” rule. ([Last.fm][6]) Last.fm enforces API limits, so the app should cache every request and never generate recommendations by hammering the API live from the TUI. ([Last.fm][7])

### MusicBrainz API

Use MusicBrainz for canonical identity, not as the main recommender.

Use it for:

```text
artist disambiguation
artist MBID lookup
recording MBID lookup
release/release-group lookup
aliases
artist relationships
tags/genres where available
```

MusicBrainz exposes artist, recording, release, release-group, genre, tag, label, area, work, and other resources through its API. It requires a proper User-Agent, and the documented client-side limit is one request per second. ([MusicBrainz][8])

For your scale, never query MusicBrainz per scrobble. Query per distinct artist/track after deduplication.

### ListenBrainz

Use ListenBrainz in v0.2+ for public collaborative data. ListenBrainz has recommendation APIs based on collaborative filtering, and it publishes data dumps that can be imported locally. ([ListenBrainz Documentation][9]) Full ListenBrainz dumps are published twice a month, and incremental dumps are available for keeping a local copy current. ([ListenBrainz Documentation][10])

v0.1 can skip ListenBrainz model training. v0.2 can add it as an optional “heavy mode.”

## 6. Configuration

Use one JSON config file. Support environment-variable interpolation for secrets so the config can be checked into a private repo without exposing keys.

Example:

```json
{
  "app": {
    "name": "scrobble-recs",
    "db_path": "~/.local/share/scrobble-recs/scrobbles.duckdb",
    "cache_dir": "~/.cache/scrobble-recs",
    "log_path": "~/.local/state/scrobble-recs/app.log"
  },
  "input": {
    "lastfm_csv": "~/exports/lastfm_scrobbles.csv",
    "timezone": "UTC",
    "timestamp_column": "uts",
    "csv_columns": [
      "uts",
      "utc_time",
      "artist",
      "artist_mbid",
      "album",
      "album_mbid",
      "track",
      "track_mbid"
    ]
  },
  "apis": {
    "lastfm": {
      "enabled": true,
      "api_key": "${LASTFM_API_KEY}",
      "base_url": "https://ws.audioscrobbler.com/2.0/",
      "rate_limit_per_second": 2.0,
      "cache_ttl_days": 180
    },
    "musicbrainz": {
      "enabled": true,
      "base_url": "https://musicbrainz.org/ws/2/",
      "user_agent": "scrobble-recs/0.1.0 your-email@example.com",
      "rate_limit_per_second": 1.0,
      "cache_ttl_days": 3650
    },
    "listenbrainz": {
      "enabled": false,
      "dump_dir": "~/datasets/listenbrainz",
      "use_recommendation_api": false
    }
  },
  "recommendation": {
    "default_mode": "balanced",
    "seed_artist_limit": 250,
    "similar_artists_per_seed": 100,
    "min_seed_scrobbles": 3,
    "max_knownness_default": 0.65,
    "rediscovery_enabled": true,
    "obviousness": {
      "enabled": true,
      "top_n_per_tag_penalty": 10,
      "top_n_per_tag_hide": 3,
      "tag_top_artists_limit": 100,
      "penalty_weight": 0.25
    },
    "weights": {
      "lastfm_similarity": 0.35,
      "tag_similarity": 0.25,
      "personal_cluster_fit": 0.15,
      "rediscovery": 0.10,
      "metadata_graph": 0.05,
      "feedback_model": 0.10
    },
    "diversity": {
      "enabled": true,
      "lambda": 0.75,
      "max_same_primary_tag_in_top_20": 4
    }
  },
  "tui": {
    "page_size": 25,
    "open_links_with": "system",
    "show_debug_scores": false
  }
}
```

Important config behavior:

```text
JSON config is the source of stable settings.
Environment variables override secrets.
CLI flags override both for one run.
The resolved config is stored in the database run metadata.
```

## 7. CLI contract

Use `typer` for the CLI.

```bash
scrobble-recs init --config ~/.config/scrobble-recs/config.json
scrobble-recs import --config config.json
scrobble-recs enrich --config config.json --source lastfm
scrobble-recs resolve --config config.json --source musicbrainz
scrobble-recs build-profile --config config.json
scrobble-recs recommend --config config.json --limit 500
scrobble-recs tui --config config.json
scrobble-recs export --config config.json --format csv --output recs.csv
scrobble-recs doctor --config config.json
```

Command responsibilities:

```text
init
  Create config skeleton and DuckDB file.

doctor
  Validate config, API keys, DB path, CSV schema, cache path, and rate-limit settings.

import
  Load CSV into raw tables.
  Normalize timestamps.
  Deduplicate exact duplicate scrobbles.
  Build initial artist/track facts.

resolve
  Resolve artist and track entities.
  Prefer supplied MBIDs when present but do not blindly trust them.
  Use fuzzy matching only when confidence is high.

enrich
  Pull Last.fm similar artists, tags, and tag top artists.
  Optionally pull MusicBrainz metadata.
  Write all API responses to cache tables first.

build-profile
  Compute personal artist weights, tag weights, genre clusters, knownness, recency, and rediscovery status.

recommend
  Generate candidates, score, diversify, and persist recommendation runs.

tui
  Load persisted recommendations.
  Allow filtering, feedback, inspection, and reranking.
```

## 8. Database schema

Use DuckDB as the state store. Keep raw data, canonicalized data, API cache, generated recommendation runs, and feedback in the same database.

### 8.1 Raw import

```sql
CREATE TABLE IF NOT EXISTS raw_scrobble (
    raw_scrobble_id UBIGINT,
    uts BIGINT,
    utc_time VARCHAR,
    artist VARCHAR,
    artist_mbid VARCHAR,
    album VARCHAR,
    album_mbid VARCHAR,
    track VARCHAR,
    track_mbid VARCHAR,
    imported_at TIMESTAMP DEFAULT current_timestamp
);
```

### 8.2 Clean scrobbles

```sql
CREATE TABLE IF NOT EXISTS scrobble (
    scrobble_id UBIGINT,
    played_at TIMESTAMP,
    played_date DATE,
    artist_raw VARCHAR,
    album_raw VARCHAR,
    track_raw VARCHAR,
    artist_mbid_raw VARCHAR,
    album_mbid_raw VARCHAR,
    track_mbid_raw VARCHAR,
    artist_key VARCHAR,
    track_key VARCHAR,
    is_duplicate BOOLEAN DEFAULT false
);
```

`artist_key` should be a normalized fallback key:

```text
lowercase
trim whitespace
collapse spaces
normalize punctuation
strip zero-width characters
do not remove meaningful words like “the”
```

### 8.3 Entity tables

```sql
CREATE TABLE IF NOT EXISTS artist (
    artist_id UBIGINT,
    canonical_name VARCHAR,
    artist_key VARCHAR,
    musicbrainz_artist_mbid VARCHAR,
    lastfm_name VARCHAR,
    disambiguation VARCHAR,
    country VARCHAR,
    begin_date VARCHAR,
    end_date VARCHAR,
    resolution_status VARCHAR,
    resolution_confidence DOUBLE,
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS track (
    track_id UBIGINT,
    artist_id UBIGINT,
    canonical_title VARCHAR,
    track_key VARCHAR,
    musicbrainz_recording_mbid VARCHAR,
    lastfm_track_name VARCHAR,
    resolution_status VARCHAR,
    resolution_confidence DOUBLE,
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP DEFAULT current_timestamp
);
```

### 8.4 Listening facts

```sql
CREATE TABLE IF NOT EXISTS artist_listening_fact (
    artist_id UBIGINT,
    play_count BIGINT,
    track_count BIGINT,
    album_count BIGINT,
    first_played_at TIMESTAMP,
    last_played_at TIMESTAMP,
    active_days BIGINT,
    active_months BIGINT,
    session_count BIGINT,
    recent_90d_plays BIGINT,
    recent_365d_plays BIGINT,
    lifetime_weight DOUBLE,
    recent_weight DOUBLE,
    knownness DOUBLE,
    rediscovery_score DOUBLE,
    updated_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS tag_profile (
    tag VARCHAR,
    source VARCHAR,
    user_weight DOUBLE,
    artist_count BIGINT,
    top_seed_artists JSON,
    updated_at TIMESTAMP DEFAULT current_timestamp
);
```

### 8.5 API cache

Cache raw API payloads. Do not design this as a temporary detail. It is central.

```sql
CREATE TABLE IF NOT EXISTS api_cache (
    cache_key VARCHAR PRIMARY KEY,
    source VARCHAR,
    endpoint VARCHAR,
    request_url VARCHAR,
    request_params JSON,
    response_status INTEGER,
    response_json JSON,
    fetched_at TIMESTAMP,
    expires_at TIMESTAMP,
    error_message VARCHAR
);
```

### 8.6 Similarity and metadata tables

```sql
CREATE TABLE IF NOT EXISTS artist_similarity_edge (
    source_artist_id UBIGINT,
    target_artist_id UBIGINT,
    source VARCHAR,
    source_score DOUBLE,
    normalized_score DOUBLE,
    evidence JSON,
    fetched_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artist_tag (
    artist_id UBIGINT,
    tag VARCHAR,
    source VARCHAR,
    tag_count DOUBLE,
    normalized_weight DOUBLE,
    fetched_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tag_top_artist (
    tag VARCHAR,
    artist_id UBIGINT,
    source VARCHAR,
    tag_rank INTEGER,
    tag_count DOUBLE,
    fetched_at TIMESTAMP
);
```

### 8.7 Recommendation run

```sql
CREATE TABLE IF NOT EXISTS recommendation_run (
    run_id UUID,
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
    created_at TIMESTAMP DEFAULT current_timestamp
);
```

### 8.8 Feedback

```sql
CREATE TABLE IF NOT EXISTS feedback_event (
    feedback_id UUID,
    created_at TIMESTAMP DEFAULT current_timestamp,
    run_id UUID,
    artist_id UBIGINT,
    action VARCHAR,
    value DOUBLE,
    context JSON,
    note VARCHAR
);
```

Allowed feedback actions:

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
rediscovery_good
rediscovery_bad
opened_link
played_externally
note
```

## 9. Import pipeline

### 9.1 Load raw CSV

The importer should validate the exact columns:

```text
uts
utc_time
artist
artist_mbid
album
album_mbid
track
track_mbid
```

Reject or warn on:

```text
missing uts
missing artist
missing track
non-integer uts
duplicate header rows inside file
timestamp mismatch between uts and utc_time
rows with impossible dates
```

### 9.2 Deduplicate

Duplicate scrobbles can happen through export glitches or double-submission. Mark duplicates rather than deleting them.

Default duplicate rule:

```text
same uts
same normalized artist
same normalized track
same normalized album
```

Possible near-duplicate rule:

```text
same artist + same track within 30 seconds
```

Near-duplicate should be configurable because live versions, repeated listens, and import quirks can be ambiguous.

### 9.3 Entity creation

Create a provisional artist for every distinct normalized artist key.

Initial artist identity priority:

```text
1. artist_mbid_raw, if non-empty and consistent across rows
2. exact normalized artist name
3. Last.fm autocorrect result, if enabled
4. MusicBrainz search result, if confidence is high
5. unresolved local artist entity
```

Do not block the app on perfect resolution. A local unresolved entity is better than losing data.

## 10. Personal taste model

The central mistake to avoid: recommending only from all-time top artists. That will mostly reproduce obvious taste.

Create several user profiles:

```text
lifetime_profile
recent_profile
deep_catalog_profile
underexplored_profile
old_obsession_profile
cluster_profiles
```

### 10.1 Artist weight

Use a weighted score, not raw play count.

Suggested formula:

```text
lifetime_weight =
    log1p(play_count)
  * (1.0 + 0.15 * log1p(track_count))
  * (1.0 + 0.10 * log1p(active_months))
```

Recent weight:

```text
days_since_last_play = today - last_played_at

recency_weight =
    exp(-days_since_last_play / 365)

recent_weight =
    lifetime_weight * (0.35 + 0.65 * recency_weight)
```

Interpretation:

```text
high play count + many tracks + many months = stable preference
high play count + one week only = obsession, not necessarily stable
old artist with huge play count = still relevant, but less dominant
```

### 10.2 Knownness

Knownness should suppress artists you already know well, without excluding barely touched artists.

```text
knownness =
    min(1.0, log1p(play_count) / log1p(50))
  * min(1.0, active_months / 6)
  * (0.40 + 0.60 * exp(-days_since_last_play / 1460))
```

Behavior:

```text
0 plays: unknown
1 play five years ago: almost unknown
5 plays eight years ago: rediscovery candidate
50 plays across many months: known
500 plays recently: heavily known
```

### 10.3 Rediscovery score

```text
rediscovery_score =
  positive if:
    play_count between 1 and 20
    last_played_at older than 2 years
    artist is similar to current/recent taste profile

  negative if:
    artist was rejected or marked already_know
```

This directly handles your “listened once five years ago” case.

## 11. Candidate generation

Generate too many candidates first, then rank and filter.

### 11.1 Seed artist selection

Seed artists should be drawn from multiple taste regions.

Default:

```text
top 150 by lifetime_weight
top 75 by recent_weight
top 50 by old_obsession_score
top 50 by underexplored-but-liked score
manually boosted artists from feedback
```

Then deduplicate and cap using `seed_artist_limit`.

### 11.2 Last.fm similar artists

For each seed artist:

```text
call artist.getSimilar(seed, limit=N)
store raw payload in api_cache
resolve returned artist names/MBIDs
insert artist_similarity_edge rows
```

Candidate score:

```text
lastfm_similarity_score(candidate) =
  sum_over_seed_artists(
    seed_weight(seed)
    * normalized_lastfm_match(seed, candidate)
  )
```

### 11.3 Tag expansion

From seed artists, pull tags and build a user tag profile.

For every important tag:

```text
call tag.getTopArtists(tag, limit=100)
store tag ranks
use ranks for both candidate generation and obviousness penalties
```

Candidate score:

```text
tag_similarity_score(candidate) =
  cosine_similarity(user_tag_vector, candidate_tag_vector)
```

### 11.4 Metadata graph expansion

In v0.1, keep this weak.

Generate candidates from:

```text
shared MusicBrainz relationships
same artist member relationships
same label, optional later
same scene/country/era, optional later
```

This should never dominate the score. It is mainly for surfacing side projects, collaborations, aliases, and adjacent acts.

### 11.5 ListenBrainz collaborative expansion

Make this v0.2.

Two possible paths:

```text
simple:
  use ListenBrainz recommendation API where applicable

heavy:
  import ListenBrainz dumps
  aggregate user-artist or user-recording matrix
  train implicit ALS/BPR model
  insert your scrobble history as one user vector
```

The heavy path is more work but gives you a real open collaborative signal.

## 12. Obviousness model

Your “avoid top 10 per genre” rule should be implemented as a penalty plus optional hide threshold.

Hard exclusion is too brittle. Example problem: an artist may be top 10 for “classic rock” but obscure for a tag you actually care about, or the genre tag itself may be too broad.

### 12.1 Calculate tag obviousness

For each candidate:

```text
candidate_tag_obviousness =
  max_over_tags(
    user_tag_weight(tag)
    * rank_penalty(candidate_rank_for_tag)
  )
```

Suggested rank penalty:

```text
rank 1-3:     1.00
rank 4-10:    0.75
rank 11-25:   0.40
rank 26-100:  0.15
rank >100:    0.00
unknown:      0.00
```

Then:

```text
obviousness_penalty =
  config.obviousness.penalty_weight * candidate_tag_obviousness
```

### 12.2 Default behavior

Default:

```text
rank 1-3 in a high-affinity tag:
  hide unless the candidate has strong rediscovery or manual boost evidence

rank 4-10:
  show only if total score survives penalty

rank 11+:
  no hard suppression
```

This matches your intent: avoid “you like classic rock, have you heard The Rolling Stones?” without making the recommender dumb.

## 13. Final ranking

Recommended v0.1 scoring formula:

```text
raw_score =
    0.35 * lastfm_similarity_score
  + 0.25 * tag_similarity_score
  + 0.15 * cluster_fit_score
  + 0.10 * rediscovery_score
  + 0.05 * metadata_graph_score
  + 0.10 * feedback_score

final_score =
    raw_score
  - knownness_penalty
  - obviousness_penalty
  - diversity_penalty
```

Knownness penalty:

```text
knownness_penalty =
  0.40 * max(0, knownness - max_knownness_default)
```

Feedback score:

```text
save:             +0.20 to similar future candidates
more_like_this:   +0.15 to nearby artists/tags
reject:           -0.25 to artist and close neighbors
too_obvious:      +0.10 to obviousness penalty for matching tags
wrong_genre:      -0.20 to that tag/candidate pathway
already_know:     increase knownness suppression
hide_artist:      exclude artist
hide_tag:         suppress tag
boost_tag:        increase tag profile weight
```

## 14. Diversity reranking

After scoring, apply a second-pass reranker so the top 50 does not become one blob of near-identical artists.

Use maximal marginal relevance:

```text
select candidate maximizing:

  lambda * final_score(candidate)
  - (1 - lambda) * max_similarity(candidate, already_selected)
```

Default:

```text
balanced mode:   lambda = 0.75
explore mode:    lambda = 0.55
safe mode:       lambda = 0.90
rediscovery:     lambda = 0.80
```

Add a simple cap:

```text
No more than 4 artists with the same primary tag in the top 20 by default.
```

## 15. Recommendation modes

The TUI should expose modes, not just filters.

```text
Balanced
  Main default. Similar but not too obvious.

Explore
  More long-tail, more diversity, less knownness tolerance.

Safe
  Higher similarity threshold, less diversity, fewer weird jumps.

Rediscovery
  Artists with low historical play count or long dormancy get boosted.

Recent Taste
  Seeds mostly from recent listening.

Old Taste
  Seeds from older high-weight eras.

Tag Focus
  User selects one or more tags/clusters.

Anti-Obvious
  Strong obviousness penalty and stricter top-rank hiding.
```

## 16. TUI design

Use a persisted recommendation run. The TUI should not call APIs while the user is navigating unless the user explicitly triggers refresh/enrich. This keeps the interface fast and avoids rate-limit problems.

### 16.1 Main layout

```text
┌────────────────────────────────────────────────────────────────────────────┐
│ scrobble-recs | mode: balanced | run: 2026-06-02  | filter: all            │
├───────────────────────────────┬────────────────────────────────────────────┤
│ Recommendations               │ Detail                                     │
│                               │                                            │
│  1. Artist A       0.842      │ Artist A                                   │
│  2. Artist B       0.811      │                                            │
│  3. Artist C       0.799      │ Why recommended:                           │
│  ...                          │ - Similar to Seed X, Seed Y                │
│                               │ - Tags: post-punk, coldwave                │
│                               │ - Not top 10 for dominant tags             │
│                               │ - Knownness: low                           │
├───────────────────────────────┴────────────────────────────────────────────┤
│ [s] save [r] reject [k] know [o] obvious [m] more like [l] less like [?]    │
└────────────────────────────────────────────────────────────────────────────┘
```

### 16.2 Screens

```text
Recommendations
  Main ranked list.

Detail
  Explanation, evidence, score breakdown, tags, links, feedback history.

Filters
  Mode, tag filters, knownness range, obviousness range, source filters.

Seeds
  Show seed artists and their weights.
  Allow manual boost/suppress.

Tags
  User tag profile.
  Show top tags, hidden tags, boosted tags.

Feedback
  Review prior feedback and undo mistakes.

Runs
  Compare recommendation runs.

Doctor
  API status, cache status, unresolved artists, import warnings.
```

### 16.3 Keybindings

```text
q      quit
j/k    move down/up
enter  open detail
s      save recommendation
r      reject
a      already know
o      too obvious
w      wrong genre/vibe
m      more like this
l      less like this
h      hide artist
t      edit tag feedback
f      filter
/      search
e      explain score
u      undo last feedback
R      regenerate from current config
O      open external links
?      help
```

### 16.4 Recommendation row columns

Default compact view:

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

Debug view:

```text
rank
artist
final_score
raw_score
lastfm_score
tag_score
cluster_score
rediscovery_score
knownness
obviousness
diversity_penalty
candidate_sources
```

### 16.5 Detail explanation

Each recommendation should have an explanation object like:

```json
{
  "summary": "Similar to several high-weight post-punk and coldwave artists, but outside the obvious top artists for those tags.",
  "seed_artists": [
    {
      "artist": "Seed Artist A",
      "seed_weight": 0.82,
      "source": "lastfm_artist_similarity",
      "similarity": 0.71
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
  "penalties": {
    "knownness": 0.04,
    "obviousness": 0.00,
    "diversity": 0.02
  },
  "classification": {
    "is_new": true,
    "is_rediscovery": false,
    "is_obvious": false
  }
}
```

## 17. Feedback loop

Do not retrain a full model after every keypress. Write feedback immediately, then apply a lightweight rerank.

Feedback should affect four things:

```text
artist-level suppression/boost
tag-level suppression/boost
similarity-neighborhood suppression/boost
mode defaults
```

Example:

```text
User marks Artist X as "too obvious".

Effects:
  artist X gets hidden in current run
  tags that made X obvious get a small obviousness penalty increase
  candidates with similar high tag-rank profiles get penalized
  explanation records that suppression came from feedback
```

Example:

```text
User marks Artist Y as "more like this".

Effects:
  Artist Y becomes a future seed
  its top tags receive small boosts
  artists similar to Y receive small boosts
  rejected/known artists are still suppressed
```

## 18. API/cache policy

### 18.1 Cache-first rule

All API clients should use this flow:

```text
build cache key
check api_cache
return cached response if fresh
otherwise wait for rate limiter
make HTTP request
write raw response to api_cache
parse response into normalized tables
```

### 18.2 Cache key

```text
source:endpoint:sha256(canonical_json_params)
```

Example:

```text
lastfm:artist.getSimilar:2f7c...
musicbrainz:artist.lookup:ad91...
```

### 18.3 Retry behavior

```text
429 / rate limited:
  exponential backoff
  keep stale cache if available

5xx:
  retry up to configured maximum
  keep stale cache if available

4xx:
  store error response
  do not retry aggressively
```

### 18.4 TUI behavior during missing enrichment

The TUI should show partial recommendations if some enrichment is missing.

Example status labels:

```text
complete
partial_tags
partial_similarity
unresolved_artist
api_error
stale_cache
```

## 19. Project layout

```text
scrobble-recs/
  pyproject.toml
  README.md
  config.example.json
  src/
    scrobble_recs/
      __init__.py
      cli.py
      config.py
      db.py
      logging.py

      importers/
        lastfm_csv.py

      normalize/
        strings.py
        timestamps.py
        identity.py

      apis/
        base.py
        cache.py
        rate_limit.py
        lastfm.py
        musicbrainz.py
        listenbrainz.py

      models/
        profile.py
        knownness.py
        tags.py
        recommend.py
        diversity.py
        feedback.py
        explain.py

      tui/
        app.py
        screens/
          recommendations.py
          detail.py
          filters.py
          seeds.py
          tags.py
          feedback.py
          doctor.py
        widgets/
          recommendation_table.py
          score_breakdown.py
          tag_cloud.py

      sql/
        001_init.sql
        002_import.sql
        003_entities.sql
        004_enrichment.sql
        005_recommendations.sql

      tests/
        fixtures/
          tiny_lastfm_export.csv
        test_import.py
        test_knownness.py
        test_scoring.py
        test_feedback.py
```

## 20. MVP implementation sequence

### Milestone 1: Import and inspect

Acceptance criteria:

```text
Can import the CSV.
Can show total scrobbles, distinct artists, distinct tracks.
Can show unresolved/null MBID counts.
Can build artist_listening_fact.
Can run without any API keys.
```

CLI:

```bash
scrobble-recs import --config config.json
scrobble-recs doctor --config config.json
```

### Milestone 2: Last.fm enrichment

Acceptance criteria:

```text
Can select seed artists.
Can call Last.fm artist.getSimilar.
Can call Last.fm tag.getTopArtists.
Can cache all responses.
Can resume after interruption.
Can avoid duplicate API requests.
```

CLI:

```bash
scrobble-recs enrich --config config.json --source lastfm
```

### Milestone 3: First recommender

Acceptance criteria:

```text
Can generate 500 ranked artist recommendations.
Can suppress known artists.
Can penalize obvious top-tag artists.
Can explain each recommendation.
Can export CSV.
```

CLI:

```bash
scrobble-recs recommend --config config.json --limit 500
scrobble-recs export --config config.json --output recs.csv
```

### Milestone 4: TUI

Acceptance criteria:

```text
Can browse recommendations.
Can inspect explanation.
Can filter by mode/tag/knownness/obviousness.
Can save/reject/mark obvious/mark already-known.
Can persist feedback.
Can rerank after feedback.
```

CLI:

```bash
scrobble-recs tui --config config.json
```

### Milestone 5: Better identity resolution

Acceptance criteria:

```text
Can resolve artists through MusicBrainz at safe rate limits.
Can merge obvious duplicate artists.
Can preserve manual corrections.
Can show low-confidence resolutions for review.
```

### Milestone 6: Optional collaborative model

Acceptance criteria:

```text
Can import a ListenBrainz-derived artist matrix.
Can train or load an implicit-feedback model.
Can blend collaborative score into existing ranking.
Can disable the model without breaking v0.1 behavior.
```

## 21. First useful SQL queries

Top artists should not be the product, but these queries validate the import.

```sql
SELECT
  artist_raw,
  count(*) AS plays,
  count(DISTINCT track_raw) AS tracks,
  min(played_at) AS first_played,
  max(played_at) AS last_played
FROM scrobble
WHERE NOT is_duplicate
GROUP BY artist_raw
ORDER BY plays DESC
LIMIT 25;
```

Build artist facts:

```sql
INSERT OR REPLACE INTO artist_listening_fact
SELECT
  a.artist_id,
  count(*) AS play_count,
  count(DISTINCT s.track_key) AS track_count,
  count(DISTINCT lower(s.album_raw)) AS album_count,
  min(s.played_at) AS first_played_at,
  max(s.played_at) AS last_played_at,
  count(DISTINCT s.played_date) AS active_days,
  count(DISTINCT date_trunc('month', s.played_at)) AS active_months,
  NULL AS session_count,
  count(*) FILTER (WHERE s.played_at >= current_timestamp - INTERVAL 90 DAY) AS recent_90d_plays,
  count(*) FILTER (WHERE s.played_at >= current_timestamp - INTERVAL 365 DAY) AS recent_365d_plays,
  NULL AS lifetime_weight,
  NULL AS recent_weight,
  NULL AS knownness,
  NULL AS rediscovery_score,
  current_timestamp AS updated_at
FROM scrobble s
JOIN artist a ON s.artist_key = a.artist_key
WHERE NOT s.is_duplicate
GROUP BY a.artist_id;
```

## 22. Export format

Recommendation export should be boring and useful:

```csv
rank,artist,score,primary_tags,knownness,obviousness,is_rediscovery,why,lastfm_url,musicbrainz_url
```

Also support JSONL for later analysis:

```json
{"rank":1,"artist":"...","score":0.842,"explanation":{...}}
```

## 23. Sensible defaults

Use these defaults unless testing shows otherwise:

```text
seed_artist_limit: 250
similar_artists_per_seed: 100
tag_top_artists_limit: 100
minimum artist plays to become seed: 3
known artist suppression threshold: 0.65
obvious top tag hard-hide: rank 1-3
obvious top tag penalty: rank 4-10
recommendation run size: 500
TUI page size: 25
```

## 24. The core algorithm in one pass

```text
1. Import scrobbles.
2. Normalize artist and track keys.
3. Build artist listening facts.
4. Compute seed artist weights.
5. Fetch Last.fm similar artists for seeds.
6. Fetch tags for seeds and candidates.
7. Fetch top artists for important tags.
8. Generate candidate artists.
9. Compute:
     Last.fm similarity score
     tag similarity score
     cluster fit score
     rediscovery score
     knownness
     obviousness
     feedback adjustment
10. Penalize known and obvious artists.
11. Diversify ranked list.
12. Persist recommendation run.
13. Browse and correct in TUI.
14. Feed corrections into next run.
```

## 25. The important design stance

The application should be **offline-first after enrichment**. API calls should improve the local graph, not be required for every interaction. The TUI should feel like browsing a local database, not waiting on web requests.

The first version should be a Last.fm-similarity + tag-profile + knownness/obviousness recommender. That is enough to create genuinely useful recommendations. MusicBrainz identity resolution and ListenBrainz collaborative modeling can be layered in after the feedback loop proves the ranking model is worth improving.

[1]: https://duckdb.org/ "DuckDB – An in-process SQL OLAP database management system"
[2]: https://textual.textualize.io/ "Textual"
[3]: https://benfred.github.io/implicit/ "Implicit — Implicit 0.7.2 documentation"
[4]: https://making.lyst.com/lightfm/docs/ "Welcome to LightFM’s documentation! — LightFM 1.16 documentation"
[5]: https://www.last.fm/api/show/artist.getSimilar "API Docs | Last.fm"
[6]: https://www.last.fm/api/show/tag.getTopArtists "API Docs | Last.fm"
[7]: https://www.last.fm/api/tos?utm_source=chatgpt.com "API Terms of Service"
[8]: https://musicbrainz.org/doc/MusicBrainz_API "MusicBrainz API - MusicBrainz"
[9]: https://listenbrainz.readthedocs.io/en/latest/users/api/recommendation.html?utm_source=chatgpt.com "Recommendations — ListenBrainz 0.1.0 documentation"
[10]: https://listenbrainz.readthedocs.io/en/latest/users/listenbrainz-dumps.html "Data Dumps — ListenBrainz 0.1.0 documentation"