"""SQLite-backed evaluation and history operations."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from outreach.database import DatabaseError, connect, get_field_provenance, get_influencer, utc_now
from outreach.qualification.engine import email_readiness, evaluate
from outreach.qualification.models import QualificationResult
from outreach.qualification.rules import QualificationRules


def get_evidence(database_path: str | Path, influencer_id: int) -> list[dict[str, object]]:
    with connect(database_path) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM niche_evidence WHERE influencer_id=? AND is_current=1 ORDER BY id", (influencer_id,)
        ).fetchall()]


def evaluate_influencer(database_path: str | Path, influencer_id: int,
                        rules: QualificationRules | None = None) -> QualificationResult:
    profile = get_influencer(database_path, influencer_id)
    if profile is None:
        raise ValueError(f"Influencer {influencer_id} does not exist.")
    provenance = get_field_provenance(database_path, influencer_id)
    evidence = get_evidence(database_path, influencer_id)
    active_rules = rules if rules is not None else QualificationRules.from_env()
    result = evaluate(profile, provenance, evidence, active_rules)
    evaluated = utc_now()
    try:
        with connect(database_path) as connection:
            connection.execute("""INSERT INTO qualification_evaluations
                (influencer_id,evaluated_at,outcome,reasons,rules_snapshot,evidence_snapshot)
                VALUES (?,?,?,?,?,?)""", (
                    influencer_id, evaluated, result.decision.value, json.dumps(result.reasons),
                    json.dumps(result.rules_snapshot),
                    json.dumps({"evidence": [item.__dict__ if hasattr(item, "__dict__") else {
                        "id": item.id, "kind": item.kind, "text": item.text,
                        "source_url": item.source_url, "verification_status": item.verification_status,
                        "collected_at": item.collected_at,
                    } for item in result.evidence_snapshot], "niche_matches": [{
                        "niche": item.niche, "matched_terms": list(item.matched_terms),
                        "excerpt": item.excerpt, "source_url": item.source_url,
                        "verification_status": "VERIFIED", "evidence_id": item.evidence_id,
                        "corroborating_themes": list(item.corroborating_themes),
                    } for item in result.niche_matches]}, ensure_ascii=False),
                ))
            connection.execute("""UPDATE influencers SET classification_status=?, classification_reasons=?,
                updated_at=? WHERE id=?""", (
                    result.decision.value, json.dumps(result.reasons, ensure_ascii=False), evaluated, influencer_id,
                ))
    except sqlite3.Error as exc:
        raise DatabaseError(f"Could not save qualification evaluation: {exc}") from exc
    return result


def evaluate_all(database_path: str | Path, rules: QualificationRules | None = None) -> int:
    active_rules = rules if rules is not None else QualificationRules.from_env()
    with connect(database_path) as connection:
        ids = [int(row[0]) for row in connection.execute("SELECT id FROM influencers ORDER BY id")]
    for influencer_id in ids:
        evaluate_influencer(database_path, influencer_id, active_rules)
    return len(ids)


def email_readiness_for(database_path: str | Path, influencer_id: int):
    profile = get_influencer(database_path, influencer_id)
    if profile is None:
        raise ValueError(f"Influencer {influencer_id} does not exist.")
    return email_readiness(profile, get_field_provenance(database_path, influencer_id))


def list_influencers(database_path: str | Path) -> list[dict[str, object]]:
    with connect(database_path) as connection:
        rows = connection.execute("SELECT * FROM influencers ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]


def evaluation_history(database_path: str | Path, influencer_id: int) -> list[dict[str, object]]:
    with connect(database_path) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM qualification_evaluations WHERE influencer_id=? ORDER BY id DESC", (influencer_id,)
        ).fetchall()]
