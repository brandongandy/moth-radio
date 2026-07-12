# Spec: Discovery-First Recommendation Quality and Serendipity

Date: 2026-07-11

## Context

`scrobble-recs` is a local-first artist recommender built from imported Last.fm listening history and cached Last.fm enrichment. Recent recommendation runs show two related problems:

- standard runs mix rediscovery candidates with new discoveries, even though the intended default experience is discovery;
- generic tag matches and components with incompatible numeric ranges can outrank candidates with meaningful direct similarity evidence.

The current database already contains useful feedback: permanent hides, known artists, positive and negative preference votes, artist-similarity edges, tags, and local listening history. This phase uses that local evidence before adding a new external data dependency.

## Goals

- Make standard recommendations discovery-first.
- Build the default taste profile from the trailing 12 months of listening history.
- Prevent a voted-on recommendation from returning for 30 days.
- Keep `hide_artist` permanent.
- Exclude weak or generic-tag-only candidates before ranking.
- Calibrate component scores so ranking weights are meaningful.
- Add an Auralist-style, relevance-bounded serendipity reranker that rewards candidates at the edge of taste clusters.
- Make exclusions, quality decisions, calibrated contributions, and serendipity inspectable in persisted run data.
- Measure quality offline before considering ListenBrainz collaborative data.

## Non-goals

- Live API calls while generating recommendations or running tests.
- Making ListenBrainz data a dependency in this phase.
- Replacing the cache-first Last.fm enrichment path.
- Recommending tracks rather than artists.
- Allowing serendipity to bypass relevance or quality eligibility.

## User-facing behavior

### Discovery-first standard modes

All standard modes recommend only artists absent from local listening history. An artist with any persisted local listening history is not a discovery candidate, including a one-off historical listen. `rediscovery` remains the explicit mode for locally known artists.

The default profile window is the 12 months immediately before the run's `as_of` time. The `recommend` command accepts explicit `--since` and `--until` options for historical or targeted runs. The effective window, the resolved `as_of` time, and its source (default or explicit) are persisted with the run so results can be reproduced and evaluated.

### Vote cooldowns and permanent hides

An artist is ineligible for a standard or rediscovery recommendation for 30 days after a preference vote associated with a recommendation. The cooldown applies to `save`, `reject`, `already_know`, `too_obvious`, `wrong_genre`, `more_like_this`, `less_like_this`, `hide_tag`, `boost_tag`, `rediscovery_good`, and `rediscovery_bad`.

Operational events such as `opened_link`, `played_externally`, and `note` do not start a cooldown. `hide_artist` remains a permanent exclusion and is also represented as an exclusion reason. Feedback continues to contribute positive or negative neighborhood and tag effects after the direct candidate cooldown is applied.

## Architecture

The run uses a scoped profile rather than the globally refreshed profile alone:

```text
listening history in [since, until]
  -> scoped seeds and tag profile
  -> local seed similarity graph
  -> candidate generation
  -> permanent/cooldown/discovery exclusions
  -> evidence quality gate
  -> component calibration and relevance ranking
  -> serendipity reranking
  -> existing list-level diversity reranking
  -> persisted explanations, diagnostics, and export/TUI
```

The profile builder and seed selector receive an explicit profile-window value. Existing global listening aggregates remain useful for knownness, rediscovery, and diagnostic fields, but no longer define the default standard-mode taste profile.

The recommendation-run snapshot stores the effective profile window and scoring configuration in inspectable JSON. Candidate-level data records eligibility, exclusion or gate reason, raw evidence, calibrated contributions, and serendipity evidence. This preserves the existing local-first, persisted-run TUI model.

## Candidate eligibility and scoring

### Hard exclusions

The eligibility stage applies, in order:

1. Seed/self exclusion.
2. Permanent hidden-artist exclusion.
3. Discovery exclusion for artists with local listening history in non-rediscovery modes.
4. Thirty-day feedback cooldown.
5. Existing knownness and mode constraints where applicable.

Excluded candidates remain available to diagnostics with structured reason codes; they are not ranked or displayed as normal recommendations.

### Evidence quality gate

Normal discovery candidates must have meaningful direct Last.fm graph evidence from the scoped seeds:

- support from at least two independent seed artists; or
- one direct seed edge above a configured strong-edge threshold, together with a reinforcing specific signal.

Tags reinforce direct graph evidence but cannot admit a normal discovery candidate by themselves. Tag-only candidates can remain diagnostic candidates for enrichment and evaluation, never normal recommendations. Tag focus modes may define stricter, explicit evidence rules but do not inherit an implicit tag-only bypass.

The gate records passing and failing evidence counts, strongest edges, tag support, and reason codes in the candidate explanation.

### Calibrated ranking

The current raw feature ranges differ substantially: aggregate graph similarity can be numerically tiny while rediscovery and feedback scores are much larger. Raw score magnitude is therefore not a reliable quality measure.

For each run, compute a robust 0-1 calibrated value for each eligible component using its position in that run's eligible-candidate distribution. Preserve raw values unchanged for explanation and export. The relevance score combines calibrated graph, tag, feedback, knownness/obviousness penalties, and any mode-specific components using configured weights. Rediscovery contributes only in explicit rediscovery mode.

Score bands classify the calibrated, eligible ranking tail. Standard exports and the TUI show strong and plausible recommendations by default; diagnostics retain weaker and rejected candidates separately.

## Auralist-style serendipity reranking

The scoped seed artists form the local preference graph. Similarity edges between seeds are symmetrized for this calculation. For an eligible candidate:

1. Take its strongest qualifying seed neighbors.
2. Compute the weighted cohesion among those neighbors in the local seed graph.
3. Assign an edge-of-cluster bonus when the candidate has strong relevance but low neighbor cohesion.
4. Add a bridge bonus when meaningful neighbors span separate local graph components.

Candidates need at least two meaningful seed neighbors before receiving a serendipity bonus. This prevents sparse, arbitrary candidates from appearing novel merely because the graph lacks data. The bonus is bounded and only reranks candidates that already passed the direct-evidence quality gate. Existing MMR/list-level diversity stays as the final selection step.

The explanation includes qualifying neighbors, their direct edges, neighbor cohesion, graph components, bridge status, raw serendipity, calibrated serendipity, and final bonus. The system can therefore distinguish "relevant bridge" from "under-supported outlier."

## Configuration and command surface

New recommendation configuration covers:

- default `profile_window_months: 12`;
- cooldown duration, default 30 days;
- discovery-first enablement for standard modes;
- direct evidence thresholds and minimum independent seed support;
- component calibration method and bounds;
- serendipity enabled flag, maximum bonus, neighbor count, cohesion threshold, and bridge bonus.

`recommend` adds `--since` and `--until`, validates that the range is non-empty and ordered, and reports the resolved window in its run summary. If omitted, the range is the trailing configured 12 months ending at the run time.

## Data and explanation model

Schema changes add only additive migration columns/tables. The recommended shape is:

- run snapshot fields for profile range and configuration;
- candidate diagnostic fields for eligibility state and reason codes;
- recommendation fields or explanation payloads for raw/calibrated contributions and serendipity details.

Use structured JSON for evolving explanation data and stable columns for fields needed by TUI filtering, export, and evaluation. Never hide core scorer math behind opaque text; the human-readable summary is derived from structured evidence.

## Evaluation

Add deterministic temporal holdout evaluation:

1. Choose a historical cutoff.
2. Build the scoped profile from listening before the cutoff.
3. Generate recommendations without future data.
4. Treat artists first listened to within a fixed future horizon as positive outcomes.

The report compares the current baseline, hardened relevance-only ranking, and hardened-plus-serendipity ranking. It records precision at K, eligible-candidate coverage, future-listen coverage, novelty, excluded/repeated recommendation rate, and score/evidence distributions. Tests use fixtures and temporary DuckDB databases only.

## Failure handling

- An empty date range or invalid range fails before persisting a run.
- Insufficient scoped seeds or missing local graph coverage produces a persisted, inspectable run with actionable diagnostics, not fabricated tag-only recommendations.
- Missing candidate-to-seed support removes the candidate from normal output.
- Sparse seed graph disables serendipity for that candidate and records why.
- No live enrichment is attempted during recommendation generation; enrichment remains an explicit cache-first operation.

## Testing

Tests cover observable behavior:

- default and explicit profile windows select different seed histories correctly;
- standard modes exclude locally known artists and rediscovery mode can include eligible historical artists;
- every preference vote starts a 30-day cooldown while operational events do not;
- hidden artists remain excluded permanently after the cooldown interval;
- tag-only and single-weak-edge candidates fail the normal discovery gate;
- multi-seed and strong-plus-reinforcement candidates pass;
- calibrated component scores preserve expected ordering despite dissimilar raw ranges;
- a relevant cross-cluster bridge outranks an equally relevant deeply clustered candidate only within the configured bound;
- sparse graph candidates receive no artificial serendipity;
- temporal evaluation uses no future data and produces deterministic metrics;
- exports and TUI data expose structured eligibility and serendipity explanations.

## Deferred follow-on

After evaluation, review graph coverage and quality. Add offline ListenBrainz collaborative signals only if the hardened Last.fm graph lacks sufficient eligible discovery candidates or underperforms the baseline in temporal evaluation. Any such work is a separate spec and must remain local-first and testable without network access.
