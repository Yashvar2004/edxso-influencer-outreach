from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from outreach.database import SCHEMA_VERSION, add_influencer, initialize_database


def _downgrade_empty_v7_to_v6_shape(database_path: Path) -> None:
    """Remove only Stage 1 objects to create a representative v6 fixture."""
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER outreach_activity_no_update")
        connection.execute("DROP TRIGGER outreach_activity_no_delete")
        connection.execute("DROP TABLE outreach_activity")
        connection.execute("DROP TABLE outreach_attempts")
        connection.execute("DROP TABLE outreach_campaigns")
        connection.execute("PRAGMA user_version=6")


def test_v6_to_v7_migration_preserves_rows_and_does_not_guess_legacy_version(tmp_path: Path) -> None:
    database_path = tmp_path / "stage1-migration.sqlite3"
    initialize_database(database_path)
    influencer_id = add_influencer(
        database_path, name="Migration fixture", platform="YOUTUBE",
        profile_url="https://youtube.com/@migration-fixture",
    )
    with sqlite3.connect(database_path) as connection:
        message = connection.execute("""INSERT INTO outreach_messages
            (influencer_id,channel,subject,body,provider,model,generated_at,generation_status,
             approval_status,approved_at,current_version,created_at,updated_at)
            VALUES (?, 'EMAIL', 'Existing subject', 'Existing v6 body', 'legacy-provider',
                    'legacy-model', 'generated-time', 'GENERATED', 'APPROVED', 'approved-time',
                    2, 'message-created', 'message-updated')""", (influencer_id,))
        message_id = int(message.lastrowid)
        connection.execute("""INSERT INTO outreach_message_versions
            (message_id,version_number,subject,body,provider,model,generated_at,source_type,
             prompt_version,evidence_snapshot,email_word_count,approval_status,approved_at,created_at)
            VALUES (?, 2, 'Existing subject', 'Existing v6 body', 'legacy-provider',
                    'legacy-model', 'generated-time', 'GENERATED', 'prompt-v1', '{}',
                    60, 'APPROVED', 'approved-time', 'version-created')""", (message_id,))
        connection.execute("""INSERT INTO outreach_logs
            (influencer_id,message_id,channel,status,idempotency_key,attempted_at,error_message,created_at)
            VALUES (?, ?, 'EMAIL', 'SENT', 'legacy-idempotency', 'attempt-time', 'legacy error', 'log-created')""",
            (influencer_id, message_id))
    _downgrade_empty_v7_to_v6_shape(database_path)

    initialize_database(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 7
        assert connection.execute("SELECT COUNT(*) FROM influencers").fetchone()[0] == 1
        assert tuple(connection.execute(
            "SELECT body,approval_status,current_version FROM outreach_messages WHERE id=?", (message_id,)
        ).fetchone()) == ("Existing v6 body", "APPROVED", 2)
        assert tuple(connection.execute(
            "SELECT body,version_number,approval_status,approved_at FROM outreach_message_versions WHERE message_id=?",
            (message_id,),
        ).fetchone()) == ("Existing v6 body", 2, "APPROVED", "approved-time")
        assert tuple(connection.execute(
            "SELECT status,idempotency_key,error_message FROM outreach_logs WHERE message_id=?", (message_id,)
        ).fetchone()) == ("SENT", "legacy-idempotency", "legacy error")
        legacy = connection.execute("""SELECT influencer_id,message_id,message_version_id,channel,
            event_type,status,occurred_at,legacy_log_id FROM outreach_activity""").fetchone()
        assert tuple(legacy) == (
            influencer_id, message_id, None, "EMAIL", "LEGACY_LOG_IMPORTED", "SENT", "attempt-time", 1,
        )
        assert connection.execute("SELECT COUNT(*) FROM outreach_campaigns").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM outreach_attempts").fetchone()[0] == 0

    # A stale v6 marker on an already migrated database must not copy history twice.
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version=6")
    initialize_database(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert connection.execute("SELECT COUNT(*) FROM outreach_activity").fetchone()[0] == 1


def test_new_database_includes_v7_outreach_tables_and_append_only_activity(tmp_path: Path) -> None:
    database_path = tmp_path / "new-v7.sqlite3"
    initialize_database(database_path)
    influencer_id = add_influencer(
        database_path, name="Schema fixture", platform="YOUTUBE",
        profile_url="https://youtube.com/@schema-fixture",
    )
    with sqlite3.connect(database_path) as connection:
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert {"outreach_campaigns", "outreach_attempts", "outreach_activity"} <= names
        connection.execute("""INSERT INTO outreach_activity
            (influencer_id,channel,event_type,status,occurred_at,event_key,created_at)
            VALUES (?, 'EMAIL', 'PENDING', 'PENDING', 't', 'event-one', 't')""", (influencer_id,))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE outreach_activity SET status='SENT' WHERE event_key='event-one'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM outreach_activity WHERE event_key='event-one'")

