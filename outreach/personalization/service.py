"""Versioned draft persistence and human approval; contains no sender code."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from outreach.database import DatabaseError, connect, utc_now
from outreach.personalization.generator import (
    MAX_EXCERPT_WORDS, MIN_EXCERPT_WORDS, compose_messages, count_words,
    generate_pair, prepare_verified_context,
)
from outreach.personalization.models import GenerationError, GroundedMessagePair, LLMProvider


class DraftExistsError(GenerationError):
    """An existing draft requires an explicit regeneration action."""


class MessageApprovalError(GenerationError):
    """A message version cannot be approved in its current state."""


def _assert_current_pass(connection: sqlite3.Connection, influencer_id: int) -> None:
    profile = connection.execute(
        "SELECT classification_status FROM influencers WHERE id=?", (influencer_id,)
    ).fetchone()
    latest = connection.execute(
        "SELECT outcome FROM qualification_evaluations WHERE influencer_id=? ORDER BY id DESC LIMIT 1",
        (influencer_id,),
    ).fetchone()
    if not profile or profile["classification_status"] != "PASS" or not latest or latest["outcome"] != "PASS":
        raise MessageApprovalError("Message generation and approval require a current PASS qualification.")


def _message_evidence(pair: GroundedMessagePair, channel: str) -> str:
    is_email = channel == "EMAIL"
    citation = {
        "evidence_id": pair.email_evidence_id if is_email else pair.dm_evidence_id,
        "excerpt": pair.email_excerpt if is_email else pair.dm_excerpt,
        "source_url": pair.email_source_url if is_email else pair.dm_source_url,
        "verification_status": "VERIFIED",
        "niche": pair.email_niche if is_email else pair.dm_niche,
    }
    return json.dumps({"citations": [citation]}, ensure_ascii=False)


def _preflight(database_path: str | Path, influencer_id: int, *, regenerate: bool) -> None:
    with connect(database_path) as connection:
        _assert_current_pass(connection, influencer_id)
        existing = connection.execute(
            "SELECT COUNT(*) FROM outreach_messages WHERE influencer_id=? AND channel IN ('EMAIL','INSTAGRAM_DM')",
            (influencer_id,),
        ).fetchone()[0]
        if existing and not regenerate:
            raise DraftExistsError("Drafts already exist. Choose explicit regeneration to create new versions.")


def _store_pair(database_path: str | Path, influencer_id: int, pair: GroundedMessagePair,
                *, regenerate: bool) -> dict[str, int]:
    now = utc_now()
    outputs = {
        "EMAIL": (pair.email_subject, pair.email_body, pair.email_word_count),
        "INSTAGRAM_DM": (None, pair.instagram_dm, pair.dm_word_count),
    }
    versions: dict[str, int] = {}
    try:
        with connect(database_path) as connection:
            _assert_current_pass(connection, influencer_id)
            for channel, (subject, body, word_count) in outputs.items():
                current = connection.execute(
                    "SELECT * FROM outreach_messages WHERE influencer_id=? AND channel=?",
                    (influencer_id, channel),
                ).fetchone()
                if current:
                    if not regenerate:
                        raise DraftExistsError("Drafts already exist. Choose explicit regeneration to create new versions.")
                    version = int(current["current_version"]) + 1
                    connection.execute("""UPDATE outreach_messages SET subject=?,body=?,provider=?,model=?,
                        generated_at=?,generation_status='GENERATED',approval_status='PENDING_REVIEW',
                        approved_at=NULL,current_version=?,updated_at=? WHERE id=?""", (
                        subject, body, pair.provider, pair.model, now, version, now, current["id"],
                    ))
                    message_id = int(current["id"])
                else:
                    version = 1
                    cursor = connection.execute("""INSERT INTO outreach_messages
                        (influencer_id,channel,subject,body,provider,model,generated_at,generation_status,
                         approval_status,approved_at,current_version,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?,'GENERATED','PENDING_REVIEW',NULL,?,?,?)""", (
                        influencer_id, channel, subject, body, pair.provider, pair.model, now, version, now, now,
                    ))
                    message_id = int(cursor.lastrowid)
                connection.execute("""INSERT INTO outreach_message_versions
                    (message_id,version_number,subject,body,provider,model,generated_at,source_type,
                     prompt_version,evidence_snapshot,email_word_count,dm_word_count,approval_status,created_at)
                    VALUES (?,?,?,?,?,?,?,'GENERATED',?,?,?,?,'PENDING_REVIEW',?)""", (
                    message_id, version, subject, body, pair.provider, pair.model, now,
                    pair.prompt_version, _message_evidence(pair, channel),
                    word_count if channel == "EMAIL" else None,
                    word_count if channel == "INSTAGRAM_DM" else None, now,
                ))
                versions[channel] = version
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not save generated message versions: {exc}") from exc
    return versions


def generate_and_save(database_path: str | Path, influencer_id: int, provider: LLMProvider,
                      *, regenerate: bool = False) -> dict[str, int]:
    """Generate both channels and atomically append new pending-review versions."""
    _preflight(database_path, influencer_id, regenerate=regenerate)
    pair = generate_pair(database_path, influencer_id, provider)
    return _store_pair(database_path, influencer_id, pair, regenerate=regenerate)


def _validate_current_body(
    database_path: str | Path,
    influencer_id: int,
    channel: str,
    subject: str | None,
    body: str,
) -> tuple[int, int | None, int | None, str]:
    """Fail closed unless the complete body matches a template and verified quote.

    Natural-language claim detection is not reliable enough to certify arbitrary
    edits. Only the neutral fixed template and an exact short quote from current
    verified evidence in the latest PASS evaluation are accepted.
    """
    count = count_words(body)
    if channel == "EMAIL":
        if not 60 <= count <= 90:
            raise GenerationError("Email body must contain 60-90 words.")
        if subject != "Collaboration inquiry":
            raise GenerationError("The email subject must remain the neutral approved subject.")
    elif channel == "INSTAGRAM_DM":
        if not 15 <= count <= 30:
            raise GenerationError("Instagram DM must contain 15-30 words.")
        if subject is not None:
            raise GenerationError("Instagram DM drafts cannot have a subject.")
    else:
        raise GenerationError("Unsupported message channel.")

    evidence_refs, _, _ = prepare_verified_context(database_path, influencer_id)
    import re

    for ref in evidence_refs:
        for niche in ref.niches:
            tokens = list(re.finditer(r"\S+", ref.text))
            for start in range(len(tokens)):
                for end in range(start + MIN_EXCERPT_WORDS, min(start + MAX_EXCERPT_WORDS, len(tokens)) + 1):
                    excerpt = ref.text[tokens[start].start():tokens[end - 1].end()]
                    if not MIN_EXCERPT_WORDS <= count_words(excerpt) <= MAX_EXCERPT_WORDS:
                        continue
                    email_subject, email_body, dm_body = compose_messages(excerpt, excerpt, niche)
                    expected_body = email_body if channel == "EMAIL" else dm_body
                    expected_subject = email_subject if channel == "EMAIL" else None
                    if body == expected_body and subject == expected_subject:
                        snapshot = json.dumps({"citations": [{
                            "evidence_id": ref.evidence_id,
                            "excerpt": excerpt,
                            "source_url": ref.source_url,
                            "verification_status": "VERIFIED",
                            "niche": niche,
                        }]}, ensure_ascii=False)
                        return count, count if channel == "EMAIL" else None, count if channel == "INSTAGRAM_DM" else None, snapshot
    raise GenerationError(
        "Message must use the approved neutral template and an exact 3-8-word excerpt "
        "from current verified, source-backed evidence. Free-form claims are rejected."
    )


def edit_current_message(database_path: str | Path, message_id: int, *, subject: str | None, body: str) -> int:
    """Save an edited current draft as an immutable new pending-review version."""
    now = utc_now()
    try:
        with connect(database_path) as connection:
            current = connection.execute("SELECT * FROM outreach_messages WHERE id=?", (message_id,)).fetchone()
            if not current:
                raise MessageApprovalError("Message draft does not exist.")
            _assert_current_pass(connection, int(current["influencer_id"]))
            count, email_count, dm_count, snapshot = _validate_current_body(
                database_path, int(current["influencer_id"]), str(current["channel"]), subject, body,
            )
            version = int(current["current_version"]) + 1
            connection.execute("""UPDATE outreach_messages SET subject=?,body=?,provider=NULL,model=NULL,
                generated_at=NULL,generation_status='DRAFT',approval_status='PENDING_REVIEW',approved_at=NULL,
                current_version=?,updated_at=? WHERE id=?""", (subject, body, version, now, message_id))
            connection.execute("""INSERT INTO outreach_message_versions
                (message_id,version_number,subject,body,source_type,prompt_version,evidence_snapshot,
                 email_word_count,dm_word_count,approval_status,created_at)
                VALUES (?,?,?,?,'MANUAL_EDIT',?,?,?,?,'PENDING_REVIEW',?)""", (
                message_id, version, subject, body, None, snapshot, email_count, dm_count, now,
            ))
            return version
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not save edited message: {exc}") from exc


def approve_current_message(database_path: str | Path, message_id: int, *, expected_version: int) -> None:
    """Approve only the exact current version after rechecking qualification and citations."""
    now = utc_now()
    try:
        with connect(database_path) as connection:
            current = connection.execute("SELECT * FROM outreach_messages WHERE id=?", (message_id,)).fetchone()
            if not current:
                raise MessageApprovalError("Message draft does not exist.")
            _assert_current_pass(connection, int(current["influencer_id"]))
            if int(current["current_version"]) != expected_version:
                raise MessageApprovalError("This is no longer the current draft version. Reload before approval.")
            version_row = connection.execute("""SELECT * FROM outreach_message_versions
                WHERE message_id=? AND version_number=?""", (message_id, expected_version)).fetchone()
            if not version_row or version_row["approval_status"] != "PENDING_REVIEW":
                raise MessageApprovalError("Only a pending-review current version can be approved.")
            _, _, _, validated_snapshot = _validate_current_body(
                database_path, int(current["influencer_id"]), str(current["channel"]),
                current["subject"], str(current["body"]),
            )
            if validated_snapshot != str(version_row["evidence_snapshot"]):
                raise MessageApprovalError("Current message evidence mapping does not match its saved version.")
            connection.execute("UPDATE outreach_message_versions SET approval_status='APPROVED',approved_at=? WHERE id=?",
                               (now, version_row["id"]))
            connection.execute("UPDATE outreach_messages SET approval_status='APPROVED',approved_at=?,updated_at=? WHERE id=?",
                               (now, now, message_id))
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not approve message draft: {exc}") from exc


def list_current_messages(database_path: str | Path, influencer_id: int) -> list[dict[str, object]]:
    with connect(database_path) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM outreach_messages WHERE influencer_id=? ORDER BY channel", (influencer_id,)
        ).fetchall()]


def message_history(database_path: str | Path, message_id: int) -> list[dict[str, object]]:
    with connect(database_path) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM outreach_message_versions WHERE message_id=? ORDER BY version_number DESC", (message_id,)
        ).fetchall()]
