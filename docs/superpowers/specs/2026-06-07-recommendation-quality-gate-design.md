# Recommendation Quality Gate Design

## Problem

The recommender currently lets rediscovery act as an independent recommendation
source. Old, low-play local artists can rank highly even when they have no
meaningful Last.fm seed similarity, no meaningful tag fit, and no positive
feedback signal. This produces repeated and low-quality recommendations such as
old one-off listens, broad tag artifacts, and artists surfaced only through
noisy tag-top-artist rows.

The latest local diagnostics showed the failure mode clearly:

- Several top recommendations had `lastfm_similarity_score = 0.0`.
- Some had near-zero tag fit.
- High `rediscovery_score` was enough to keep them near the top.
- Repeated runs resurfaced many of the same artists because prior exposure does
  not affect later runs.
- Broad tags such as `rock`, `classic rock`, `soundtrack`, `indie`,
  `electronic`, and `pop` dominate the current tag profile.

Rediscovery should still exist, but it must require a current taste match.

## Goals

- Prevent rediscovery from recommending artists with no current evidence.
- Keep old low-play artists eligible when they match current taste.
- Reduce nonsense tag-expansion candidates from broad or weak Last.fm tags.
- Reduce repeated recommendations across runs.
- Make all filtering and penalty decisions visible in explanation JSON.
- Preserve local-first behavior and test determinism.

## Non-Goals

- Do not call live APIs during recommendation scoring.
- Do not require MusicBrainz, ListenBrainz, Spotify, or external search.
- Do not hard-delete enriched rows from the local database.
- Do not ban all previously listened artists.
- Do not turn the recommender into a black-box model.

## Recommended Approach

Add a candidate quality gate before final scoring. A candidate must have at
least one current taste-match signal before rediscovery can boost it:

- Last.fm seed similarity above a configured threshold.
- Tag similarity above a configured threshold.
- Positive feedback or feedback-neighborhood evidence above a configured
  threshold.

Rediscovery remains a score component, but only after the candidate passes the
gate. In rediscovery mode, thresholds may be relaxed, but the gate must still
exist.

## Configuration

Add this optional config block under `recommendation`:

```json
{
  "quality_gate": {
    "enabled": true,
    "rediscovery_requires_taste_match": true,
    "min_lastfm_similarity_score": 0.01,
    "min_tag_similarity_score": 0.05,
    "min_positive_feedback_score": 0.05,
    "rediscovery_mode_multiplier": 0.75
  },
  "fatigue": {
    "enabled": true,
    "lookback_runs": 5,
    "latest_run_penalty": 0.12,
    "repeat_appearance_penalty": 0.04
  }
}
```

Rules:

- Older configs remain valid.
- If `enabled` is false, existing behavior is preserved for debugging.
- If `rediscovery_requires_taste_match` is true, `rediscovery_score` is never a
  standalone pass reason.
- If `rediscovery_requires_taste_match` is false, rediscovery may count as a
  pass reason. This is not the recommended default.
- `rediscovery_mode_multiplier` relaxes thresholds in rediscovery mode only.
- Fatigue penalties are subtracted after raw scoring and before diversity
  reranking.

## Candidate Gate

For each candidate, compute component scores as the recommender already does:

```text
lastfm_similarity_score
tag_similarity_score
feedback_score
rediscovery_score
```

Then evaluate:

```text
has_lastfm_match = lastfm_similarity_score >= min_lastfm_similarity_score
has_tag_match = tag_similarity_score >= min_tag_similarity_score
has_feedback_match = feedback_score >= min_positive_feedback_score

passes_quality_gate =
    has_lastfm_match
    or has_tag_match
    or has_feedback_match
```

If `passes_quality_gate` is false, skip the candidate before applying
rediscovery to the raw score, knownness, obviousness, fatigue, diversity, or
persistence.

This means a high `rediscovery_score` can improve ranking only after a current
taste-match signal exists.

## Tag Hygiene

The current tag profile should be damped before it drives candidate generation
or tag fit. Add tag specificity and weak-tag penalties.

Specificity:

```text
tag_specificity = 1 / log(2 + number_of_seed_artists_with_tag)
```

Weak tags receive a multiplicative penalty rather than being deleted. Defaults
should include:

```text
seen live
favorite
favorites
favourite
lastfm
spotify
soundtrack
spoken word
rock
indie
alternative
pop
electronic
classic rock
female vocalists
80s
90s
70s
american
canadian
british
japanese
```

These defaults are intentionally conservative and configurable. The point is
not that every broad tag is wrong; it is that broad tags should not dominate
the profile enough to swamp more specific taste evidence.

## Cross-Run Fatigue

Add a recommendation exposure penalty based on prior persisted runs.

Default behavior:

- Penalize artists shown in the last 5 runs.
- Strongly penalize artists shown in the latest run.
- Do not apply fatigue to artists with positive feedback recorded after their
  most recent recommendation appearance.
- Persist fatigue value in explanation JSON.

This addresses repeated recommendations without requiring destructive cleanup
or manual feedback on every repeated artist.

## Explanation JSON

Each persisted recommendation should include gate and fatigue details:

```json
{
  "quality_gate": {
    "passed": true,
    "reasons": ["lastfm_similarity", "tag_similarity"],
    "thresholds": {
      "min_lastfm_similarity_score": 0.01,
      "min_tag_similarity_score": 0.05,
      "min_positive_feedback_score": 0.05
    },
    "scores": {
      "lastfm_similarity_score": 0.12,
      "tag_similarity_score": 0.18,
      "feedback_score": 0.0,
      "rediscovery_score": 0.64
    }
  },
  "fatigue": {
    "prior_run_appearances": 2,
    "latest_run_rank": 14,
    "penalty": 0.08
  }
}
```

Skipped candidates do not need to be persisted, but tests should exercise the
gate directly so the behavior remains inspectable.

## Testing

Add behavioral tests for:

- A high-rediscovery candidate with zero Last.fm, zero tag fit, and zero
  feedback is skipped.
- A high-rediscovery candidate with meaningful tag fit is eligible.
- A high-rediscovery candidate with meaningful seed similarity is eligible.
- Rediscovery mode relaxes thresholds but does not remove the gate.
- Weak/generic tags contribute less than specific tags.
- Prior-run appearances demote repeated recommendations.
- Positive feedback bypasses or reduces fatigue.
- Explanation JSON includes quality gate and fatigue details.

All tests must use deterministic local DuckDB fixtures and mocked clients. No
test should call Last.fm.

## Documentation

Update:

- `README.md` recommendation algorithm summary.
- `docs/spec-v02.md` rediscovery, tag profile, and ranking sections.
- `docs/implementation-log.md` with implementation and verification details.

Also replace any committed literal Last.fm API key in local config examples with
`${LASTFM_API_KEY}` before committing related changes.
