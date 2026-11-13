from __future__ import annotations

from pathlib import Path

import pytest

from outreach.database import add_influencer, initialize_database
from outreach.personalization.service import approve_current_message, generate_and_save, list_current_messages
from outreach.personalization.models import ProviderResponse
from outreach.qualification.service import evaluate_influencer
from outreach.sending import OutreachNotReady, list_outreach_activity, list_send_candidates, simulate_email_send


class FakeProvider:
    provider_name = "test"

    def generate(self, request):
        import json
        payload = json.loads(request.evidence_payload)
        ref = payload["untrusted_creator_evidence"][0]
        return ProviderResponse(json.dumps({
            "email_evidence_id": ref["evidence_id"], "email_excerpt": "Python programming tutorials for beginners",
            "dm_evidence_id": ref["evidence_id"], "dm_excerpt": "Python programming tutorials for beginners",
        }), "test", "test-model")


def ready_creator(path: Path) -> int:
    initialize_database(path)
    creator_id = add_influencer(
        path, name="Fixture Creator", platform="YOUTUBE", profile_url="https://youtube.com/@fixturecreator",
        follower_count=12000, engagement_rate=2.2, business_email="business@fixturecreator.example",
        field_provenance={
            "follower_count": {"verification_status": "VERIFIED", "source_url": "https://example.com/metrics"},
            "engagement_rate": {"verification_status": "VERIFIED", "source_url": "https://example.com/metrics"},
            "contact_email": {"verification_status": "VERIFIED", "source_url": "https://fixturecreator.example/contact"},
        },
        niche_evidence=[{"evidence_kind": "CONTENT_EVIDENCE",
                         "evidence_text": "I publish detailed Python programming tutorials for beginners and explain software design.",
                         "source_url": "https://youtube.com/@fixturecreator/videos", "verification_status": "VERIFIED"}],
    )
    assert evaluate_influencer(path, creator_id).decision.value == "PASS"
    generate_and_save(path, creator_id, FakeProvider())
    email = next(item for item in list_current_messages(path, creator_id) if item["channel"] == "EMAIL")
    approve_current_message(path, int(email["id"]), expected_version=1)
    return creator_id


def test_simulated_send_records_status_and_blocks_duplicate(tmp_path: Path) -> None:
    db = tmp_path / "sending.sqlite3"
    creator_id = ready_creator(db)
    assert len(list_send_candidates(db)) == 1
    result = simulate_email_send(db, creator_id)
    assert result["status"] == "SIMULATED_SENT"
    assert len(list_outreach_activity(db)) == 1
    assert list_outreach_activity(db)[0]["delivery"] == "simulated; no email sent"
    assert list_send_candidates(db) == []
    with pytest.raises(OutreachNotReady, match="already recorded"):
        simulate_email_send(db, creator_id)


def test_simulated_send_requires_verified_email(tmp_path: Path) -> None:
    db = tmp_path / "no-email.sqlite3"
    creator_id = ready_creator(db)
    # Revoke verified contact provenance to model a stale/unverified address.
    from outreach.database import connect
    with connect(db) as connection:
        connection.execute("UPDATE influencer_field_provenance SET verification_status='UNVERIFIED' WHERE influencer_id=? AND field_name='contact_email'", (creator_id,))
    assert list_send_candidates(db) == []
    with pytest.raises(OutreachNotReady, match="verified"):
        simulate_email_send(db, creator_id)
