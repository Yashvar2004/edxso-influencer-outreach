"""SQLite schema and access helpers."""

from __future__ import annotations

import json
import ipaddress
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit


SCHEMA_VERSION = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS influencers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    platform TEXT NOT NULL CHECK (length(trim(platform)) > 0),
    profile_url TEXT NOT NULL CHECK (length(trim(profile_url)) > 0),
    follower_count INTEGER CHECK (follower_count IS NULL OR follower_count >= 0),
    engagement_rate REAL CHECK (engagement_rate IS NULL OR (engagement_rate >= 0 AND engagement_rate <= 100)),
    niche TEXT,
    content_themes TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(content_themes)),
    business_email TEXT NOT NULL DEFAULT 'Not Found',
    website TEXT,
    audience_age TEXT,
    audience_gender TEXT,
    audience_geography TEXT,
    classification_status TEXT NOT NULL DEFAULT 'REVIEW_REQUIRED'
        CHECK (classification_status IN ('PASS', 'FAIL', 'REVIEW_REQUIRED')),
    classification_reasons TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(classification_reasons)),
    verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED'
        CHECK (verification_status IN ('VERIFIED', 'UNVERIFIED', 'UNAVAILABLE')),
    source_url TEXT,
    collected_at TEXT NOT NULL,
    content_evidence TEXT,
    profile_description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(platform, profile_url)
);

CREATE TABLE IF NOT EXISTS outreach_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK (channel IN ('EMAIL', 'INSTAGRAM_DM')),
    subject TEXT,
    body TEXT NOT NULL CHECK (length(trim(body)) > 0),
    provider TEXT,
    model TEXT,
    generated_at TEXT,
    generation_status TEXT NOT NULL DEFAULT 'DRAFT'
        CHECK (generation_status IN ('DRAFT', 'GENERATED', 'FAILED')),
    approval_status TEXT NOT NULL DEFAULT 'PENDING_REVIEW'
        CHECK (approval_status IN ('PENDING_REVIEW', 'APPROVED')),
    approved_at TEXT,
    current_version INTEGER NOT NULL DEFAULT 0 CHECK (current_version >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(influencer_id, channel)
);

CREATE TABLE IF NOT EXISTS outreach_message_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES outreach_messages(id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    subject TEXT,
    body TEXT NOT NULL CHECK (length(trim(body)) > 0),
    provider TEXT,
    model TEXT,
    generated_at TEXT,
    source_type TEXT NOT NULL CHECK (source_type IN ('GENERATED', 'MANUAL_EDIT', 'LEGACY')),
    prompt_version TEXT,
    evidence_snapshot TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(evidence_snapshot)),
    email_word_count INTEGER CHECK (email_word_count IS NULL OR email_word_count >= 0),
    dm_word_count INTEGER CHECK (dm_word_count IS NULL OR dm_word_count >= 0),
    approval_status TEXT NOT NULL DEFAULT 'PENDING_REVIEW'
        CHECK (approval_status IN ('PENDING_REVIEW', 'APPROVED')),
    approved_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(message_id, version_number)
);
CREATE INDEX IF NOT EXISTS idx_message_versions_message
    ON outreach_message_versions(message_id, version_number DESC);

CREATE TABLE IF NOT EXISTS outreach_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
    message_id INTEGER REFERENCES outreach_messages(id) ON DELETE SET NULL,
    channel TEXT NOT NULL CHECK (channel IN ('EMAIL', 'INSTAGRAM_DM')),
    status TEXT NOT NULL CHECK (status IN ('PENDING_REVIEW', 'SIMULATED_SENT', 'SENT', 'FAILED')),
    idempotency_key TEXT NOT NULL UNIQUE,
    attempted_at TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_influencers_classification
    ON influencers(classification_status);
CREATE INDEX IF NOT EXISTS idx_outreach_logs_influencer
    ON outreach_logs(influencer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS outreach_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE RESTRICT,
    channel TEXT NOT NULL CHECK (channel IN ('EMAIL', 'INSTAGRAM_DM')),
    mode TEXT NOT NULL CHECK (mode IN ('DEMO', 'LIVE', 'MANUAL')),
    message_id INTEGER REFERENCES outreach_messages(id) ON DELETE SET NULL,
    message_version_id INTEGER REFERENCES outreach_message_versions(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'SENDING', 'SIMULATED_SENT', 'SENT', 'FAILED', 'UNKNOWN',
        'MANUALLY_SENT', 'REPLIED', 'CANCELLED'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((message_id IS NULL) = (message_version_id IS NULL)),
    UNIQUE(influencer_id, channel, mode)
);
CREATE INDEX IF NOT EXISTS idx_outreach_campaign_status
    ON outreach_campaigns(status, channel, mode);

CREATE TABLE IF NOT EXISTS outreach_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES outreach_campaigns(id) ON DELETE RESTRICT,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    message_id INTEGER REFERENCES outreach_messages(id) ON DELETE SET NULL,
    message_version_id INTEGER REFERENCES outreach_message_versions(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'SENDING', 'SIMULATED_SENT', 'SENT', 'FAILED', 'UNKNOWN',
        'MANUALLY_SENT', 'REPLIED', 'CANCELLED'
    )),
    idempotency_key TEXT NOT NULL UNIQUE,
    started_at TEXT,
    completed_at TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    CHECK ((message_id IS NULL) = (message_version_id IS NULL)),
    UNIQUE(campaign_id, attempt_number)
);
CREATE INDEX IF NOT EXISTS idx_outreach_attempts_campaign
    ON outreach_attempts(campaign_id, attempt_number DESC);

CREATE TABLE IF NOT EXISTS outreach_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER REFERENCES outreach_campaigns(id) ON DELETE RESTRICT,
    attempt_id INTEGER REFERENCES outreach_attempts(id) ON DELETE RESTRICT,
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE RESTRICT,
    message_id INTEGER REFERENCES outreach_messages(id) ON DELETE SET NULL,
    message_version_id INTEGER REFERENCES outreach_message_versions(id) ON DELETE SET NULL,
    channel TEXT NOT NULL CHECK (channel IN ('EMAIL', 'INSTAGRAM_DM')),
    mode TEXT CHECK (mode IS NULL OR mode IN ('DEMO', 'LIVE', 'MANUAL')),
    event_type TEXT NOT NULL CHECK (event_type IN (
        'LEGACY_LOG_IMPORTED', 'PENDING', 'SEND_STARTED', 'SIMULATED_SENT', 'SENT',
        'FAILED', 'UNKNOWN', 'MANUALLY_SENT', 'REPLIED', 'CANCELLED',
        'RETRY_AUTHORIZED', 'OPERATOR_NOTE'
    )),
    status TEXT NOT NULL CHECK (status IN (
        'PENDING_REVIEW', 'PENDING', 'SENDING', 'SIMULATED_SENT', 'SENT', 'FAILED',
        'UNKNOWN', 'MANUALLY_SENT', 'REPLIED', 'CANCELLED'
    )),
    occurred_at TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json)),
    event_key TEXT NOT NULL UNIQUE,
    legacy_log_id INTEGER UNIQUE,
    created_at TEXT NOT NULL,
    CHECK (legacy_log_id IS NULL OR (campaign_id IS NULL AND attempt_id IS NULL AND message_version_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_outreach_activity_influencer
    ON outreach_activity(influencer_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_outreach_activity_status
    ON outreach_activity(status, occurred_at DESC);
CREATE TRIGGER IF NOT EXISTS outreach_activity_no_update
BEFORE UPDATE ON outreach_activity BEGIN
    SELECT RAISE(ABORT, 'outreach activity is append-only');
END;
CREATE TRIGGER IF NOT EXISTS outreach_activity_no_delete
BEFORE DELETE ON outreach_activity BEGIN
    SELECT RAISE(ABORT, 'outreach activity is append-only');
END;

CREATE TABLE IF NOT EXISTS influencer_field_provenance (
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL CHECK (field_name IN (
        'follower_count', 'engagement_rate', 'contact_email', 'content_evidence'
    )),
    verification_status TEXT NOT NULL CHECK (verification_status IN ('MISSING', 'UNVERIFIED', 'VERIFIED')),
    source_url TEXT,
    collected_at TEXT,
    evidence_text TEXT,
    PRIMARY KEY (influencer_id, field_name),
    CHECK (verification_status != 'VERIFIED' OR (source_url IS NOT NULL AND length(trim(source_url)) > 0))
);

CREATE TABLE IF NOT EXISTS qualification_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
    evaluated_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('PASS', 'FAIL', 'REVIEW_REQUIRED')),
    reasons TEXT NOT NULL CHECK (json_valid(reasons)),
    rules_snapshot TEXT NOT NULL CHECK (json_valid(rules_snapshot)),
    evidence_snapshot TEXT NOT NULL CHECK (json_valid(evidence_snapshot))
);
CREATE INDEX IF NOT EXISTS idx_qualification_evaluations_influencer
    ON qualification_evaluations(influencer_id, evaluated_at DESC);

CREATE TABLE IF NOT EXISTS niche_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
    evidence_kind TEXT NOT NULL CHECK (evidence_kind IN ('PROFILE_DESCRIPTION', 'CONTENT_THEME', 'CONTENT_EVIDENCE', 'DECLARED_NICHE')),
    evidence_text TEXT NOT NULL CHECK (length(trim(evidence_text)) > 0),
    source_url TEXT,
    verification_status TEXT NOT NULL CHECK (verification_status IN ('MISSING', 'UNVERIFIED', 'VERIFIED')),
    collected_at TEXT,
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    CHECK (verification_status != 'VERIFIED' OR (source_url IS NOT NULL AND length(trim(source_url)) > 0))
);
CREATE INDEX IF NOT EXISTS idx_niche_evidence_current
    ON niche_evidence(influencer_id, is_current, verification_status);
"""


MIGRATE_1_TO_2 = """
BEGIN IMMEDIATE;
ALTER TABLE influencers ADD COLUMN content_evidence TEXT;
CREATE TABLE influencer_field_provenance (
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL CHECK (field_name IN (
        'follower_count', 'engagement_rate', 'contact_email', 'content_evidence'
    )),
    verification_status TEXT NOT NULL CHECK (verification_status IN ('MISSING', 'UNVERIFIED', 'VERIFIED')),
    source_url TEXT,
    collected_at TEXT,
    evidence_text TEXT,
    PRIMARY KEY (influencer_id, field_name),
    CHECK (verification_status != 'VERIFIED' OR source_url IS NOT NULL)
);
INSERT INTO influencer_field_provenance
    (influencer_id, field_name, verification_status, source_url, collected_at)
SELECT id, 'follower_count',
       CASE WHEN follower_count IS NULL THEN 'MISSING' ELSE 'UNVERIFIED' END,
       source_url, collected_at FROM influencers;
INSERT INTO influencer_field_provenance
    (influencer_id, field_name, verification_status, source_url, collected_at)
SELECT id, 'engagement_rate',
       CASE WHEN engagement_rate IS NULL THEN 'MISSING' ELSE 'UNVERIFIED' END,
       source_url, collected_at FROM influencers;
INSERT INTO influencer_field_provenance
    (influencer_id, field_name, verification_status, source_url, collected_at)
SELECT id, 'contact_email',
       CASE WHEN lower(trim(business_email)) IN ('', 'not found') THEN 'MISSING' ELSE 'UNVERIFIED' END,
       source_url, collected_at FROM influencers;
INSERT INTO influencer_field_provenance
    (influencer_id, field_name, verification_status, source_url, collected_at, evidence_text)
SELECT id, 'content_evidence', 'MISSING', NULL, collected_at, NULL FROM influencers;
PRAGMA user_version = 2;
COMMIT;
"""

MIGRATE_2_TO_3 = """
BEGIN IMMEDIATE;
UPDATE influencers
SET business_email = 'Not Found'
WHERE business_email IS NULL OR lower(trim(business_email)) IN ('', 'not found');
UPDATE influencer_field_provenance
SET verification_status = 'MISSING'
WHERE field_name = 'contact_email'
  AND influencer_id IN (SELECT id FROM influencers WHERE business_email = 'Not Found');
PRAGMA user_version = 3;
COMMIT;
"""

MIGRATE_3_TO_4 = """
BEGIN IMMEDIATE;
ALTER TABLE influencers ADD COLUMN profile_description TEXT;
CREATE TABLE qualification_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
    evaluated_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('PASS', 'FAIL', 'REVIEW_REQUIRED')),
    reasons TEXT NOT NULL CHECK (json_valid(reasons)),
    rules_snapshot TEXT NOT NULL CHECK (json_valid(rules_snapshot)),
    evidence_snapshot TEXT NOT NULL CHECK (json_valid(evidence_snapshot))
);
CREATE INDEX idx_qualification_evaluations_influencer
    ON qualification_evaluations(influencer_id, evaluated_at DESC);
CREATE TABLE niche_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
    evidence_kind TEXT NOT NULL CHECK (evidence_kind IN ('PROFILE_DESCRIPTION', 'CONTENT_THEME', 'CONTENT_EVIDENCE', 'DECLARED_NICHE')),
    evidence_text TEXT NOT NULL CHECK (length(trim(evidence_text)) > 0),
    source_url TEXT,
    verification_status TEXT NOT NULL CHECK (verification_status IN ('MISSING', 'UNVERIFIED', 'VERIFIED')),
    collected_at TEXT,
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1))
);
CREATE INDEX idx_niche_evidence_current ON niche_evidence(influencer_id, is_current, verification_status);
INSERT INTO niche_evidence (influencer_id, evidence_kind, evidence_text, source_url, verification_status, collected_at)
SELECT i.id, 'CONTENT_EVIDENCE', i.content_evidence, p.source_url,
       CASE WHEN p.verification_status = 'VERIFIED' AND p.source_url IS NOT NULL THEN 'VERIFIED' ELSE 'UNVERIFIED' END,
       p.collected_at
FROM influencers i JOIN influencer_field_provenance p ON p.influencer_id=i.id
WHERE p.field_name='content_evidence' AND i.content_evidence IS NOT NULL AND trim(i.content_evidence) != '';
PRAGMA user_version = 4;
COMMIT;
"""

MIGRATE_4_TO_5 = """
BEGIN IMMEDIATE;
UPDATE niche_evidence
SET verification_status='UNVERIFIED'
WHERE verification_status='VERIFIED' AND (source_url IS NULL OR trim(source_url)='');
ALTER TABLE niche_evidence RENAME TO niche_evidence_v4;
CREATE TABLE niche_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
    evidence_kind TEXT NOT NULL CHECK (evidence_kind IN ('PROFILE_DESCRIPTION', 'CONTENT_THEME', 'CONTENT_EVIDENCE', 'DECLARED_NICHE')),
    evidence_text TEXT NOT NULL CHECK (length(trim(evidence_text)) > 0),
    source_url TEXT,
    verification_status TEXT NOT NULL CHECK (verification_status IN ('MISSING', 'UNVERIFIED', 'VERIFIED')),
    collected_at TEXT,
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    CHECK (verification_status != 'VERIFIED' OR source_url IS NOT NULL)
);
INSERT INTO niche_evidence (id,influencer_id,evidence_kind,evidence_text,source_url,verification_status,collected_at,is_current)
SELECT id,influencer_id,evidence_kind,evidence_text,source_url,verification_status,collected_at,is_current
FROM niche_evidence_v4;
DROP TABLE niche_evidence_v4;
CREATE INDEX idx_niche_evidence_current ON niche_evidence(influencer_id, is_current, verification_status);
PRAGMA user_version = 5;
COMMIT;
"""

MIGRATE_5_TO_6 = """
BEGIN IMMEDIATE;
ALTER TABLE outreach_messages ADD COLUMN model TEXT;
ALTER TABLE outreach_messages ADD COLUMN generated_at TEXT;
ALTER TABLE outreach_messages ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'PENDING_REVIEW'
    CHECK (approval_status IN ('PENDING_REVIEW', 'APPROVED'));
ALTER TABLE outreach_messages ADD COLUMN approved_at TEXT;
ALTER TABLE outreach_messages ADD COLUMN current_version INTEGER NOT NULL DEFAULT 0 CHECK (current_version >= 0);
CREATE TABLE outreach_message_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES outreach_messages(id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    subject TEXT,
    body TEXT NOT NULL CHECK (length(trim(body)) > 0),
    provider TEXT,
    model TEXT,
    generated_at TEXT,
    source_type TEXT NOT NULL CHECK (source_type IN ('GENERATED', 'MANUAL_EDIT', 'LEGACY')),
    prompt_version TEXT,
    evidence_snapshot TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(evidence_snapshot)),
    email_word_count INTEGER CHECK (email_word_count IS NULL OR email_word_count >= 0),
    dm_word_count INTEGER CHECK (dm_word_count IS NULL OR dm_word_count >= 0),
    approval_status TEXT NOT NULL DEFAULT 'PENDING_REVIEW' CHECK (approval_status IN ('PENDING_REVIEW', 'APPROVED')),
    approved_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(message_id, version_number)
);
CREATE INDEX idx_message_versions_message ON outreach_message_versions(message_id, version_number DESC);
INSERT INTO outreach_message_versions
    (message_id,version_number,subject,body,provider,model,generated_at,source_type,evidence_snapshot,
     email_word_count,dm_word_count,approval_status,created_at)
SELECT id,1,subject,body,provider,NULL,NULL,'LEGACY','{}',NULL,NULL,'PENDING_REVIEW',created_at
FROM outreach_messages;
UPDATE outreach_messages SET current_version=1, approval_status='PENDING_REVIEW', approved_at=NULL
WHERE id IN (SELECT message_id FROM outreach_message_versions);
PRAGMA user_version = 6;
COMMIT;
"""

MIGRATE_6_TO_7 = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS outreach_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
    message_id INTEGER REFERENCES outreach_messages(id) ON DELETE SET NULL,
    channel TEXT NOT NULL CHECK (channel IN ('EMAIL', 'INSTAGRAM_DM')),
    status TEXT NOT NULL CHECK (status IN ('PENDING_REVIEW', 'SIMULATED_SENT', 'SENT', 'FAILED')),
    idempotency_key TEXT NOT NULL UNIQUE,
    attempted_at TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outreach_logs_influencer
    ON outreach_logs(influencer_id, created_at DESC);
CREATE TABLE IF NOT EXISTS outreach_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE RESTRICT,
    channel TEXT NOT NULL CHECK (channel IN ('EMAIL', 'INSTAGRAM_DM')),
    mode TEXT NOT NULL CHECK (mode IN ('DEMO', 'LIVE', 'MANUAL')),
    message_id INTEGER REFERENCES outreach_messages(id) ON DELETE SET NULL,
    message_version_id INTEGER REFERENCES outreach_message_versions(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'SENDING', 'SIMULATED_SENT', 'SENT', 'FAILED', 'UNKNOWN',
        'MANUALLY_SENT', 'REPLIED', 'CANCELLED'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((message_id IS NULL) = (message_version_id IS NULL)),
    UNIQUE(influencer_id, channel, mode)
);
CREATE INDEX IF NOT EXISTS idx_outreach_campaign_status ON outreach_campaigns(status, channel, mode);
CREATE TABLE IF NOT EXISTS outreach_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES outreach_campaigns(id) ON DELETE RESTRICT,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    message_id INTEGER REFERENCES outreach_messages(id) ON DELETE SET NULL,
    message_version_id INTEGER REFERENCES outreach_message_versions(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'SENDING', 'SIMULATED_SENT', 'SENT', 'FAILED', 'UNKNOWN',
        'MANUALLY_SENT', 'REPLIED', 'CANCELLED'
    )),
    idempotency_key TEXT NOT NULL UNIQUE,
    started_at TEXT,
    completed_at TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    CHECK ((message_id IS NULL) = (message_version_id IS NULL)),
    UNIQUE(campaign_id, attempt_number)
);
CREATE INDEX IF NOT EXISTS idx_outreach_attempts_campaign ON outreach_attempts(campaign_id, attempt_number DESC);
CREATE TABLE IF NOT EXISTS outreach_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER REFERENCES outreach_campaigns(id) ON DELETE RESTRICT,
    attempt_id INTEGER REFERENCES outreach_attempts(id) ON DELETE RESTRICT,
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE RESTRICT,
    message_id INTEGER REFERENCES outreach_messages(id) ON DELETE SET NULL,
    message_version_id INTEGER REFERENCES outreach_message_versions(id) ON DELETE SET NULL,
    channel TEXT NOT NULL CHECK (channel IN ('EMAIL', 'INSTAGRAM_DM')),
    mode TEXT CHECK (mode IS NULL OR mode IN ('DEMO', 'LIVE', 'MANUAL')),
    event_type TEXT NOT NULL CHECK (event_type IN (
        'LEGACY_LOG_IMPORTED', 'PENDING', 'SEND_STARTED', 'SIMULATED_SENT', 'SENT',
        'FAILED', 'UNKNOWN', 'MANUALLY_SENT', 'REPLIED', 'CANCELLED',
        'RETRY_AUTHORIZED', 'OPERATOR_NOTE'
    )),
    status TEXT NOT NULL CHECK (status IN (
        'PENDING_REVIEW', 'PENDING', 'SENDING', 'SIMULATED_SENT', 'SENT', 'FAILED',
        'UNKNOWN', 'MANUALLY_SENT', 'REPLIED', 'CANCELLED'
    )),
    occurred_at TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json)),
    event_key TEXT NOT NULL UNIQUE,
    legacy_log_id INTEGER UNIQUE,
    created_at TEXT NOT NULL,
    CHECK (legacy_log_id IS NULL OR (campaign_id IS NULL AND attempt_id IS NULL AND message_version_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_outreach_activity_influencer ON outreach_activity(influencer_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_outreach_activity_status ON outreach_activity(status, occurred_at DESC);
CREATE TRIGGER IF NOT EXISTS outreach_activity_no_update
BEFORE UPDATE ON outreach_activity BEGIN
    SELECT RAISE(ABORT, 'outreach activity is append-only');
END;
CREATE TRIGGER IF NOT EXISTS outreach_activity_no_delete
BEFORE DELETE ON outreach_activity BEGIN
    SELECT RAISE(ABORT, 'outreach activity is append-only');
END;
INSERT INTO outreach_activity
    (influencer_id,message_id,message_version_id,channel,event_type,status,occurred_at,
     details_json,event_key,legacy_log_id,created_at)
SELECT influencer_id,message_id,NULL,channel,'LEGACY_LOG_IMPORTED',status,
       COALESCE(attempted_at,created_at),json_object('source','outreach_logs'),
       'legacy-outreach-log-' || id,id,created_at
FROM outreach_logs AS logs
WHERE NOT EXISTS (
    SELECT 1 FROM outreach_activity AS activity WHERE activity.legacy_log_id=logs.id
);
PRAGMA user_version = 7;
COMMIT;
"""


class DatabaseError(RuntimeError):
    """Raised when a database operation cannot be completed safely."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_profile_url(profile_url: str) -> str:
    """Validate a public HTTP(S) URL with a syntactically valid host and port."""
    value = profile_url
    if not value or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("URL must not be empty or contain whitespace/control characters.")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL contains a malformed host or port.") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not hostname:
        raise ValueError("URL must be an absolute HTTP or HTTPS URL with a hostname.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL must not contain embedded credentials.")
    _normalize_hostname(hostname)
    if port == 0:
        raise ValueError("URL port must be between 1 and 65535.")
    return value


def _normalize_hostname(hostname: str) -> str:
    """Validate a DNS name or IP literal and return its canonical host form."""
    if not hostname or any(char.isspace() or ord(char) < 33 for char in hostname):
        raise ValueError("URL must contain a valid hostname.")
    try:
        return ipaddress.ip_address(hostname).compressed.lower()
    except ValueError:
        pass
    if re.fullmatch(r"\d+(?:\.\d+){3}", hostname):
        raise ValueError("URL contains a malformed IPv4 address.")
    try:
        ascii_host = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("URL contains a malformed internationalized hostname.") from exc
    if len(ascii_host) > 253 or "." not in ascii_host:
        raise ValueError("URL must contain a fully qualified hostname.")
    labels = ascii_host.split(".")
    label_pattern = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    if any(not label_pattern.fullmatch(label) for label in labels):
        raise ValueError("URL contains a malformed domain name.")
    if len(labels[-1]) < 2 or not re.fullmatch(r"(?:[a-z]{2,}|xn--[a-z0-9-]+)", labels[-1]):
        raise ValueError("URL contains a malformed top-level domain.")
    return ascii_host


def normalize_business_email(value: str | None) -> str:
    """Validate a plain business email and canonicalize all missing markers."""
    clean = (value or "").strip()
    if not clean or clean.lower() == "not found":
        return "Not Found"
    if clean.count("@") != 1 or any(char.isspace() for char in clean):
        raise ValueError("business_email must be a plain email address or Not Found.")
    local, domain = clean.rsplit("@", 1)
    local_pattern = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
    if len(local) > 64 or not local_pattern.fullmatch(local):
        raise ValueError("business_email has an invalid local part.")
    try:
        ascii_domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("business_email has an invalid domain.") from exc
    try:
        _normalize_hostname(ascii_domain)
    except ValueError as exc:
        raise ValueError("business_email has an invalid domain.") from exc
    return f"{local}@{ascii_domain}"


def initialize_database(database_path: str | Path) -> None:
    """Initialize or safely migrate the SQLite schema to the current version."""
    path = Path(database_path)
    try:
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        with connect(path) as connection:
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > SCHEMA_VERSION:
                raise DatabaseError(
                    f"Database schema version {current_version} is newer than supported version {SCHEMA_VERSION}."
                )
            if 0 < current_version < SCHEMA_VERSION:
                tables = {str(row[0]) for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                influencer_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(influencers)")}
                message_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(outreach_messages)")}
                niche_ddl = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='niche_evidence'"
                ).fetchone()
                already_v6 = (
                    "outreach_message_versions" in tables
                    and {"model", "generated_at", "approval_status", "approved_at", "current_version"} <= message_columns
                    and "profile_description" in influencer_columns
                    and niche_ddl is not None
                    and "length(trim(source_url))" in str(niche_ddl[0])
                )
                already_v7 = (
                    already_v6
                    and {"outreach_campaigns", "outreach_attempts", "outreach_activity"} <= tables
                    and connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                        "AND name IN ('outreach_activity_no_update','outreach_activity_no_delete')"
                    ).fetchone()[0] == 2
                )
                if already_v7:
                    # Recover safely if a backup has a stale schema version marker.
                    connection.execute("PRAGMA user_version = 7")
                    current_version = 7
                elif already_v6:
                    # Recover safely if a backup/fixture has a stale version marker.
                    connection.execute("UPDATE influencers SET business_email='Not Found' WHERE business_email IS NULL OR lower(trim(business_email)) IN ('', 'not found')")
                    connection.execute("UPDATE influencer_field_provenance SET verification_status='MISSING' WHERE field_name='contact_email' AND influencer_id IN (SELECT id FROM influencers WHERE business_email='Not Found')")
                    connection.execute("PRAGMA user_version = 6")
                    current_version = 6
            if current_version == 0:
                existing_tables = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if existing_tables:
                    raise DatabaseError(
                        "Database has tables but no schema version; refusing an unsafe automatic migration."
                    )
                connection.executescript(SCHEMA)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            else:
                if current_version == 1:
                    connection.executescript(MIGRATE_1_TO_2)
                    current_version = 2
                if current_version == 2:
                    connection.executescript(MIGRATE_2_TO_3)
                    current_version = 3
                if current_version == 3:
                        columns = {row[1] for row in connection.execute("PRAGMA table_info(influencers)")}
                        has_v4_tables = connection.execute(
                            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('niche_evidence','qualification_evaluations')"
                        ).fetchone()[0] == 2
                        if "profile_description" in columns and has_v4_tables:
                            # A downgraded version marker (for example, after restoring an
                            # already migrated backup) must not re-run additive DDL.
                            connection.execute("UPDATE influencers SET business_email='Not Found' WHERE business_email IS NULL OR lower(trim(business_email)) IN ('', 'not found')")
                            connection.execute("UPDATE influencer_field_provenance SET verification_status='MISSING' WHERE field_name='contact_email' AND influencer_id IN (SELECT id FROM influencers WHERE business_email='Not Found')")
                            connection.execute("PRAGMA user_version = 4")
                        else:
                            connection.executescript(MIGRATE_3_TO_4)
                        current_version = 4
                if current_version == 4:
                    connection.executescript(MIGRATE_4_TO_5)
                    current_version = 5
                if current_version == 5:
                    tables = {str(row[0]) for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )}
                    if "outreach_messages" not in tables:
                        # Some older compatible schemas had no draft table yet.
                        connection.execute("""CREATE TABLE outreach_messages (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
                            channel TEXT NOT NULL CHECK (channel IN ('EMAIL', 'INSTAGRAM_DM')),
                            subject TEXT,
                            body TEXT NOT NULL CHECK (length(trim(body)) > 0),
                            provider TEXT,
                            generation_status TEXT NOT NULL DEFAULT 'DRAFT'
                                CHECK (generation_status IN ('DRAFT', 'GENERATED', 'FAILED')),
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            UNIQUE(influencer_id, channel)
                        )""")
                        tables.add("outreach_messages")
                    message_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(outreach_messages)")}
                    has_version_store = "outreach_message_versions" in tables
                    if {"model", "generated_at", "approval_status", "approved_at", "current_version"} <= message_columns and has_version_store:
                        connection.execute("PRAGMA user_version = 6")
                        current_version = 6
                    else:
                        connection.executescript(MIGRATE_5_TO_6)
                        current_version = 6
                if current_version == 6:
                    connection.executescript(MIGRATE_6_TO_7)
                    current_version = 7
            connection.executescript(SCHEMA)
    except DatabaseError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseError(f"Could not initialize SQLite database at {path}: {exc}") from exc


@contextmanager
def connect(database_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Yield a configured SQLite connection and always close it."""
    path = str(database_path)
    try:
        connection = sqlite3.connect(path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
        connection.commit()
    except sqlite3.Error:
        if "connection" in locals():
            connection.rollback()
        raise
    finally:
        if "connection" in locals():
            connection.close()


def add_influencer(
    database_path: str | Path,
    *,
    name: str,
    platform: str,
    profile_url: str,
    follower_count: int | None = None,
    engagement_rate: float | None = None,
    niche: str | None = None,
    content_themes: list[str] | None = None,
    business_email: str | None = None,
    website: str | None = None,
    source_url: str | None = None,
    collected_at: str | None = None,
    content_evidence: str | None = None,
    profile_description: str | None = None,
    niche_evidence: list[dict[str, str | None]] | None = None,
    field_provenance: dict[str, dict[str, str | None]] | None = None,
) -> int:
    """Insert one creator record and return its ID; duplicate profiles raise.

    This method accepts actual input data only; it does not generate profile data.
    """
    clean_name = name.strip()
    clean_platform = platform.strip().upper()
    clean_url = normalize_profile_url(profile_url, clean_platform)
    if not clean_name:
        raise ValueError("name must not be empty.")
    if not clean_platform:
        raise ValueError("platform must not be empty.")
    if follower_count is not None and follower_count < 0:
        raise ValueError("follower_count must be zero or greater.")
    if engagement_rate is not None and not 0 <= engagement_rate <= 100:
        raise ValueError("engagement_rate must be between 0 and 100.")

    now = utc_now()
    clean_source = validate_profile_url(source_url) if source_url else None
    clean_website = validate_profile_url(website) if website else None
    collected = collected_at or now
    provenance = field_provenance or {}
    clean_email = normalize_business_email(business_email)
    field_values = {
        "follower_count": follower_count is not None,
        "engagement_rate": engagement_rate is not None,
        "contact_email": clean_email.lower() != "not found",
        "content_evidence": bool(content_evidence and content_evidence.strip()),
    }
    try:
        with connect(database_path) as connection:
            cursor = connection.execute(
                """INSERT INTO influencers (
                    name, platform, profile_url, follower_count, engagement_rate,
                    niche, content_themes, business_email, website, source_url, collected_at,
                    content_evidence, profile_description,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    clean_name,
                    clean_platform,
                    clean_url,
                    follower_count,
                    engagement_rate,
                    niche.strip() if niche and niche.strip() else None,
                    json.dumps(content_themes or [], ensure_ascii=False),
                    clean_email,
                    clean_website,
                    clean_source,
                    collected,
                    content_evidence.strip() if content_evidence and content_evidence.strip() else None,
                    profile_description.strip() if profile_description and profile_description.strip() else None,
                    now,
                    now,
                ),
            )
            influencer_id = int(cursor.lastrowid)
            for field_name, has_value in field_values.items():
                supplied = provenance.get(field_name, {})
                status = (supplied.get("verification_status") or ("UNVERIFIED" if has_value else "MISSING")).upper()
                if status not in {"MISSING", "UNVERIFIED", "VERIFIED"}:
                    raise ValueError(f"Invalid verification status for {field_name}.")
                field_source = supplied.get("source_url")
                if field_source:
                    field_source = validate_profile_url(field_source)
                if status == "VERIFIED" and (not has_value or not field_source):
                    raise ValueError(f"Verified {field_name} requires a value and source URL.")
                if not has_value and status != "MISSING":
                    raise ValueError(f"Missing {field_name} must use MISSING verification status.")
                connection.execute(
                    """INSERT INTO influencer_field_provenance
                       (influencer_id, field_name, verification_status, source_url, collected_at, evidence_text)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        influencer_id,
                        field_name,
                        status,
                        field_source,
                        collected,
                        supplied.get("evidence_text"),
                    ),
                )
            evidence_rows = list(niche_evidence or [])
            if profile_description and profile_description.strip() and not any(
                item.get("evidence_kind") == "PROFILE_DESCRIPTION" for item in evidence_rows
            ):
                evidence_rows.append({"evidence_kind": "PROFILE_DESCRIPTION", "evidence_text": profile_description.strip(),
                                      "source_url": None, "verification_status": "UNVERIFIED"})
            if content_evidence and content_evidence.strip() and not any(
                item.get("evidence_kind") == "CONTENT_EVIDENCE" for item in evidence_rows
            ):
                supplied = provenance.get("content_evidence", {})
                status = (supplied.get("verification_status") or "UNVERIFIED").upper()
                evidence_rows.append({
                    "evidence_kind": "CONTENT_EVIDENCE", "evidence_text": content_evidence.strip(),
                    "source_url": supplied.get("source_url"), "verification_status": status,
                })
            for item in evidence_rows:
                kind = str(item.get("evidence_kind", ""))
                text = str(item.get("evidence_text", "")).strip()
                status = str(item.get("verification_status", "UNVERIFIED")).upper()
                source = item.get("source_url")
                if kind not in {"PROFILE_DESCRIPTION", "CONTENT_THEME", "CONTENT_EVIDENCE", "DECLARED_NICHE"}:
                    raise ValueError("Invalid niche evidence kind.")
                if not text or status not in {"MISSING", "UNVERIFIED", "VERIFIED"}:
                    raise ValueError("Niche evidence requires text and a valid verification status.")
                if status == "MISSING":
                    raise ValueError("Niche evidence with text cannot use MISSING status.")
                if source:
                    source = validate_profile_url(str(source))
                if status == "VERIFIED" and not source:
                    raise ValueError("Verified niche evidence requires its field-specific source URL.")
                connection.execute(
                    """INSERT INTO niche_evidence
                       (influencer_id,evidence_kind,evidence_text,source_url,verification_status,collected_at)
                       VALUES (?,?,?,?,?,?)""",
                    (influencer_id, kind, text, source, status, collected),
                )
            return influencer_id
    except sqlite3.IntegrityError as exc:
        if "influencers.platform, influencers.profile_url" in str(exc):
            raise DuplicateInfluencerError("This platform profile is already stored.") from exc
        raise DatabaseError(f"Influencer record violates a database constraint: {exc}") from exc
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not save influencer record: {exc}") from exc


def get_influencer(database_path: str | Path, influencer_id: int) -> dict[str, object] | None:
    """Return a creator row by ID, or None when it does not exist."""
    try:
        with connect(database_path) as connection:
            row = connection.execute(
                "SELECT * FROM influencers WHERE id = ?", (influencer_id,)
            ).fetchone()
            return dict(row) if row else None
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not read influencer record: {exc}") from exc


def update_influencer(
    database_path: str | Path, influencer_id: int, *, name: str, platform: str, profile_url: str,
    follower_count: int | None = None, engagement_rate: float | None = None,
    niche: str | None = None, content_themes: list[str] | None = None,
    business_email: str | None = None, website: str | None = None,
    source_url: str | None = None, collected_at: str | None = None,
    content_evidence: str | None = None, profile_description: str | None = None,
    niche_evidence: list[dict[str, str | None]] | None = None,
    field_provenance: dict[str, dict[str, str | None]] | None = None,
    provided_fields: set[str] | frozenset[str] | None = None,
) -> None:
    """Update supplied fields, preserving omitted/blank optional CSV values.

    Passing a field in ``provided_fields`` opts into replacing that optional
    field. An explicit ``Not Found`` email is an intentional missing-email
    update. Other fields can be intentionally cleared by including their key
    in ``provided_fields`` while passing an empty/None value.
    """
    clean_name, clean_platform = name.strip(), platform.strip().upper()
    clean_url = normalize_profile_url(profile_url, clean_platform)
    clean_email = normalize_business_email(business_email)
    clean_source = validate_profile_url(source_url) if source_url else None
    clean_website = validate_profile_url(website) if website else None
    if not clean_name or not clean_platform:
        raise ValueError("name and platform must not be empty.")
    if follower_count is not None and (not isinstance(follower_count, int) or follower_count < 0):
        raise ValueError("follower_count must be a non-negative integer.")
    if engagement_rate is not None and not 0 <= engagement_rate <= 100:
        raise ValueError("engagement_rate must be between 0 and 100.")
    collected = collected_at or utc_now()
    provenance = field_provenance or {}
    now = utc_now()
    try:
        with connect(database_path) as connection:
            current = connection.execute("SELECT * FROM influencers WHERE id=?", (influencer_id,)).fetchone()
            if not current:
                raise ValueError(f"Influencer {influencer_id} does not exist.")
            fields = set(provided_fields) if provided_fields is not None else {
                "follower_count", "engagement_rate", "business_email", "website", "niche",
                "content_themes", "content_evidence", "profile_description",
            }
            allowed_fields = {
                "follower_count", "engagement_rate", "business_email", "website", "niche",
                "content_themes", "content_evidence", "profile_description",
            }
            if fields - allowed_fields:
                raise ValueError(f"Unknown optional profile fields: {', '.join(sorted(fields - allowed_fields))}.")
            old_themes = json.loads(str(current["content_themes"] or "[]"))
            merged = {
                "follower_count": follower_count if "follower_count" in fields else current["follower_count"],
                "engagement_rate": engagement_rate if "engagement_rate" in fields else current["engagement_rate"],
                "niche": (niche.strip() if niche and niche.strip() else None) if "niche" in fields else current["niche"],
                "content_themes": list(content_themes or []) if "content_themes" in fields else old_themes,
                "business_email": clean_email if "business_email" in fields else current["business_email"],
                "website": clean_website if "website" in fields else current["website"],
                "content_evidence": (content_evidence.strip() if content_evidence and content_evidence.strip() else None)
                    if "content_evidence" in fields else current["content_evidence"],
                "profile_description": (profile_description.strip() if profile_description and profile_description.strip() else None)
                    if "profile_description" in fields else current["profile_description"],
            }
            connection.execute("""UPDATE influencers SET name=?,platform=?,profile_url=?,follower_count=?,
                engagement_rate=?,niche=?,content_themes=?,business_email=?,website=?,source_url=?,collected_at=?,
                content_evidence=?,profile_description=?,updated_at=? WHERE id=?""", (
                clean_name, clean_platform, clean_url, merged["follower_count"], merged["engagement_rate"],
                merged["niche"], json.dumps(merged["content_themes"], ensure_ascii=False), merged["business_email"], merged["website"],
                clean_source, collected,
                merged["content_evidence"], merged["profile_description"],
                now, influencer_id,
            ))
            values = {
                "follower_count": merged["follower_count"] is not None,
                "engagement_rate": merged["engagement_rate"] is not None,
                "contact_email": merged["business_email"] != "Not Found",
                "content_evidence": bool(merged["content_evidence"]),
            }
            provenance_field_for = {
                "follower_count": "follower_count", "engagement_rate": "engagement_rate",
                "contact_email": "business_email", "content_evidence": "content_evidence",
            }
            for field_name, has_value in values.items():
                if provenance_field_for[field_name] not in fields:
                    continue
                supplied = provenance.get(field_name, {})
                status = (supplied.get("verification_status") or ("UNVERIFIED" if has_value else "MISSING")).upper()
                source = supplied.get("source_url")
                if source:
                    source = validate_profile_url(source)
                if status not in {"MISSING", "UNVERIFIED", "VERIFIED"} or (has_value and status == "MISSING") or (not has_value and status != "MISSING"):
                    raise ValueError(f"Contradictory verification status for {field_name}.")
                if status == "VERIFIED" and (not has_value or not source):
                    raise ValueError(f"Verified {field_name} requires a value and field-specific source URL.")
                connection.execute("""INSERT INTO influencer_field_provenance
                    (influencer_id,field_name,verification_status,source_url,collected_at,evidence_text)
                    VALUES (?,?,?,?,?,?) ON CONFLICT(influencer_id,field_name) DO UPDATE SET
                    verification_status=excluded.verification_status,source_url=excluded.source_url,
                    collected_at=excluded.collected_at,evidence_text=excluded.evidence_text""", (
                    influencer_id, field_name, status, source, collected, supplied.get("evidence_text"),
                ))
            evidence_field_for = {
                "PROFILE_DESCRIPTION": "profile_description", "CONTENT_THEME": "content_themes",
                "CONTENT_EVIDENCE": "content_evidence", "DECLARED_NICHE": "niche",
            }
            evidence_rows = list(niche_evidence or [])
            if "profile_description" in fields and merged["profile_description"] and not any(x.get("evidence_kind") == "PROFILE_DESCRIPTION" for x in evidence_rows):
                evidence_rows.append({"evidence_kind":"PROFILE_DESCRIPTION","evidence_text":profile_description.strip(),
                                      "source_url":None,"verification_status":"UNVERIFIED"})
            if "content_evidence" in fields and merged["content_evidence"] and not any(x.get("evidence_kind") == "CONTENT_EVIDENCE" for x in evidence_rows):
                source = provenance.get("content_evidence", {}).get("source_url")
                status = provenance.get("content_evidence", {}).get("verification_status", "UNVERIFIED")
                evidence_rows.append({"evidence_kind":"CONTENT_EVIDENCE","evidence_text":merged["content_evidence"],
                                      "source_url":source,"verification_status":status})
            replace_kinds = {kind for kind, field_name in evidence_field_for.items() if field_name in fields}
            for kind in replace_kinds:
                connection.execute("UPDATE niche_evidence SET is_current=0 WHERE influencer_id=? AND evidence_kind=? AND is_current=1",
                                   (influencer_id, kind))
            for item in evidence_rows:
                kind, text = str(item.get("evidence_kind", "")), str(item.get("evidence_text", "")).strip()
                if kind not in replace_kinds:
                    continue
                status, source = str(item.get("verification_status", "UNVERIFIED")).upper(), item.get("source_url")
                if kind not in {"PROFILE_DESCRIPTION","CONTENT_THEME","CONTENT_EVIDENCE","DECLARED_NICHE"} or not text:
                    raise ValueError("Invalid niche evidence kind or empty evidence text.")
                if status not in {"MISSING","UNVERIFIED","VERIFIED"} or status == "MISSING":
                    raise ValueError("Invalid niche evidence verification status.")
                if source:
                    source = validate_profile_url(str(source))
                if status == "VERIFIED" and not source:
                    raise ValueError("Verified niche evidence requires its field-specific source URL.")
                connection.execute("""INSERT INTO niche_evidence
                    (influencer_id,evidence_kind,evidence_text,source_url,verification_status,collected_at)
                    VALUES (?,?,?,?,?,?)""", (influencer_id,kind,text,source,status,collected))
    except ValueError:
        raise
    except sqlite3.IntegrityError as exc:
        raise DatabaseError(f"Updated profile violates a database constraint: {exc}") from exc
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not update influencer record: {exc}") from exc


def count_influencers(database_path: str | Path) -> int:
    """Return the number of stored influencer records."""
    try:
        with connect(database_path) as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM influencers").fetchone()
            return int(row["total"])
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not count influencer records: {exc}") from exc


def normalize_profile_url(profile_url: str, platform: str | None = None) -> str:
    """Validate and normalize a profile URL for stable duplicate detection."""
    value = validate_profile_url(profile_url)
    value = validate_profile_url(profile_url)
    parsed = urlsplit(value)
    hostname = _normalize_hostname(parsed.hostname or "")
    try:
        ip = ipaddress.ip_address(hostname)
        host_for_netloc = f"[{ip.compressed.lower()}]" if ip.version == 6 else ip.compressed
    except ValueError:
        host_for_netloc = hostname
    port = parsed.port
    scheme = parsed.scheme.lower()
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        host_for_netloc = f"{host_for_netloc}:{port}"

    path = parsed.path.rstrip("/")
    social_platforms = {"YOUTUBE", "INSTAGRAM", "TIKTOK", "X", "TWITTER", "TWITCH"}
    if platform and platform.strip().upper() in social_platforms and path:
        segments = path.split("/")
        if len(segments) > 1:
            first = segments[1]
            if platform.strip().upper() == "YOUTUBE":
                if first.startswith("@"):
                    segments[1] = first.lower()
            elif platform.strip().upper() in {"TIKTOK"}:
                if first.startswith("@"):
                    segments[1] = first.lower()
            else:
                segments[1] = first.lower()
            path = "/".join(segments)
    normalized = f"{scheme}://{host_for_netloc}{path}"
    return normalized or f"{scheme}://{host_for_netloc}/"


class DuplicateInfluencerError(DatabaseError):
    """Raised when a platform/profile identity is already stored."""


def get_field_provenance(database_path: str | Path, influencer_id: int) -> dict[str, dict[str, object]]:
    """Return the provenance records for an influencer, keyed by field name."""
    try:
        with connect(database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM influencer_field_provenance WHERE influencer_id = ?",
                (influencer_id,),
            ).fetchall()
            return {str(row["field_name"]): dict(row) for row in rows}
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not read field provenance: {exc}") from exc
