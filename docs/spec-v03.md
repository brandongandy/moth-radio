# Codex Handoff Spec: scrobble-recs v0.3
# Auralist-Inspired Serendipity Ranking

## Read first

Before coding, read:

- `AGENTS.md`
- `README.md`
- `docs/spec.md`
- `docs/plan.md`
- `docs/implementation-log.md`
- v0.2 implementation
- v0.2 recommendation/scoring modules
- v0.2 TUI detail/evidence panels
- v0.2 tests

This spec assumes v0.2 exists and passes.

If v0.2 does not exist yet, do not start v0.3. Implement v0.2 first.

## Version goal

Implement v0.3 of `scrobble-recs`.

v0.3 adds an Auralist-inspired serendipity ranking layer.

The release should add:

1. score-component persistence
2. artist vectors
3. user taste clusters
4. optional Last.fm popularity enrichment via `artist.getInfo`
5. novelty rank
6. unserendipity metric
7. listener-diversity proxy
8. declustering proxy
9. rank-interpolated serendipity mode
10. Auralist-style evaluation metrics
11. TUI serendipity/debug panel
12. tests using deterministic fake graphs and fake API responses

## Important conceptual boundary

Do not claim this is exact Auralist.

Exact Auralist requires multi-user listening data and Artist-based LDA over artist-listener relationships.

This implementation is:

```text
Auralist-inspired
```

or:

```text
serendipity mode inspired by Auralist
```

It approximates Auralist-like behavior using the v0.2 local artist graph, Last.fm similarity edges, Last.fm tags, local listening history, user taste clusters, and optional Last.fm popularity.

## Auralist concepts to approximate

v0.3 should approximate:

```text
1. Basic relevance:
   the existing v0.2 hybrid recommender

2. Listener Diversity:
   promote artists connected to multiple user taste clusters

3. Declustering:
   promote artists that are relevant but not buried inside one tight known cluster

4. Novelty:
   prefer less globally obvious artists, bounded by relevance

5. Rank interpolation:
   combine component rankings rather than only raw weighted score summation
```

## Existing app constraints

Preserve:

- Python.
- DuckDB.
- Textual TUI.
- Typer CLI.
- JSON config.
- Local-first behavior.
- No network access required for tests.
- No live API calls from TUI.
- No Spotify.
- Existing v0.2 balanced/explore/rediscovery modes must keep working.
- Missing popularity data must not break recommendations.
- Missing tags/vectors must degrade gracefully.
- Previously listened artists must be handled with knownness/rediscovery logic, not blunt exclusion.

## Out of scope for v0.3

Do not implement:

- exact Artist-based LDA from ListenBrainz dumps
- ListenBrainz dump import
- full collaborative filtering training
- audio-feature extraction
- Spotify integration
- web app
- full MusicBrainz graph traversal
- manual entity merge UI

You may add schema that will support later ListenBrainz vectors, but do not require them.

## User-facing commands

Add or update:

```bash
scrobble-recs enrich run --source lastfm --endpoint artist.getInfo --limit 1000 --config config.json

scrobble-recs build-vectors --config config.json
scrobble-recs build-clusters --config config.json

scrobble-recs recommend --mode serendipity --limit 500 --config config.json
scrobble-recs recommend --mode balanced --limit 500 --config config.json

scrobble-recs evaluate --metrics standard --config config.json
scrobble-recs evaluate --metrics serendipity --config config.json
scrobble-recs evaluate --metrics all --config config.json

scrobble-recs explain --artist "Artist Name" --run latest --config config.json

scrobble-recs tui --config config.json
```

If an `explain` command does not exist, add it.

## v0.3 config additions

Extend config without breaking older configs.

Example:

```json
{
  "serendipity": {
    "enabled": true,
    "mode": "auralist_inspired",
    "default_rank_blend": {
      "basic": 0.77,
      "listener_diversity": 0.03,
      "declustering": 0.20,
      "novelty": 0.00
    },
    "experimental_rank_blend": {
      "basic": 0.72,
      "listener_diversity": 0.03,
      "declustering": 0.20,
      "novelty": 0.05
    },
    "guardrails": {
      "min_basic_relevance_percentile": 0.40,
      "min_history_neighbors": 2,
      "max_knownness": 0.75,
      "allow_rediscovery": true,
      "allow_obvious_if_positive_feedback": true
    },
    "distance": {
      "target_history_similarity": 0.35,
      "too_close_similarity": 0.75,
      "too_far_similarity": 0.05
    },
    "popularity": {
      "source": "lastfm_artist_getInfo",
      "novelty_enabled": true,
      "popularity_adjustment_beta": 0.15,
      "missing_popularity_strategy": "neutral"
    },
    "declustering": {
      "similarity_threshold_strategy": "user_average",
      "stddev_offset": 0.0,
      "max_history_artists": 500,
      "max_neighbors_per_candidate": 50
    },
    "clusters": {
      "method": "tag_vector_kmeans",
      "max_clusters": 12,
      "min_cluster_size": 3,
      "fallback_method": "tag_prefix_or_primary_tag"
    }
  }
}
```

Config behavior:

- If `serendipity.enabled = false`, v0.3 should not affect recommendations.
- If popularity is missing, novelty component should be neutral.
- If clusters cannot be built, listener-diversity proxy should be neutral.
- If graph is too sparse, declustering proxy should be neutral.
- Balanced mode should remain stable.

## Database migrations

Add these tables.

Adapt naming/versioning to existing migration conventions.

```sql
CREATE TABLE IF NOT EXISTS artist_popularity (
    artist_id UBIGINT PRIMARY KEY,
    source VARCHAR,
    listeners BIGINT,
    plays BIGINT,
    popularity_percentile DOUBLE,
    fetched_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artist_vector (
    artist_id UBIGINT,
    vector_type VARCHAR,
    vector JSON,
    norm DOUBLE,
    source VARCHAR,
    built_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (artist_id, vector_type, source)
);

CREATE TABLE IF NOT EXISTS artist_cluster (
    artist_id UBIGINT,
    cluster_id INTEGER,
    cluster_weight DOUBLE,
    source VARCHAR,
    built_at TIMESTAMP,
    PRIMARY KEY (artist_id, cluster_id, source)
);

CREATE TABLE IF NOT EXISTS recommendation_score_component (
    run_id UUID,
    artist_id UBIGINT,
    component VARCHAR,
    raw_value DOUBLE,
    normalized_value DOUBLE,
    percentile_rank DOUBLE,
    weight DOUBLE,
    explanation JSON,
    created_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (run_id, artist_id, component)
);

CREATE TABLE IF NOT EXISTS evaluation_run (
    evaluation_id UUID PRIMARY KEY,
    run_id UUID,
    created_at TIMESTAMP DEFAULT current_timestamp,
    config_hash VARCHAR,
    metric_set VARCHAR,
    split_config JSON,
    notes VARCHAR
);

CREATE TABLE IF NOT EXISTS evaluation_metric (
    evaluation_id UUID,
    run_id UUID,
    metric VARCHAR,
    value DOUBLE,
    details JSON,
    created_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (evaluation_id, metric)
);
```

If these tables already exist from v0.2 preparation, migrate them carefully rather than duplicating.

## Suggested package additions

Adapt to existing layout.

```text
src/scrobble_recs/
  models/
    vectors.py
    clusters.py
    rank_blend.py
    novelty.py
    serendipity.py
    declustering.py
    listener_diversity.py
    evaluation_metrics.py

  enrichment/
    popularity.py

  tui/
    widgets/
      serendipity_panel.py
      component_rank_table.py
```

Keep functions testable and deterministic.

## Last.fm artist.getInfo enrichment

Add or complete support for `artist.getInfo`.

Purpose:

- collect global listeners
- collect global playcount
- collect Last.fm URL
- support novelty/popularity percentile

Public method:

```python
class LastFmClient:
    def artist_get_info(
        self,
        artist: str | None = None,
        mbid: str | None = None,
        autocorrect: bool = False,
    ) -> dict: ...
```

Parsing target:

```sql
artist_popularity
```

Input shape may resemble:

```json
{
  "artist": {
    "name": "The Chameleons",
    "mbid": "fake-mbid-chameleons",
    "url": "https://www.last.fm/music/The+Chameleons",
    "stats": {
      "listeners": "456789",
      "playcount": "12345678"
    },
    "tags": {
      "tag": [
        {
          "name": "post-punk",
          "url": "https://www.last.fm/tag/post-punk"
        }
      ]
    }
  }
}
```

Parsing requirements:

- Convert listeners and playcount to integers.
- Store source `lastfm_artist_getInfo`.
- Missing stats should not fail enrichment.
- Use cache-first client from v0.2.
- TUI must not call this endpoint directly.

Popularity percentile:

```text
For all artists with known listener counts:
  popularity_percentile = percentile_rank(listeners ascending)
```

Interpretation:

```text
0.00 = least popular among known candidates
1.00 = most popular among known candidates
```

Missing popularity strategy:

```text
neutral:
  novelty rank = 0.50

optimistic:
  novelty rank = 0.75

pessimistic:
  novelty rank = 0.25
```

Default:

```text
neutral
```

## Artist vectors

Build and persist artist vectors.

Minimum vector types:

```text
lastfm_tag_vector
similarity_neighborhood_vector
```

Optional if easy:

```text
combined_v02_vector
```

### lastfm_tag_vector

From `artist_tag`.

Sparse JSON representation:

```json
{
  "post-punk": 1.0,
  "new wave": 0.82,
  "gothic rock": 0.64
}
```

Normalize vector to unit length.

### similarity_neighborhood_vector

From `artist_similarity_edge`.

Sparse JSON representation:

```json
{
  "artist:123": 0.91,
  "artist:456": 0.74
}
```

Normalize vector to unit length.

### combined_v02_vector

Weighted combination:

```text
combined_v02_vector =
  0.70 * lastfm_tag_vector
  + 0.30 * similarity_neighborhood_vector
```

Only combine where meaningful. Since tag dimensions and artist dimensions differ, either:

- store them as separate namespaces in one sparse vector, e.g. `tag:post-punk`, `artist:123`
- or compute similarities separately and blend

Recommended:

```text
Use namespaced sparse vector keys.
```

Example:

```json
{
  "tag:post-punk": 0.70,
  "tag:new-wave": 0.52,
  "artist:123": 0.27,
  "artist:456": 0.18
}
```

## Vector helpers

Implement deterministic helpers:

```python
def sparse_norm(vector: dict[str, float]) -> float: ...

def normalize_sparse(vector: dict[str, float]) -> dict[str, float]: ...

def sparse_cosine(
    left: Mapping[str, float],
    right: Mapping[str, float],
) -> float: ...

def blend_sparse_vectors(
    weighted_vectors: Sequence[tuple[float, Mapping[str, float]]],
) -> dict[str, float]: ...
```

Tests must cover:

```text
empty vectors
single-key vectors
orthogonal vectors
identical vectors
weighted blending
normalization
```

## User taste clusters

Build user taste clusters from listened artists, not from all external candidates.

Minimum implementation:

1. Build `combined_v02_vector` for listened artists.
2. Select history artists with meaningful weight.
3. Cluster them.
4. Persist artist-cluster memberships.

Preferred method:

```text
sklearn KMeans on dense representation of top vector dimensions
```

But avoid making scikit-learn a hard dependency if the project does not already allow it. If adding scikit-learn is too much, use deterministic fallback:

```text
primary-tag clusters
```

Fallback clustering:

```text
cluster_id = stable hash / mapping of artist's highest-weight non-generic tag
```

Example:

```text
cluster 1: post-punk
cluster 2: dream pop
cluster 3: synthpop
cluster 4: folk
```

Persist:

```sql
artist_cluster
```

Rules:

- Artists may belong to multiple clusters if using soft cluster memberships.
- Fallback primary-tag clustering can use weight 1.0 for the primary cluster.
- Clusters with fewer than `min_cluster_size` should be merged into `other` or ignored.
- Store source:
  - `tag_vector_kmeans`
  - or `primary_tag_fallback`

`build-clusters` should print:

```text
clusters built: 8
artists clustered: 312
method: primary_tag_fallback
largest cluster: post-punk, 54 artists
smallest cluster: sophisti-pop, 4 artists
```

## v0.3 recommendation score components

Persist components in `recommendation_score_component`.

Components:

```text
basic_relevance
novelty
history_similarity
unserendipity
listener_diversity_proxy
declustering_proxy
knownness
obviousness
feedback
final_rank_blend
```

Component persistence requirements:

- raw value
- normalized value, 0..1 when possible
- percentile rank
- component weight
- explanation JSON

Percentile convention:

```text
percentile_rank = 0.0 means best for that component
percentile_rank = 1.0 means worst for that component
```

For components where higher raw is better:

```text
percentile_rank = rank_descending(raw_value) / (n - 1)
```

For components where lower raw is better:

```text
percentile_rank = rank_ascending(raw_value) / (n - 1)
```

Handle ties deterministically.

## Basic relevance component

Use v0.2 `pre_diversity_score` or v0.2 `raw_score` plus penalties before MMR.

Recommended:

```text
basic_relevance_raw =
    v02_raw_score
  - knownness_penalty
  - obviousness_penalty
```

Then:

```text
basic_relevance_percentile_rank =
  percentile rank descending basic_relevance_raw
```

This is the anchor. Serendipity mode should still require plausible relevance.

Guardrail:

```text
candidate must pass min_basic_relevance_percentile
```

Default:

```text
min_basic_relevance_percentile = 0.40
```

Meaning:

```text
Only candidates in the top 40% by basic relevance are eligible for serendipity promotion.
```

## Novelty component

Novelty should reward less globally common artists without turning the recommender into random obscurity.

Use Last.fm listener count when available.

Raw novelty:

```text
novelty_raw = 1.0 - popularity_percentile
```

If no popularity:

```text
novelty_raw = 0.50
```

Optional information-content variant:

```text
popularity_probability =
  (listeners + smoothing) / total_listener_mass

novelty_information_content =
  -log2(popularity_probability)
```

For v0.3, percentile-based novelty is enough and more stable.

Novelty percentile rank:

```text
higher novelty_raw is better
```

Guardrails:

```text
Do not let novelty rescue candidates below relevance floor.
Do not apply novelty to hidden/rejected artists.
Do not hard-promote artists with zero similarity to the user profile.
```

Explanation example:

```json
{
  "component": "novelty",
  "raw_value": 0.72,
  "popularity_percentile": 0.28,
  "listeners": 456789,
  "interpretation": "Less globally popular than most enriched candidates."
}
```

## History similarity and unserendipity

Auralist-like serendipity needs a way to measure “too close to what I already know” versus “too far to be relevant.”

Compute candidate-to-history similarity.

History set:

```text
top weighted listened artists
limit = serendipity.declustering.max_history_artists
exclude hidden/rejected artists
```

Similarity source priority:

```text
1. combined_v02_vector cosine
2. tag vector cosine
3. Last.fm similarity edge
4. 0
```

For each candidate:

```text
history_similarity_raw =
  weighted_average_similarity(candidate, history_artist)
```

Also compute:

```text
max_history_similarity
neighbor_count_above_threshold
```

Unserendipity:

```text
unserendipity_raw = history_similarity_raw
```

Interpretation:

```text
high unserendipity = too similar / unsurprising
medium unserendipity = potentially serendipitous
very low unserendipity = probably irrelevant
```

Do not directly minimize unserendipity. Instead, use it for metrics and guardrails.

Distance preference score:

```text
distance_from_target =
  abs(history_similarity_raw - target_history_similarity)

history_distance_score =
  max(0, 1 - distance_from_target / target_history_similarity)
```

Default:

```text
target_history_similarity = 0.35
too_close_similarity = 0.75
too_far_similarity = 0.05
```

Candidates with:

```text
history_similarity_raw > too_close_similarity:
  apply mild too-close penalty

history_similarity_raw < too_far_similarity:
  apply relevance guardrail unless strong tag/candidate evidence exists
```

## Listener-diversity proxy

Exact Auralist Listener Diversity uses entropy over LDA listener-community topic distribution.

v0.3 proxy:

```text
A candidate is more listener-diverse if it connects to multiple user taste clusters.
```

For each candidate, compute contribution from each user cluster:

```text
cluster_contribution[c] =
  sum over history artists h in cluster c:
    user_artist_weight(h)
    * similarity(candidate, h)
    * cluster_weight(h, c)
```

Normalize:

```text
p[c] = cluster_contribution[c] / sum(cluster_contribution)
```

Entropy:

```text
listener_diversity_raw =
  -sum(p[c] * log2(p[c])) / log2(number_of_active_clusters)
```

If fewer than 2 active clusters:

```text
listener_diversity_raw = 0.0
```

Popularity adjustment:

```text
adjusted_listener_diversity =
  listener_diversity_raw
  - beta * popularity_percentile
```

Default:

```text
beta = 0.15
```

Clamp to 0..1.

Higher adjusted listener diversity is better.

Explanation example:

```json
{
  "component": "listener_diversity_proxy",
  "raw_value": 0.68,
  "adjusted_value": 0.61,
  "clusters_touched": [
    {
      "cluster_id": 1,
      "label": "post-punk",
      "contribution": 0.44
    },
    {
      "cluster_id": 3,
      "label": "dream pop",
      "contribution": 0.31
    },
    {
      "cluster_id": 5,
      "label": "synthpop",
      "contribution": 0.25
    }
  ],
  "interpretation": "Candidate connects to multiple parts of your listening history."
}
```

## Declustering proxy

Exact Auralist Declustering promotes candidates outside established user bubbles by measuring local graph clustering.

v0.3 proxy:

```text
A candidate is better if it is relevant to the user's history but its nearest historical neighbors are not all tightly connected to each other.
```

For candidate `i`:

```text
N_u(i) =
  top historical neighbors h
  where similarity(i, h) >= threshold
```

Threshold options:

```text
user_average:
  threshold = average nonzero candidate-history similarity

fixed:
  threshold = config value if later added

top_k:
  keep top k neighbors
```

Default:

```text
similarity_threshold_strategy = user_average
min_history_neighbors = 2
max_neighbors_per_candidate = 50
```

Compute neighbor embeddedness:

```text
embeddedness(i) =
  average pairwise similarity among artists in N_u(i)
```

Declustering raw:

```text
declustering_raw = 1.0 - embeddedness(i)
```

Higher is better.

Guardrails:

```text
if neighbor_count < min_history_neighbors:
  declustering_raw = 0.0 or neutral, based on config

if basic relevance below floor:
  candidate is ineligible for serendipity promotion

if candidate hidden/rejected:
  exclude
```

Explanation example:

```json
{
  "component": "declustering_proxy",
  "raw_value": 0.73,
  "neighbor_count": 5,
  "embeddedness": 0.27,
  "nearest_history_neighbors": [
    {
      "artist": "Seed Artist A",
      "similarity": 0.61,
      "cluster": "post-punk"
    },
    {
      "artist": "Seed Artist B",
      "similarity": 0.44,
      "cluster": "dream pop"
    }
  ],
  "interpretation": "Candidate is relevant but bridges less-connected areas of your history."
}
```

## Rank interpolation

Serendipity mode must use rank interpolation, not only raw weighted sums.

Default Auralist-inspired blend:

```text
rank_serendipity =
    0.77 * rank_basic_relevance
  + 0.03 * rank_listener_diversity
  + 0.20 * rank_declustering
```

Experimental blend with novelty:

```text
rank_serendipity_plus =
    0.72 * rank_basic_relevance
  + 0.03 * rank_listener_diversity
  + 0.20 * rank_declustering
  + 0.05 * rank_novelty
```

Conventions:

```text
lower rank score is better
each rank component is percentile_rank in [0, 1]
weights should sum to 1.0
```

If a component is unavailable:

```text
use neutral rank = 0.50
renormalize weights only if config says so
default: do not renormalize; neutral rank keeps behavior stable
```

Store `final_rank_blend` as a score component.

Convert final rank blend to `recommendation.final_score` for compatibility:

```text
final_score = 1.0 - final_rank_blend
```

Do not delete v0.2 final-score behavior for balanced mode.

## Serendipity recommendation mode

Add:

```bash
scrobble-recs recommend --mode serendipity --limit 500 --config config.json
```

Serendipity mode flow:

```text
1. Generate v0.2 candidates.
2. Compute v0.2 relevance scores.
3. Build/load artist vectors.
4. Build/load user clusters.
5. Compute popularity/novelty if available.
6. Compute history similarity.
7. Compute listener-diversity proxy.
8. Compute declustering proxy.
9. Convert components to percentile ranks.
10. Apply rank interpolation.
11. Apply hard filters:
    - hidden artist
    - hidden tag
    - rejected artist if configured as hard reject
12. Persist score components.
13. Persist recommendations.
14. Build explanation JSON.
```

Serendipity should produce a different ranking from balanced mode on deterministic fixture data.

## Recommendation explanation JSON additions

Extend existing v0.2 explanation.

Example:

```json
{
  "summary": "Relevant to your post-punk and dream-pop history, but less embedded in a single known cluster than typical similar-artist recommendations.",
  "classification": {
    "is_new": true,
    "is_rediscovery": false,
    "is_obvious": false,
    "is_serendipity_candidate": true
  },
  "score_components": {
    "basic_relevance": {
      "raw_value": 0.742,
      "percentile_rank": 0.09,
      "weight": 0.77
    },
    "listener_diversity_proxy": {
      "raw_value": 0.61,
      "percentile_rank": 0.21,
      "weight": 0.03
    },
    "declustering_proxy": {
      "raw_value": 0.73,
      "percentile_rank": 0.04,
      "weight": 0.20
    },
    "novelty": {
      "raw_value": 0.72,
      "percentile_rank": 0.33,
      "weight": 0.00
    },
    "final_rank_blend": {
      "raw_value": 0.083,
      "final_score": 0.917
    }
  },
  "serendipity": {
    "history_similarity": 0.36,
    "unserendipity": 0.36,
    "target_history_similarity": 0.35,
    "clusters_touched": [
      {
        "cluster_id": 1,
        "label": "post-punk",
        "contribution": 0.44
      },
      {
        "cluster_id": 3,
        "label": "dream pop",
        "contribution": 0.31
      }
    ],
    "declustering": {
      "neighbor_count": 5,
      "embeddedness": 0.27,
      "declustering_raw": 0.73
    },
    "novelty": {
      "listeners": 456789,
      "popularity_percentile": 0.28,
      "novelty_raw": 0.72
    }
  },
  "why_serendipitous": [
    "Candidate is close enough to your history to be relevant.",
    "Candidate connects to more than one taste cluster.",
    "Candidate's nearest history neighbors are not all tightly connected.",
    "Candidate is not heavily known from prior scrobbles."
  ]
}
```

## TUI additions

Do not rewrite the TUI.

Add a serendipity/debug panel to the detail screen.

Panel should show:

```text
Serendipity evidence

Basic relevance rank:          0.09
Listener diversity rank:       0.21
Declustering rank:             0.04
Novelty rank:                  0.33
Final rank blend:              0.083
Knownness:                     0.02
Obviousness:                   0.00
History similarity:            0.36
Target history similarity:     0.35
Taste clusters touched:        post-punk, dream pop, synthpop
Cluster embeddedness:          low

Why:
- Similar enough to your history to be relevant.
- Connected to multiple taste clusters.
- Less embedded in a single known bubble.
- Not strongly obvious or already known.
```

Add list filters:

```text
mode = serendipity
show only cluster-bridging candidates
show only high-novelty candidates
hide too-close candidates
hide too-far candidates
```

TUI must read persisted `recommendation_score_component` rows and `recommendation.explanation`.

No network calls from TUI.

## Evaluation metrics

Extend `evaluate`.

Metric sets:

```text
standard
serendipity
all
```

### Standard metrics from v0.2

Keep:

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

### v0.3 serendipity metrics

Add:

```text
intra_list_similarity@20
intra_list_similarity@50
novelty@20
novelty@50
unserendipity@20
unserendipity@50
listener_diversity_proxy@20
declustering_proxy@20
cluster_bridge_rate@20
too_close_rate@20
too_far_rate@20
serendipity_component_coverage
```

Definitions:

```text
intra_list_similarity@N:
  average pairwise similarity among top N recommended artists
  lower usually means more diverse

novelty@N:
  average novelty_raw among top N

unserendipity@N:
  average candidate-history similarity among top N

listener_diversity_proxy@N:
  average listener_diversity_proxy among top N

declustering_proxy@N:
  average declustering_proxy among top N

cluster_bridge_rate@N:
  share of top N candidates touching at least 2 clusters with meaningful contribution

too_close_rate@N:
  share of top N candidates with history_similarity > too_close_similarity

too_far_rate@N:
  share of top N candidates with history_similarity < too_far_similarity

serendipity_component_coverage:
  share of recommendations with non-neutral listener-diversity and declustering components
```

Output example:

```text
Serendipity evaluation

run_id: ...
mode: serendipity

recall@50: 0.039
intra_list_similarity@20: 0.31
novelty@20: 0.62
unserendipity@20: 0.37
listener_diversity_proxy@20: 0.58
declustering_proxy@20: 0.66
cluster_bridge_rate@20: 0.45
too_close_rate@20: 0.10
too_far_rate@20: 0.05
component_coverage: 0.93
```

Persist metrics in `evaluation_metric`.

## Explain command

Add:

```bash
scrobble-recs explain --artist "Artist Name" --run latest --config config.json
```

Output should include:

```text
artist
rank
mode
final score
basic relevance
listener diversity
declustering
novelty
knownness
obviousness
history similarity
nearest historical neighbors
clusters touched
feedback effects
candidate sources
```

Example:

```text
Artist: Candidate Artist
Run: latest
Mode: serendipity
Rank: 12
Final score: 0.917

Why recommended:
  Similar enough to your post-punk history, but connects to dream-pop and synthpop clusters too.

Component ranks:
  basic relevance:        0.09
  listener diversity:     0.21
  declustering:           0.04
  novelty:                0.33
  final rank blend:       0.083

Nearest history neighbors:
  Seed Artist A           similarity 0.61       cluster post-punk
  Seed Artist B           similarity 0.44       cluster dream pop

Penalties:
  knownness:              0.02
  obviousness:            0.00
```

## Tests

All tests must pass without network access.

Use deterministic fake data.

### New fixtures

Add fixtures:

```text
tests/fixtures/lastfm_artist_get_info_chameleons.json
tests/fixtures/serendipity_graph.json
tests/fixtures/serendipity_vectors.json
tests/fixtures/serendipity_clusters.json
```

### Fake graph fixture idea

Use a small graph with clear clusters.

```json
{
  "artists": [
    {"id": 1, "name": "History A1", "cluster": "post-punk", "listened": true, "weight": 1.0},
    {"id": 2, "name": "History A2", "cluster": "post-punk", "listened": true, "weight": 0.9},
    {"id": 3, "name": "History B1", "cluster": "dream pop", "listened": true, "weight": 0.8},
    {"id": 4, "name": "History B2", "cluster": "dream pop", "listened": true, "weight": 0.7},
    {"id": 10, "name": "Candidate Embedded", "listened": false},
    {"id": 11, "name": "Candidate Bridge", "listened": false},
    {"id": 12, "name": "Candidate Random", "listened": false}
  ],
  "similarities": [
    [10, 1, 0.90],
    [10, 2, 0.86],
    [10, 3, 0.02],
    [10, 4, 0.01],

    [11, 1, 0.48],
    [11, 2, 0.42],
    [11, 3, 0.39],
    [11, 4, 0.36],

    [12, 1, 0.01],
    [12, 2, 0.02],
    [12, 3, 0.01],
    [12, 4, 0.00],

    [1, 2, 0.92],
    [3, 4, 0.88],
    [1, 3, 0.10],
    [1, 4, 0.08],
    [2, 3, 0.07],
    [2, 4, 0.06]
  ]
}
```

Expected:

```text
Candidate Embedded:
  high basic relevance
  high history similarity
  low declustering
  low listener diversity

Candidate Bridge:
  good basic relevance
  medium history similarity
  high declustering
  high listener diversity

Candidate Random:
  low basic relevance
  too far
  should not be promoted despite novelty
```

### Unit tests

Add tests for:

```text
sparse vector norm
sparse vector normalization
sparse cosine
vector blending
artist vector build from tags
artist vector build from similarity edges
popularity parsing from artist.getInfo
popularity percentile calculation
novelty rank
history similarity calculation
unserendipity metric
listener-diversity proxy entropy
listener-diversity popularity adjustment
declustering proxy embeddedness calculation
rank interpolation
neutral component behavior
serendipity guardrails
score component persistence
explanation JSON includes serendipity fields
balanced mode unchanged
serendipity mode ranking differs from balanced mode on fixture
```

### Integration test

Use temporary DuckDB file.

Flow:

```text
1. import tiny CSV
2. run v0.2 mocked enrichment
3. build profile
4. build vectors
5. build clusters
6. insert fake popularity
7. recommend balanced
8. recommend serendipity
9. assert both runs exist
10. assert serendipity has recommendation_score_component rows
11. assert Candidate Bridge outranks Candidate Embedded in serendipity mode when fixture is designed that way
12. assert Candidate Random is not promoted above relevance floor
13. assert TUI/explain can read persisted component data without network
14. run evaluate --metrics serendipity
15. assert required metrics are printed/persisted
```

## Acceptance criteria

v0.3 is complete when:

1. Existing v0.2 tests still pass.
2. `build-vectors` builds and persists artist vectors.
3. `build-clusters` builds and persists user taste clusters or documented fallback clusters.
4. `artist.getInfo` enrichment can populate `artist_popularity` using mocked Last.fm responses.
5. `recommend --mode balanced` still works.
6. `recommend --mode serendipity` works.
7. Serendipity mode uses rank interpolation.
8. Score components are persisted in `recommendation_score_component`.
9. Serendipity mode computes:
   - basic relevance
   - novelty
   - history similarity
   - unserendipity
   - listener-diversity proxy
   - declustering proxy
   - final rank blend
10. Missing popularity data does not fail recommendation generation.
11. Sparse graph data does not fail recommendation generation.
12. Serendipity mode ranking differs from balanced mode on deterministic fixture data.
13. Candidate with medium relevance and cluster-bridging evidence can be promoted.
14. Candidate with near-zero relevance is not promoted merely for novelty.
15. TUI shows serendipity explanation from persisted data.
16. `explain` command prints component ranks and evidence.
17. `evaluate --metrics serendipity` reports required metrics.
18. No tests require network access.
19. Docs clearly say this is Auralist-inspired, not exact Auralist.
20. `docs/implementation-log.md` is updated.

## Verification commands

Run:

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src
```

Run smoke commands:

```bash
scrobble-recs doctor --config tests/fixtures/config.test.json
scrobble-recs import --config tests/fixtures/config.test.json
scrobble-recs build-profile --config tests/fixtures/config.test.json
scrobble-recs enrich plan --source lastfm --config tests/fixtures/config.test.json
scrobble-recs enrich run --source lastfm --limit 100 --config tests/fixtures/config.test.json
scrobble-recs build-vectors --config tests/fixtures/config.test.json
scrobble-recs build-clusters --config tests/fixtures/config.test.json
scrobble-recs recommend --mode balanced --limit 25 --config tests/fixtures/config.test.json
scrobble-recs recommend --mode serendipity --limit 25 --config tests/fixtures/config.test.json
scrobble-recs evaluate --metrics serendipity --config tests/fixtures/config.test.json
scrobble-recs explain --artist "Candidate Bridge" --run latest --config tests/fixtures/config.test.json
```

Use mocked Last.fm responses for smoke tests.

## Documentation updates

Update:

```text
README.md
docs/spec.md
docs/plan.md
docs/implementation-log.md
docs/decisions.md
```

Add a doc section:

```markdown
## Serendipity mode

Serendipity mode is inspired by Auralist but is not an exact implementation.

The app does not yet train Artist-based LDA over multi-user listening data. Instead, it approximates Auralist-like behavior using:

- v0.2 basic relevance
- Last.fm similarity graph
- Last.fm tag vectors
- local user taste clusters
- optional Last.fm popularity
- listener-diversity proxy
- declustering proxy
- rank interpolation

Exact Artist-LDA and multi-user collaborative modeling are deferred to a later version.
```

## Implementation log

Update `docs/implementation-log.md`:

```markdown
## v0.3 implementation

### Added

- artist vectors
- user taste clusters
- popularity enrichment
- novelty rank
- listener-diversity proxy
- declustering proxy
- rank-interpolated serendipity mode
- serendipity evaluation metrics
- TUI serendipity panel

### Changed

- recommendation explanations now include serendipity components
- evaluation now supports metric sets

### Tests

- `python -m pytest`
- ...

### Known limitations

- This is Auralist-inspired, not exact Artist-LDA.
- Listener diversity is approximated with local taste clusters.
- Declustering is approximated with the local artist graph.
- Popularity depends on available Last.fm getInfo data.

### Follow-up

- v0.4 could import ListenBrainz dumps.
- v0.4/v0.5 could train real collaborative embeddings or Artist-LDA.
```

## Important implementation warnings

- Do not break balanced mode.
- Do not claim exact Auralist.
- Do not add ListenBrainz dump import in v0.3.
- Do not require popularity data.
- Do not let novelty recommend irrelevant artists.
- Do not compute serendipity from final recommendation list alone; compute candidate-level components first.
- Do not call APIs from TUI.
- Do not require network access in tests.
- Do not hide the component math behind opaque abstractions.
- Keep explanation JSON inspectable.