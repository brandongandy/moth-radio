from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import log1p
from typing import TypedDict

from duckdb import DuckDBPyConnection

from scrobble_recs.models.modes import SeedRegionName, SeedRegionSetting


@dataclass(frozen=True)
class SeedArtist:
    artist_id: int
    name: str
    artist_key: str
    lastfm_name: str
    play_count: int
    seed_weight: float
    regions: tuple[str, ...] = ()


@dataclass
class _SeedAccumulator:
    artist_id: int
    name: str
    artist_key: str
    lastfm_name: str
    play_count: int
    seed_weight: float
    regions: set[str]


class _SeedRow(TypedDict):
    artist_id: int
    name: str
    artist_key: str
    lastfm_name: str
    play_count: int
    lifetime_weight: float
    recent_weight: float
    knownness: float
    rediscovery_score: float


def select_seed_artists(
    conn: DuckDBPyConnection,
    *,
    limit: int,
    min_scrobbles: int,
    regions: Sequence[SeedRegionSetting] | None = None,
) -> list[SeedArtist]:
    if limit < 1:
        return []
    resolved_regions: Sequence[SeedRegionSetting] = (
        regions if regions is not None else (("recent", limit, 1.0),)
    )
    rows = _load_seed_rows(conn, min_scrobbles=min_scrobbles)
    selected: dict[int, _SeedAccumulator] = {}
    total_region_limit = sum(
        region_limit for _, region_limit, _ in resolved_regions if region_limit > 0
    )
    backfill_regions: list[tuple[SeedRegionName, float, list[_SeedRow], int]] = []
    for region_name, region_limit, multiplier in resolved_regions:
        effective_limit = _effective_region_limit(
            caller_limit=limit,
            region_limit=region_limit,
            total_region_limit=total_region_limit,
        )
        if effective_limit < 1:
            continue
        ranked = sorted(
            rows,
            key=lambda row: (-_region_score(row, region_name), row["name"]),
        )
        taken = 0
        missing_backfill_slots = 0
        for row in ranked:
            region_score = _region_score(row, region_name)
            if region_score <= 0.0:
                continue
            accumulator = selected.get(int(row["artist_id"]))
            weighted_score = region_score * multiplier
            if accumulator is None:
                _add_seed_accumulator(
                    selected,
                    row=row,
                    region_name=region_name,
                    weighted_score=weighted_score,
                )
            elif _add_region_score(
                accumulator,
                region_name=region_name,
                weighted_score=weighted_score,
            ):
                missing_backfill_slots += 1
            taken += 1
            if taken >= effective_limit:
                break
        if missing_backfill_slots > 0:
            backfill_regions.append((region_name, multiplier, ranked, missing_backfill_slots))

    if len(selected) < limit:
        for region_name, multiplier, ranked, missing_backfill_slots in backfill_regions:
            deferred_existing: list[tuple[_SeedAccumulator, float]] = []
            for row in ranked:
                if missing_backfill_slots <= 0:
                    break
                artist_id = int(row["artist_id"])
                region_score = _region_score(row, region_name)
                if region_score <= 0.0:
                    continue
                weighted_score = region_score * multiplier
                accumulator = selected.get(artist_id)
                if accumulator is not None:
                    if region_name not in accumulator.regions:
                        deferred_existing.append((accumulator, weighted_score))
                    continue
                if len(selected) < limit:
                    _add_seed_accumulator(
                        selected,
                        row=row,
                        region_name=region_name,
                        weighted_score=weighted_score,
                    )
                    missing_backfill_slots -= 1
            for accumulator, weighted_score in deferred_existing:
                if missing_backfill_slots <= 0:
                    break
                if _add_region_score(
                    accumulator,
                    region_name=region_name,
                    weighted_score=weighted_score,
                ):
                    missing_backfill_slots -= 1

    ranked_seeds = sorted(
        selected.values(),
        key=lambda item: (-item.seed_weight, item.name),
    )[:limit]
    return [
        SeedArtist(
            artist_id=item.artist_id,
            name=item.name,
            artist_key=item.artist_key,
            lastfm_name=item.lastfm_name,
            play_count=item.play_count,
            seed_weight=item.seed_weight,
            regions=tuple(sorted(item.regions)),
        )
        for item in ranked_seeds
    ]


def _add_seed_accumulator(
    selected: dict[int, _SeedAccumulator],
    *,
    row: _SeedRow,
    region_name: str,
    weighted_score: float,
) -> None:
    selected[int(row["artist_id"])] = _SeedAccumulator(
        artist_id=int(row["artist_id"]),
        name=str(row["name"]),
        artist_key=str(row["artist_key"]),
        lastfm_name=str(row["lastfm_name"]),
        play_count=int(row["play_count"]),
        seed_weight=weighted_score,
        regions={region_name},
    )


def _add_region_score(
    accumulator: _SeedAccumulator,
    *,
    region_name: str,
    weighted_score: float,
) -> bool:
    if region_name in accumulator.regions:
        return False
    accumulator.seed_weight += weighted_score
    accumulator.regions.add(region_name)
    return True


def _load_seed_rows(
    conn: DuckDBPyConnection,
    *,
    min_scrobbles: int,
) -> list[_SeedRow]:
    rows = conn.execute(
        """
        SELECT
            a.artist_id,
            a.canonical_name,
            a.artist_key,
            coalesce(nullif(a.lastfm_name, ''), a.canonical_name) AS lastfm_name,
            f.play_count,
            coalesce(f.lifetime_weight, ln(1 + f.play_count)) AS lifetime_weight,
            coalesce(f.recent_weight, f.lifetime_weight, ln(1 + f.play_count)) AS recent_weight,
            coalesce(f.knownness, 0.0) AS knownness,
            coalesce(f.rediscovery_score, 0.0) AS rediscovery_score,
            f.last_played_at
        FROM artist a
        JOIN artist_listening_fact f ON f.artist_id = a.artist_id
        WHERE f.play_count >= ?
        """,
        [min_scrobbles],
    ).fetchall()
    return [
        {
            "artist_id": int(row[0]),
            "name": str(row[1]),
            "artist_key": str(row[2]),
            "lastfm_name": str(row[3]),
            "play_count": int(row[4]),
            "lifetime_weight": float(row[5]),
            "recent_weight": float(row[6]),
            "knownness": float(row[7]),
            "rediscovery_score": float(row[8]),
        }
        for row in rows
    ]


def _effective_region_limit(
    *,
    caller_limit: int,
    region_limit: int,
    total_region_limit: int,
) -> int:
    if region_limit < 1 or total_region_limit < 1:
        return 0
    proportional_limit = caller_limit * region_limit / total_region_limit
    return max(1, int(proportional_limit + 0.5))


def _region_score(row: _SeedRow, region_name: str) -> float:
    lifetime_weight = float(row["lifetime_weight"])
    recent_weight = float(row["recent_weight"])
    knownness = float(row["knownness"])
    rediscovery_score = float(row["rediscovery_score"])
    play_count = int(row["play_count"])
    if region_name == "lifetime":
        return lifetime_weight
    if region_name == "recent":
        return recent_weight
    if region_name == "old_obsession":
        return lifetime_weight * rediscovery_score
    if region_name == "underexplored":
        if play_count > 30:
            return 0.0
        return lifetime_weight * max(0.0, 1.0 - knownness)
    return log1p(play_count)
