"""Qualification result types and evidence snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class QualificationDecision(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True, slots=True)
class QualificationEvidence:
    id: int
    kind: str
    text: str
    source_url: str | None
    verification_status: str
    collected_at: str | None


@dataclass(frozen=True, slots=True)
class NicheMatch:
    niche: str
    matched_terms: tuple[str, ...]
    excerpt: str
    source_url: str
    evidence_id: int
    corroborating_themes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QualificationResult:
    decision: QualificationDecision
    reasons: tuple[str, ...]
    niche_matches: tuple[NicheMatch, ...]
    evidence_snapshot: tuple[QualificationEvidence, ...]
    rules_snapshot: dict[str, int | float]


@dataclass(frozen=True, slots=True)
class EmailOutreachReadiness:
    email_available: bool
    ready: bool
    reason: str
