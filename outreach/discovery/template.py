"""Header-only CSV template generation; no example creator data is included."""

from __future__ import annotations

import csv
import io


CSV_COLUMNS = (
    "name",
    "platform",
    "profile_url",
    "follower_count",
    "follower_count_status",
    "follower_count_source_url",
    "engagement_rate",
    "engagement_rate_status",
    "engagement_rate_source_url",
    "niche",
    "content_themes",
    "content_themes_status",
    "content_themes_source_url",
    "profile_description",
    "profile_description_status",
    "profile_description_source_url",
    "content_evidence",
    "content_evidence_status",
    "content_evidence_source_url",
    "niche_status",
    "niche_source_url",
    "business_email",
    "contact_email_status",
    "contact_email_source_url",
    "website",
    "source_url",
    "collected_at",
)


def generate_csv_template() -> bytes:
    """Return a UTF-8 CSV containing the documented headers and zero data rows."""
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\n").writerow(CSV_COLUMNS)
    return buffer.getvalue().encode("utf-8")
