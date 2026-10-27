"""Source-local reciprocal rank fusion; raw provider values never cross lists."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite

from scrobble_recs.models.evidence import EvidenceFamily


@dataclass(frozen=True)
class SourceCandidate:
    artist_id: int
    source: str
    family: EvidenceFamily
    source_rank: int | None
    ranked: bool
    confidence: float

    def __post_init__(self) -> None:
        expected_family = {
            "lastfm_similarity": "collaborative",
            "lastfm_walk": "collaborative",
            "listenbrainz": "collaborative",
            "semantic": "semantic",
            "musicbrainz": "catalog",
            "local_feedback": "personal",
        }.get(self.source)
        if expected_family is not None and self.family != expected_family:
            raise ValueError("retriever source and evidence family disagree")
        if type(self.ranked) is not bool or self.ranked != (self.source_rank is not None):
            raise ValueError("ranked and source_rank must agree")
        if self.source_rank is not None and (
            type(self.source_rank) is not int or self.source_rank < 1
        ):
            raise ValueError("source_rank must be a positive integer")
        if not isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be finite and between zero and one")
        if self.family not in ("collaborative", "semantic", "catalog", "personal"):
            raise ValueError("unknown evidence family")


@dataclass(frozen=True)
class FusedCandidate:
    artist_id: int
    fusion_score: float
    source_contributions: dict[str, float]
    family_contributions: dict[str, float]
    independent_family_count: int
    unranked_sources: tuple[str, ...]


def fuse_source_results(
    candidates: Sequence[SourceCandidate],
    *,
    rrf_constant: float,
    source_weights: Mapping[str, float],
    family_caps: Mapping[EvidenceFamily, float],
) -> tuple[FusedCandidate, ...]:
    if not isfinite(rrf_constant) or rrf_constant <= 0:
        raise ValueError("rrf_constant must be positive and finite")
    if any(not isfinite(v) or v < 0 for v in (*source_weights.values(), *family_caps.values())):
        raise ValueError("source weights and family caps must be finite and nonnegative")
    families: dict[str, EvidenceFamily] = {}
    unique: dict[tuple[int, str], SourceCandidate] = {}
    for row in candidates:
        if row.source in families and families[row.source] != row.family:
            raise ValueError("a source must belong to exactly one evidence family")
        families[row.source] = row.family
        key = row.artist_id, row.source
        previous = unique.get(key)
        # A ranked observation wins over membership; best rank wins conflicting
        # duplicates. Confidence is diagnostic only and never multiplies RRF.
        if previous is None or (row.source_rank or float("inf"), -row.confidence) < (
            previous.source_rank or float("inf"),
            -previous.confidence,
        ):
            unique[key] = row
    by_artist: dict[int, list[SourceCandidate]] = {}
    for key in sorted(unique):
        by_artist.setdefault(key[0], []).append(unique[key])
    result = []
    for artist, rows in by_artist.items():
        contributions = {
            row.source: source_weights.get(row.source, 0.0) / (rrf_constant + row.source_rank)
            for row in rows
            if row.source_rank is not None
        }
        totals: dict[EvidenceFamily, float] = {}
        for row in rows:
            if row.source_rank is not None:
                totals[row.family] = totals.get(row.family, 0.0) + contributions[row.source]
        capped = {
            family: min(total, family_caps.get(family, total))
            for family, total in sorted(totals.items())
        }
        result.append(
            FusedCandidate(
                artist,
                sum(capped.values()),
                contributions,
                {str(key): value for key, value in capped.items()},
                len({row.family for row in rows}),
                tuple(row.source for row in rows if not row.ranked),
            )
        )
    return tuple(sorted(result, key=lambda row: (-row.fusion_score, row.artist_id)))
