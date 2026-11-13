"""Shared provider and import data contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


VerificationStatus = str  # Runtime-validated against MISSING/UNVERIFIED/VERIFIED.


@dataclass(frozen=True, slots=True)
class FieldProvenance:
    verification_status: VerificationStatus
    source_url: str | None = None
    evidence_text: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryRecord:
    name: str
    platform: str
    profile_url: str
    source_url: str
    collected_at: str
    follower_count: int | None = None
    engagement_rate: float | None = None
    niche: str | None = None
    content_themes: tuple[str, ...] = ()
    content_evidence: str | None = None
    profile_description: str | None = None
    niche_evidence: tuple[dict[str, str | None], ...] = ()
    business_email: str = "Not Found"
    website: str | None = None
    field_provenance: dict[str, FieldProvenance] = field(default_factory=dict)
    source_row_number: int = 0
    provided_fields: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    """A generic provider request.

    API providers can use query/filters/cursor. File-backed providers consume
    source_data and may ignore query fields.
    """

    query: str = ""
    filters: dict[str, object] = field(default_factory=dict)
    cursor: str | None = None
    source_data: bytes | None = None
    source_name: str | None = None


@dataclass(frozen=True, slots=True)
class ImportIssue:
    row_number: int
    reason: str
    record_key: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryBatch:
    records: tuple[DiscoveryRecord, ...] = ()
    rejected: tuple[ImportIssue, ...] = ()
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class ImportedRecord:
    row_number: int
    influencer_id: int
    profile_url: str


@dataclass(frozen=True, slots=True)
class ImportReport:
    imported: tuple[ImportedRecord, ...] = ()
    updated: tuple[ImportedRecord, ...] = ()
    duplicates: tuple[ImportIssue, ...] = ()
    rejected: tuple[ImportIssue, ...] = ()

    @property
    def imported_count(self) -> int:
        return len(self.imported)

    @property
    def duplicate_count(self) -> int:
        return len(self.duplicates)

    @property
    def updated_count(self) -> int:
        return len(self.updated)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)


class DiscoveryProvider(Protocol):
    """Contract implemented by CSV, marketplace, and API discovery sources."""

    provider_name: str

    def discover(self, request: DiscoveryRequest) -> DiscoveryBatch:
        """Return normalized records and row-level validation issues."""
