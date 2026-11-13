"""Safe, idempotent simulated email delivery and outreach tracking."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from outreach.database import DatabaseError, connect, normalize_business_email, utc_now


class OutreachNotReady(ValueError):
    """Raised when a profile or message is not eligible for outreach."""


def simulate_email_send(database_path: str | Path, influencer_id: int) -> dict[str, object]:
    """Record one simulated send for an approved, qualified, verified email draft.

    This function never opens a network connection. A demo campaign is unique per
    influencer and channel, so a second call cannot create duplicate outreach.
    """
    now = utc_now()
    request_key = str(uuid.uuid4())
    try:
        with connect(database_path) as connection:
            profile = connection.execute(
                "SELECT * FROM influencers WHERE id=?", (influencer_id,)
            ).fetchone()
            if not profile:
                raise OutreachNotReady("Influencer does not exist.")
            email = normalize_business_email(profile["business_email"])
            if email == "Not Found":
                raise OutreachNotReady("A public contact email is required for email outreach.")
            provenance = connection.execute(
                "SELECT verification_status,source_url FROM influencer_field_provenance "
                "WHERE influencer_id=? AND field_name='contact_email'", (influencer_id,)
            ).fetchone()
            if not provenance or provenance["verification_status"] != "VERIFIED" or not provenance["source_url"]:
                raise OutreachNotReady("Contact email must have verified, field-specific source evidence.")
            latest = connection.execute(
                "SELECT outcome FROM qualification_evaluations WHERE influencer_id=? ORDER BY id DESC LIMIT 1",
                (influencer_id,),
            ).fetchone()
            if profile["classification_status"] != "PASS" or not latest or latest["outcome"] != "PASS":
                raise OutreachNotReady("A current PASS qualification is required.")
            message = connection.execute(
                "SELECT * FROM outreach_messages WHERE influencer_id=? AND channel='EMAIL'", (influencer_id,)
            ).fetchone()
            if not message or message["approval_status"] != "APPROVED":
                raise OutreachNotReady("An approved email draft is required.")
            version = connection.execute(
                "SELECT * FROM outreach_message_versions WHERE message_id=? AND version_number=?",
                (message["id"], message["current_version"]),
            ).fetchone()
            if not version or version["approval_status"] != "APPROVED":
                raise OutreachNotReady("The current email draft version must be approved.")

            existing = connection.execute(
                "SELECT id,status FROM outreach_campaigns WHERE influencer_id=? AND channel='EMAIL' AND mode='DEMO'",
                (influencer_id,),
            ).fetchone()
            if existing:
                raise OutreachNotReady(
                    f"Outreach was already recorded for this creator (campaign status: {existing['status']})."
                )

            campaign_cursor = connection.execute("""INSERT INTO outreach_campaigns
                (influencer_id,channel,mode,message_id,message_version_id,status,attempt_count,created_at,updated_at)
                VALUES (?,'EMAIL','DEMO',?,?, 'SENDING',1,?,?)""",
                (influencer_id, message["id"], version["id"], now, now))
            campaign_id = int(campaign_cursor.lastrowid)
            attempt_cursor = connection.execute("""INSERT INTO outreach_attempts
                (campaign_id,attempt_number,message_id,message_version_id,status,idempotency_key,
                 started_at,completed_at,created_at)
                VALUES (?,1,?,?,'SIMULATED_SENT',?,?,?,?)""",
                (campaign_id, message["id"], version["id"], request_key, now, now, now))
            attempt_id = int(attempt_cursor.lastrowid)
            connection.execute(
                "UPDATE outreach_campaigns SET status='SIMULATED_SENT',updated_at=? WHERE id=?",
                (now, campaign_id),
            )
            connection.execute("""INSERT INTO outreach_activity
                (campaign_id,attempt_id,influencer_id,message_id,message_version_id,channel,mode,
                 event_type,status,occurred_at,details_json,event_key,created_at)
                VALUES (?,?,?,?,?,'EMAIL','DEMO','SIMULATED_SENT','SIMULATED_SENT',?,?,?,?)""",
                (campaign_id, attempt_id, influencer_id, message["id"], version["id"], now,
                 json.dumps({"recipient": email, "subject": message["subject"],
                             "message_version": int(message["current_version"]),
                             "delivery": "simulated; no email sent"}, ensure_ascii=False),
                 f"demo-send-{request_key}", now))
            connection.execute("""INSERT INTO outreach_logs
                (influencer_id,message_id,channel,status,idempotency_key,attempted_at,created_at)
                VALUES (?,?,'EMAIL','SIMULATED_SENT',?,?,?)""",
                (influencer_id, message["id"], f"demo-send-{request_key}", now, now))
            return {"campaign_id": campaign_id, "attempt_id": attempt_id, "email": email,
                    "status": "SIMULATED_SENT", "message_id": int(message["id"]),
                    "message_version": int(message["current_version"]), "subject": message["subject"]}
    except sqlite3.IntegrityError as exc:
        raise OutreachNotReady("Outreach was already recorded or could not be safely recorded.") from exc
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not record simulated outreach: {exc}") from exc


def list_outreach_activity(database_path: str | Path) -> list[dict[str, object]]:
    """Return a readable chronological outreach tracker."""
    with connect(database_path) as connection:
        return [dict(row) for row in connection.execute("""SELECT a.id, i.name AS influencer,
            i.business_email AS email, a.channel, a.mode, a.event_type, a.status,
            a.occurred_at AS date, json_extract(a.details_json,'$.subject') AS subject,
            json_extract(a.details_json,'$.message_version') AS message_version,
            json_extract(a.details_json,'$.delivery') AS delivery
            FROM outreach_activity a JOIN influencers i ON i.id=a.influencer_id
            ORDER BY a.id DESC""").fetchall()]


def list_send_candidates(database_path: str | Path) -> list[dict[str, object]]:
    """List creators with a verified email, current PASS, and approved current email."""
    with connect(database_path) as connection:
        return [dict(row) for row in connection.execute("""SELECT i.id AS influencer_id,
            i.name, i.platform, i.business_email, m.id AS message_id, m.subject, m.body,
            m.current_version, m.approval_status
            FROM influencers i
            JOIN influencer_field_provenance p ON p.influencer_id=i.id
                AND p.field_name='contact_email' AND p.verification_status='VERIFIED'
                AND p.source_url IS NOT NULL AND trim(p.source_url)<>''
            JOIN outreach_messages m ON m.influencer_id=i.id AND m.channel='EMAIL'
                AND m.approval_status='APPROVED'
            JOIN outreach_message_versions v ON v.message_id=m.id
                AND v.version_number=m.current_version AND v.approval_status='APPROVED'
            WHERE i.business_email<>'Not Found' AND i.classification_status='PASS'
                AND EXISTS (SELECT 1 FROM qualification_evaluations q
                    WHERE q.influencer_id=i.id AND q.outcome='PASS'
                    AND q.id=(SELECT MAX(q2.id) FROM qualification_evaluations q2
                              WHERE q2.influencer_id=i.id))
                AND NOT EXISTS (SELECT 1 FROM outreach_campaigns c
                    WHERE c.influencer_id=i.id AND c.channel='EMAIL' AND c.mode='DEMO')
            ORDER BY i.name COLLATE NOCASE""").fetchall()]
