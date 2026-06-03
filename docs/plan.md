# Plan

## Milestone 1: Import and stats

Build CSV import, DuckDB schema, normalization, deduplication, and artist facts.

Done when:
- tests pass
- import works on fixture CSV
- stats command prints expected counts

## Milestone 2: Local scoring skeleton

Build knownness, rediscovery, recommendation run persistence, and CSV export without external APIs.

## Milestone 3: Last.fm enrichment

Add cache-first Last.fm enrichment with mocked tests.

## Milestone 4: TUI

Browse persisted recommendations and record feedback.

## Milestone 5: MusicBrainz resolution

Add optional identity resolution.

## Milestone 6: ListenBrainz collaborative model

Add optional collaborative recommender.