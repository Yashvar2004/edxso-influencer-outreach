from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from outreach.database import add_influencer, get_influencer, initialize_database
from outreach.qualification.engine import evaluate, email_readiness
from outreach.qualification.rules import QualificationRules
from outreach.qualification.service import evaluate_influencer, evaluation_history


SOURCE = "https://example.com/creator-evidence"
CONTEXT = "I publish detailed Python programming tutorials for beginners and experienced software developers."


def create_profile(path: Path, *, followers=5000, engagement=1.0,
                   follower_status="VERIFIED", engagement_status="VERIFIED",
                   evidence=CONTEXT, evidence_status="VERIFIED", evidence_source=SOURCE,
                   email="Not Found", email_status="MISSING") -> int:
    initialize_database(path)
    return add_influencer(
        path, name="Research Creator", platform="YOUTUBE",
        profile_url="https://youtube.com/@researchcreator", follower_count=followers,
        engagement_rate=engagement, business_email=email,
        field_provenance={
            "follower_count": {"verification_status": follower_status, "source_url": SOURCE},
            "engagement_rate": {"verification_status": engagement_status, "source_url": SOURCE},
            "contact_email": {"verification_status": email_status, "source_url": SOURCE if email_status == "VERIFIED" else None},
        },
        niche_evidence=[{"evidence_kind": "CONTENT_EVIDENCE", "evidence_text": evidence,
                         "source_url": evidence_source,
                         "verification_status": evidence_status}],
    )


def profile_data(path: Path, creator_id: int):
    from outreach.database import get_field_provenance
    from outreach.qualification.service import get_evidence
    return (get_influencer(path, creator_id), get_field_provenance(path, creator_id), get_evidence(path, creator_id))


@pytest.mark.parametrize("followers,expected", [(4999, "FAIL"), (5000, "PASS"), (100000, "PASS"), (100001, "FAIL")])
def test_follower_boundaries(path_fixture, followers, expected):
    path = path_fixture
    creator = create_profile(path, followers=followers)
    result = evaluate_influencer(path, creator)
    assert result.decision.value == expected


@pytest.mark.parametrize("engagement,expected", [(0.99, "FAIL"), (1.0, "PASS")])
def test_engagement_boundary(path_fixture, engagement, expected):
    creator = create_profile(path_fixture, engagement=engagement)
    assert evaluate_influencer(path_fixture, creator).decision.value == expected


@pytest.fixture
def path_fixture(tmp_path: Path) -> Path:
    return tmp_path / "qualification.sqlite3"


@pytest.mark.parametrize("field", ["follower_count", "engagement_rate"])
def test_missing_and_unverified_metrics_require_review(path_fixture: Path, field: str) -> None:
    opts = {"follower_status" if field == "follower_count" else "engagement_status": "UNVERIFIED"}
    creator = create_profile(path_fixture, **opts)
    result = evaluate_influencer(path_fixture, creator)
    assert result.decision.value == "REVIEW_REQUIRED"
    assert any("not verified" in reason.lower() for reason in result.reasons)


def test_missing_metric_requires_review(path_fixture: Path) -> None:
    initialize_database(path_fixture)
    creator = add_influencer(path_fixture, name="Missing Metrics", platform="X", profile_url="https://x.com/missing")
    assert evaluate_influencer(path_fixture, creator).decision.value == "REVIEW_REQUIRED"


def test_username_only_niche_keywords_never_qualify(path_fixture: Path) -> None:
    initialize_database(path_fixture)
    creator = add_influencer(
        path_fixture, name="AI Programming Tutorials", platform="YOUTUBE",
        profile_url="https://youtube.com/@machine-learning-python",
        follower_count=12000, engagement_rate=2.0,
        field_provenance={
            "follower_count": {"verification_status": "VERIFIED", "source_url": SOURCE},
            "engagement_rate": {"verification_status": "VERIFIED", "source_url": SOURCE},
        },
    )
    result = evaluate_influencer(path_fixture, creator)
    assert result.decision.value == "REVIEW_REQUIRED"
    assert not result.niche_matches


def test_niche_match_requires_context_verified_source_and_preserves_exact_excerpt(path_fixture: Path) -> None:
    creator = create_profile(path_fixture)
    result = evaluate_influencer(path_fixture, creator)
    assert result.decision.value == "PASS"
    match = result.niche_matches[0]
    assert match.niche == "Programming"
    assert match.excerpt == CONTEXT
    assert match.source_url == SOURCE
    assert match.evidence_id > 0


def test_verified_themes_corroborate_but_do_not_replace_contextual_content(path_fixture: Path) -> None:
    initialize_database(path_fixture)
    creator = add_influencer(
        path_fixture, name="Theme Evidence", platform="YOUTUBE", profile_url="https://youtube.com/@themes",
        follower_count=5000, engagement_rate=1.0,
        field_provenance={
            "follower_count": {"verification_status": "VERIFIED", "source_url": SOURCE},
            "engagement_rate": {"verification_status": "VERIFIED", "source_url": SOURCE},
        },
        niche_evidence=[
            {"evidence_kind":"CONTENT_EVIDENCE","evidence_text":CONTEXT,"source_url":SOURCE,"verification_status":"VERIFIED"},
            {"evidence_kind":"CONTENT_THEME","evidence_text":"Python programming","source_url":SOURCE,"verification_status":"VERIFIED"},
        ],
    )
    result = evaluate_influencer(path_fixture, creator)
    assert result.decision.value == "PASS"
    assert result.niche_matches[0].corroborating_themes == ("Python programming",)


@pytest.mark.parametrize("evidence,status,source", [
    ("AI is cool", "VERIFIED", SOURCE),
    (CONTEXT, "UNVERIFIED", SOURCE),
    (CONTEXT, "UNVERIFIED", None),
])
def test_unsupported_or_unverified_niche_evidence_never_passes(path_fixture: Path, evidence, status, source) -> None:
    creator = create_profile(path_fixture, evidence=evidence, evidence_status=status, evidence_source=source)
    result = evaluate_influencer(path_fixture, creator)
    assert result.decision.value == "REVIEW_REQUIRED"
    assert not result.niche_matches


def test_verified_metric_failure_takes_precedence_over_missing_evidence(path_fixture: Path) -> None:
    creator = create_profile(path_fixture, followers=4000, evidence_status="UNVERIFIED")
    assert evaluate_influencer(path_fixture, creator).decision.value == "FAIL"


def test_email_readiness_is_independent_from_qualification(path_fixture: Path) -> None:
    creator = create_profile(path_fixture, email="business@example.com", email_status="VERIFIED",
                             evidence_status="UNVERIFIED")
    result = evaluate_influencer(path_fixture, creator)
    readiness = email_readiness(*profile_data(path_fixture, creator)[:2])
    assert result.decision.value == "REVIEW_REQUIRED"
    assert readiness.email_available and readiness.ready
    no_email = create_profile(tmp_path_unique(path_fixture), email="Not Found")
    assert not email_readiness(*profile_data(tmp_path_unique(path_fixture), no_email)[:2]).ready


def test_valid_but_unverified_email_is_not_ready(path_fixture: Path) -> None:
    creator = create_profile(path_fixture, email="business@example.com", email_status="UNVERIFIED")
    readiness = email_readiness(*profile_data(path_fixture, creator)[:2])
    assert readiness.email_available
    assert not readiness.ready


def test_qualification_rules_load_configurable_thresholds() -> None:
    rules = QualificationRules.from_env({
        "MIN_FOLLOWERS": "7000", "MAX_FOLLOWERS": "90000", "MIN_ENGAGEMENT_RATE": "2.25",
    })
    assert rules.min_followers == 7000
    assert rules.max_followers == 90000
    assert rules.min_engagement_rate == 2.25


def test_individual_evaluation_uses_current_environment_thresholds(path_fixture: Path, monkeypatch) -> None:
    monkeypatch.setenv("MIN_FOLLOWERS", "13000")
    monkeypatch.setenv("MAX_FOLLOWERS", "100000")
    monkeypatch.setenv("MIN_ENGAGEMENT_RATE", "1.0")
    creator = create_profile(path_fixture, followers=12000)
    result = evaluate_influencer(path_fixture, creator)
    assert result.decision.value == "FAIL"
    assert evaluation_history(path_fixture, creator)[0]["rules_snapshot"]
    saved_rules = json.loads(evaluation_history(path_fixture, creator)[0]["rules_snapshot"])
    assert saved_rules["min_followers"] == 13000


def tmp_path_unique(path: Path) -> Path:
    target = path.with_name(path.stem + "-empty" + path.suffix)
    return target


def test_reevaluation_persists_history_and_current_decision(path_fixture: Path) -> None:
    creator = create_profile(path_fixture)
    evaluate_influencer(path_fixture, creator)
    evaluate_influencer(path_fixture, creator, QualificationRules(max_followers=6000))
    assert len(evaluation_history(path_fixture, creator)) == 2
    assert get_influencer(path_fixture, creator)["classification_status"] == "PASS"
    assert json.loads(evaluation_history(path_fixture, creator)[0]["rules_snapshot"])["max_followers"] == 6000


def test_v3_migration_preserves_records_and_marks_old_unattributed_themes_unverified(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite3"
    creator = create_profile(db)
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA user_version=3")
    initialize_database(db)
    saved = get_influencer(db, creator)
    assert saved and saved["name"] == "Research Creator"
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7


def test_actual_v3_schema_migrates_additively_and_retains_creator(tmp_path: Path) -> None:
    db = tmp_path / "actual-v3.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.executescript("""
            CREATE TABLE influencers (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, platform TEXT NOT NULL, profile_url TEXT NOT NULL,
                follower_count INTEGER, engagement_rate REAL, niche TEXT, content_themes TEXT NOT NULL DEFAULT '[]',
                business_email TEXT NOT NULL DEFAULT 'Not Found', website TEXT, audience_age TEXT,
                audience_gender TEXT, audience_geography TEXT, classification_status TEXT NOT NULL DEFAULT 'REVIEW_REQUIRED',
                classification_reasons TEXT NOT NULL DEFAULT '[]', verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
                source_url TEXT, collected_at TEXT NOT NULL, content_evidence TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, UNIQUE(platform,profile_url));
            CREATE TABLE influencer_field_provenance (
                influencer_id INTEGER NOT NULL, field_name TEXT NOT NULL, verification_status TEXT NOT NULL,
                source_url TEXT, collected_at TEXT, evidence_text TEXT, PRIMARY KEY(influencer_id,field_name));
            INSERT INTO influencers(id,name,platform,profile_url,content_evidence,collected_at,created_at,updated_at)
                VALUES(7,'Legacy preserved','YOUTUBE','https://youtube.com/@legacy','A description','2026-10-31T00:00:00+00:00','t','t');
            INSERT INTO influencer_field_provenance VALUES(7,'content_evidence','UNVERIFIED',NULL,'t','A description');
            PRAGMA user_version=3;
        """)
    initialize_database(db)
    assert get_influencer(db, 7)["name"] == "Legacy preserved"
    from outreach.qualification.service import get_evidence
    evidence = get_evidence(db, 7)
    assert len(evidence) == 1
    assert evidence[0]["verification_status"] == "UNVERIFIED"
    assert evidence[0]["source_url"] is None


def test_v5_database_constraint_rejects_verified_evidence_without_source(path_fixture: Path) -> None:
    initialize_database(path_fixture)
    creator = add_influencer(path_fixture, name="Constraint test", platform="TEST", profile_url="https://example.com/c")
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(path_fixture) as connection:
            connection.execute("""INSERT INTO niche_evidence
                (influencer_id,evidence_kind,evidence_text,source_url,verification_status)
                VALUES (?, 'CONTENT_EVIDENCE', 'A genuine text excerpt', NULL, 'VERIFIED')""", (creator,))


def test_v4_migration_preserves_evidence_and_downgrades_unverifiable_source_status(tmp_path: Path) -> None:
    db = tmp_path / "legacy-v4.sqlite3"
    creator = create_profile(db)
    from outreach.database import connect
    with connect(db) as connection:
        connection.execute("ALTER TABLE niche_evidence RENAME TO niche_evidence_new")
        connection.execute("""CREATE TABLE niche_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
            evidence_kind TEXT NOT NULL,
            evidence_text TEXT NOT NULL,
            source_url TEXT,
            verification_status TEXT NOT NULL,
            collected_at TEXT,
            is_current INTEGER NOT NULL DEFAULT 1)""")
        connection.execute("""INSERT INTO niche_evidence
            SELECT id,influencer_id,evidence_kind,evidence_text,source_url,verification_status,collected_at,is_current
            FROM niche_evidence_new""")
        connection.execute("DROP TABLE niche_evidence_new")
        connection.execute("CREATE INDEX idx_niche_evidence_current ON niche_evidence(influencer_id,is_current,verification_status)")
        connection.execute("UPDATE niche_evidence SET source_url=NULL,verification_status='VERIFIED' WHERE influencer_id=?", (creator,))
        connection.execute("PRAGMA user_version=4")
    initialize_database(db)
    from outreach.qualification.service import get_evidence
    evidence = get_evidence(db, creator)
    assert evidence and evidence[0]["evidence_text"] == CONTEXT
    assert evidence[0]["verification_status"] == "UNVERIFIED"
    assert evidence[0]["source_url"] is None
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7


def test_custom_thresholds_are_recorded_in_evaluation_history(path_fixture: Path) -> None:
    creator = create_profile(path_fixture)
    rules = QualificationRules(min_followers=6000, max_followers=9000, min_engagement_rate=2.0)
    result = evaluate_influencer(path_fixture, creator, rules)
    assert result.decision.value == "FAIL"
    saved = evaluation_history(path_fixture, creator)[0]
    snapshot = json.loads(saved["evidence_snapshot"])
    assert snapshot["niche_matches"][0]["excerpt"] == CONTEXT
    assert snapshot["niche_matches"][0]["source_url"] == SOURCE
    assert snapshot["niche_matches"][0]["verification_status"] == "VERIFIED"

