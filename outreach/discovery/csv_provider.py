"""CSV-backed discovery provider with row-level validation."""

from __future__ import annotations

import csv
import io
import math
import re
from datetime import datetime, timezone

from outreach.database import (
    normalize_business_email,
    normalize_profile_url,
    validate_profile_url,
)
from outreach.discovery.models import (
    DiscoveryBatch,
    DiscoveryRecord,
    DiscoveryRequest,
    FieldProvenance,
    ImportIssue,
)


REQUIRED_COLUMNS = {"name", "platform", "profile_url", "source_url", "collected_at"}
FIELD_MAP = {
    "follower_count": ("follower_count_status", "follower_count_source_url"),
    "engagement_rate": ("engagement_rate_status", "engagement_rate_source_url"),
    "contact_email": ("contact_email_status", "contact_email_source_url"),
    "content_evidence": ("content_evidence_status", "content_evidence_source_url"),
}
VALID_STATUSES = {"MISSING", "UNVERIFIED", "VERIFIED"}
MAX_IMPORT_BYTES = 10 * 1024 * 1024


class CSVDiscoveryProvider:
    """Read creator candidates from operator-prepared, authorized CSV data."""

    provider_name = "csv"

    def discover(self, request: DiscoveryRequest) -> DiscoveryBatch:
        if request.source_data is None:
            raise ValueError("CSV provider requires source_data in the discovery request.")
        if len(request.source_data) > MAX_IMPORT_BYTES:
            raise ValueError("CSV file exceeds the 10 MiB import limit.")

        try:
            content = request.source_data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return DiscoveryBatch(rejected=(ImportIssue(1, "File must use UTF-8 encoding."),))

        reader = csv.DictReader(io.StringIO(content, newline=""), strict=True)
        try:
            fieldnames = reader.fieldnames
        except csv.Error as exc:
            return DiscoveryBatch(rejected=(ImportIssue(1, f"Malformed CSV header: {exc}"),))
        if not fieldnames:
            return DiscoveryBatch(rejected=(ImportIssue(1, "CSV file is empty or has no header row."),))
        headers = [header.strip() for header in fieldnames]
        if len(set(headers)) != len(headers):
            return DiscoveryBatch(rejected=(ImportIssue(1, "CSV contains duplicate column names."),))
        missing_headers = sorted(REQUIRED_COLUMNS - set(headers))
        if missing_headers:
            return DiscoveryBatch(
                rejected=(ImportIssue(1, f"Missing required columns: {', '.join(missing_headers)}."),)
            )
        reader.fieldnames = headers

        records: list[DiscoveryRecord] = []
        rejected: list[ImportIssue] = []
        rows_seen = False
        try:
            for row_number, raw_row in enumerate(reader, start=2):
                rows_seen = True
                if None in raw_row:
                    rejected.append(ImportIssue(row_number, "Row has more values than the CSV header."))
                    continue
                row = {key.strip(): (value or "") for key, value in raw_row.items() if key}
                try:
                    records.append(self._parse_row(row, row_number=row_number))
                except ValueError as exc:
                    rejected.append(
                        ImportIssue(row_number, str(exc), record_key=row.get("profile_url") or None)
                    )
        except csv.Error as exc:
            rejected.append(
                ImportIssue(max(reader.line_num, 1), f"Malformed CSV data: {exc}")
            )
        if not rows_seen and not rejected:
            rejected.append(ImportIssue(2, "CSV contains headers but no influencer records."))
        return DiscoveryBatch(records=tuple(records), rejected=tuple(rejected))

    def _parse_row(self, row: dict[str, str], *, row_number: int) -> DiscoveryRecord:
        name = row.get("name", "").strip()
        platform = row.get("platform", "").strip().upper()
        if not name:
            raise ValueError("name is required.")
        if not platform:
            raise ValueError("platform is required.")

        profile_url = normalize_profile_url(row.get("profile_url", ""), platform)
        source_url = validate_profile_url(row.get("source_url", ""))
        collected_at = _normalize_timestamp(row.get("collected_at", ""))

        follower_count = _optional_int(row.get("follower_count", ""), field_name="follower_count")
        engagement_rate = _optional_float(
            row.get("engagement_rate", ""), field_name="engagement_rate", allow_percent=True
        )
        if engagement_rate is not None and not 0 <= engagement_rate <= 100:
            raise ValueError("engagement_rate must be between 0 and 100 percent.")

        business_email = normalize_business_email(row.get("business_email", ""))
        website = _optional_url(row.get("website", ""), field_name="website")
        content_evidence = row.get("content_evidence", "").strip() or None
        profile_description = row.get("profile_description", "").strip() or None
        themes = tuple(
            dict.fromkeys(
                theme.strip()
                for theme in re.split(r"[|;]", row.get("content_themes", ""))
                if theme.strip()
            )
        )
        provenance: dict[str, FieldProvenance] = {}
        values_present = {
            "follower_count": follower_count is not None,
            "engagement_rate": engagement_rate is not None,
            "contact_email": business_email != "Not Found",
            "content_evidence": content_evidence is not None,
        }
        for field_name, (status_column, source_column) in FIELD_MAP.items():
            supplied_status = row.get(status_column, "").strip().upper()
            has_value = values_present[field_name]
            status = supplied_status or ("UNVERIFIED" if has_value else "MISSING")
            if status not in VALID_STATUSES:
                raise ValueError(f"{status_column} must be MISSING, UNVERIFIED, or VERIFIED.")
            if has_value and status == "MISSING":
                raise ValueError(f"{field_name} has a value but its status is MISSING.")
            if not has_value and status != "MISSING":
                raise ValueError(f"{field_name} is unavailable and must use MISSING status.")
            field_source = _optional_url(row.get(source_column, ""), field_name=source_column)
            if status == "VERIFIED" and (not has_value or not field_source):
                raise ValueError(
                    f"Verified {field_name} requires a value and its field-specific source URL."
                )
            provenance[field_name] = FieldProvenance(
                verification_status=status,
                source_url=field_source,
                evidence_text=content_evidence if field_name == "content_evidence" else None,
            )

        evidence_rows: list[dict[str, str | None]] = []
        for kind, text, status_key, source_key in (
            ("PROFILE_DESCRIPTION", profile_description, "profile_description_status", "profile_description_source_url"),
            ("CONTENT_EVIDENCE", content_evidence, "content_evidence_status", "content_evidence_source_url"),
        ):
            if text:
                status = row.get(status_key, "").strip().upper() or "UNVERIFIED"
                if status not in VALID_STATUSES or status == "MISSING":
                    raise ValueError(f"{status_key} must be UNVERIFIED or VERIFIED when text is supplied.")
                field_source = _optional_url(row.get(source_key, ""), field_name=source_key)
                if status == "VERIFIED" and not field_source:
                    raise ValueError(f"Verified {kind.lower()} requires its field-specific source URL.")
                evidence_rows.append({"evidence_kind": kind, "evidence_text": text,
                                      "source_url": field_source, "verification_status": status})
        for theme in themes:
            status = row.get("content_themes_status", "").strip().upper() or "UNVERIFIED"
            if status not in VALID_STATUSES or status == "MISSING":
                raise ValueError("content_themes_status must be UNVERIFIED or VERIFIED when themes are supplied.")
            theme_source = _optional_url(row.get("content_themes_source_url", ""), field_name="content_themes_source_url")
            if status == "VERIFIED" and not theme_source:
                raise ValueError("Verified content themes require their field-specific source URL.")
            evidence_rows.append({"evidence_kind": "CONTENT_THEME", "evidence_text": theme,
                                  "source_url": theme_source, "verification_status": status})
        if row.get("niche", "").strip():
            status = row.get("niche_status", "").strip().upper() or "UNVERIFIED"
            niche_source = _optional_url(row.get("niche_source_url", ""), field_name="niche_source_url")
            if status not in VALID_STATUSES or status == "MISSING":
                raise ValueError("niche_status must be UNVERIFIED or VERIFIED when niche is supplied.")
            if status == "VERIFIED" and not niche_source:
                raise ValueError("Verified niche requires its field-specific source URL.")
            evidence_rows.append({"evidence_kind": "DECLARED_NICHE", "evidence_text": row["niche"].strip(),
                                  "source_url": niche_source, "verification_status": status})

        return DiscoveryRecord(
            name=name,
            platform=platform,
            profile_url=profile_url,
            source_url=source_url,
            collected_at=collected_at,
            follower_count=follower_count,
            engagement_rate=engagement_rate,
            niche=row.get("niche", "").strip() or None,
            content_themes=themes,
            content_evidence=content_evidence,
            profile_description=profile_description,
            niche_evidence=tuple(evidence_rows),
            business_email=business_email,
            website=website,
            field_provenance=provenance,
            source_row_number=row_number,
            provided_fields=frozenset(
                field_name for field_name, column in {
                    "follower_count": "follower_count",
                    "engagement_rate": "engagement_rate",
                    "business_email": "business_email",
                    "website": "website",
                    "niche": "niche",
                    "content_themes": "content_themes",
                    "content_evidence": "content_evidence",
                    "profile_description": "profile_description",
                }.items()
                if row.get(column, "").strip()
            ),
        )


def _normalize_timestamp(value: str) -> str:
    if not value.strip():
        raise ValueError("collected_at is required and must be an ISO-8601 date/time.")
    clean = value.strip()
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?",
        clean,
    ):
        raise ValueError(
            "collected_at must use YYYY-MM-DDTHH:MM:SS with an optional timezone (Z or ±HH:MM)."
        )
    try:
        parsed = datetime.fromisoformat(clean[:-1] + "+00:00" if clean.endswith("Z") else clean)
    except ValueError as exc:
        raise ValueError("collected_at must be an ISO-8601 date/time.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _optional_int(value: str, *, field_name: str) -> int | None:
    if not value.strip():
        return None
    clean = value.strip()
    if not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)", clean):
        raise ValueError(f"{field_name} must be a non-negative integer with valid thousands separators or blank.")
    try:
        parsed = int(clean.replace(",", ""))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a whole number or blank.") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} must be zero or greater.")
    return parsed


def _optional_float(value: str, *, field_name: str, allow_percent: bool = False) -> float | None:
    if not value.strip():
        return None
    cleaned = value.strip()
    if allow_percent:
        cleaned = cleaned.removesuffix("%").strip()
    if not re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)", cleaned):
        raise ValueError(f"{field_name} must be a decimal number without thousands separators or blank.")
    try:
        parsed = float(cleaned)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a number or blank.") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field_name} must be a finite number.")
    return parsed


def _optional_url(value: str, *, field_name: str) -> str | None:
    if not value.strip():
        return None
    try:
        return validate_profile_url(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an absolute HTTP or HTTPS URL.") from exc
