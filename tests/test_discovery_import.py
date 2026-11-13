import csv
import io
import json
import sqlite3
from pathlib import Path

import pytest

from outreach.database import (
    DatabaseError,
    add_influencer,
    count_influencers,
    get_field_provenance,
    get_influencer,
    initialize_database,
)
from outreach.discovery import CSVDiscoveryProvider, DiscoveryRequest, import_batch
from outreach.discovery.template import CSV_COLUMNS, generate_csv_template


def make_csv(rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def valid_test_row(**overrides: str) -> dict[str, str]:
    row = {
        "name": "Test Creator",
        "platform": "YouTube",
        "profile_url": "https://www.youtube.com/@test_creator/",
        "follower_count": "12,000",
        "follower_count_status": "VERIFIED",
        "follower_count_source_url": "https://www.youtube.com/@test_creator/about",
        "engagement_rate": "2.5%",
        "engagement_rate_status": "UNVERIFIED",
        "engagement_rate_source_url": "https://example.invalid/metrics/test-creator",
        "niche": "Programming",
        "content_themes": "Python|AI; developer tools",
        "content_evidence": "Public videos discuss Python tutorials and model evaluation.",
        "content_evidence_status": "VERIFIED",
        "content_evidence_source_url": "https://www.youtube.com/@test_creator/videos",
        "business_email": "Not Found",
        "contact_email_status": "MISSING",
        "contact_email_source_url": "",
        "website": "",
        "source_url": "https://www.youtube.com/@test_creator/about",
        "collected_at": "2026-10-31T10:30:00+05:30",
    }
    row.update(overrides)
    return row


def test_valid_csv_import_persists_normalized_record_and_provenance(tmp_path: Path) -> None:
    db_path = tmp_path / "import.sqlite3"
    initialize_database(db_path)
    provider = CSVDiscoveryProvider()
    batch = provider.discover(DiscoveryRequest(source_data=make_csv([valid_test_row()])))

    report = import_batch(str(db_path), batch)

    assert report.imported_count == 1
    assert report.duplicate_count == 0
    assert report.rejected_count == 0
    saved = get_influencer(db_path, report.imported[0].influencer_id)
    assert saved is not None
    assert saved["name"] == "Test Creator"
    assert saved["profile_url"] == "https://www.youtube.com/@test_creator"
    assert saved["follower_count"] == 12000
    assert saved["engagement_rate"] == 2.5
    assert saved["business_email"] == "Not Found"
    assert saved["source_url"] == "https://www.youtube.com/@test_creator/about"
    assert saved["collected_at"] == "2026-10-31T05:00:00+00:00"
    assert saved["classification_status"] == "REVIEW_REQUIRED"

    provenance = get_field_provenance(db_path, report.imported[0].influencer_id)
    assert provenance["follower_count"]["verification_status"] == "VERIFIED"
    assert provenance["follower_count"]["source_url"].endswith("/about")
    assert provenance["engagement_rate"]["verification_status"] == "UNVERIFIED"
    assert provenance["contact_email"]["verification_status"] == "MISSING"
    assert provenance["content_evidence"]["verification_status"] == "VERIFIED"
    assert provenance["content_evidence"]["evidence_text"].startswith("Public videos")


def test_explicit_update_replaces_supplied_fields_and_appends_history(tmp_path: Path) -> None:
    db_path = tmp_path / "update.sqlite3"
    initialize_database(db_path)
    provider = CSVDiscoveryProvider()
    first = import_batch(str(db_path), provider.discover(
        DiscoveryRequest(source_data=make_csv([valid_test_row()]))))
    updated_row = valid_test_row(
        engagement_rate="2.5", engagement_rate_status="VERIFIED",
        engagement_rate_source_url="https://example.invalid/metrics/test-creator",
        content_evidence="I publish detailed Python programming tutorials for beginners and experienced software developers.",
    )
    report = import_batch(str(db_path), provider.discover(
        DiscoveryRequest(source_data=make_csv([updated_row]))), update_existing=True)
    assert report.updated_count == 1
    assert report.duplicate_count == 0
    assert report.updated[0].influencer_id == first.imported[0].influencer_id
    saved = get_influencer(db_path, report.updated[0].influencer_id)
    assert saved["classification_status"] == "PASS"
    from outreach.qualification.service import evaluation_history
    history = evaluation_history(db_path, report.updated[0].influencer_id)
    assert len(history) == 2
    import json
    prior_snapshot = json.loads(history[1]["evidence_snapshot"])
    assert prior_snapshot["evidence"][0]["text"].startswith("Public videos")


def test_partial_csv_update_preserves_blank_values_provenance_and_evidence(tmp_path: Path) -> None:
    from outreach.qualification.service import evaluation_history, get_evidence

    db_path = tmp_path / "partial-update.sqlite3"
    initialize_database(db_path)
    provider = CSVDiscoveryProvider()
    evidence = "I publish detailed Python programming tutorials for beginners and experienced software developers."
    description = "I publish consumer technology reviews that compare practical hardware and software for everyday buyers."
    initial = valid_test_row(
        business_email="business@example.com", contact_email_status="VERIFIED",
        contact_email_source_url="https://example.com/business-contact",
        engagement_rate="2.5", engagement_rate_status="VERIFIED",
        engagement_rate_source_url="https://example.com/metrics",
        niche="Programming", niche_status="VERIFIED", niche_source_url="https://example.com/niche",
        content_themes="Python programming", content_themes_status="VERIFIED",
        content_themes_source_url="https://example.com/themes",
        profile_description=description, profile_description_status="VERIFIED",
        profile_description_source_url="https://example.com/about",
        content_evidence=evidence, content_evidence_status="VERIFIED",
        content_evidence_source_url="https://example.com/content",
        website="https://example.com/creator",
    )
    first = import_batch(str(db_path), provider.discover(DiscoveryRequest(source_data=make_csv([initial]))))
    creator_id = first.imported[0].influencer_id
    blank_update = valid_test_row(
        name="Updated label",
        follower_count="", follower_count_status="", follower_count_source_url="",
        engagement_rate="", engagement_rate_status="", engagement_rate_source_url="",
        business_email="", contact_email_status="", contact_email_source_url="",
        website="", niche="", niche_status="", niche_source_url="",
        content_themes="", content_themes_status="", content_themes_source_url="",
        profile_description="", profile_description_status="", profile_description_source_url="",
        content_evidence="", content_evidence_status="", content_evidence_source_url="",
    )
    updated = import_batch(str(db_path), provider.discover(DiscoveryRequest(source_data=make_csv([blank_update]))),
                          update_existing=True)
    assert updated.updated_count == 1
    saved = get_influencer(db_path, creator_id)
    assert saved["name"] == "Updated label"
    assert saved["follower_count"] == 12000
    assert saved["engagement_rate"] == 2.5
    assert saved["business_email"] == "business@example.com"
    assert saved["website"] == "https://example.com/creator"
    assert saved["niche"] == "Programming"
    assert saved["profile_description"] == description
    assert saved["content_evidence"] == evidence
    provenance = get_field_provenance(db_path, creator_id)
    assert provenance["follower_count"]["source_url"].endswith("/about")
    assert provenance["engagement_rate"]["source_url"] == "https://example.com/metrics"
    assert provenance["contact_email"]["source_url"] == "https://example.com/business-contact"
    assert provenance["follower_count"]["verification_status"] == "VERIFIED"
    assert provenance["engagement_rate"]["verification_status"] == "VERIFIED"
    assert provenance["contact_email"]["verification_status"] == "VERIFIED"
    current_evidence = get_evidence(db_path, creator_id)
    assert {item["evidence_kind"] for item in current_evidence} >= {"PROFILE_DESCRIPTION", "CONTENT_EVIDENCE", "CONTENT_THEME"}
    assert any(item["source_url"] == "https://example.com/content" for item in current_evidence)
    history = evaluation_history(db_path, creator_id)
    assert len(history) == 2
    niche_sources = {item["niche"]: item["source_url"]
                     for item in json.loads(history[1]["evidence_snapshot"])["niche_matches"]}
    assert niche_sources["Technology"] == "https://example.com/about"
    assert niche_sources["Programming"] == "https://example.com/content"


def test_explicit_not_found_email_clears_email_without_clearing_other_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "email-clear.sqlite3"
    initialize_database(db_path)
    provider = CSVDiscoveryProvider()
    initial = valid_test_row(business_email="business@example.com", contact_email_status="VERIFIED",
                             contact_email_source_url="https://example.com/contact")
    first = import_batch(str(db_path), provider.discover(DiscoveryRequest(source_data=make_csv([initial]))))
    creator_id = first.imported[0].influencer_id
    update = valid_test_row(
        business_email="Not Found", contact_email_status="MISSING", contact_email_source_url="",
        follower_count="", follower_count_status="", follower_count_source_url="",
        engagement_rate="", engagement_rate_status="", engagement_rate_source_url="",
        content_evidence="", content_evidence_status="", content_evidence_source_url="",
    )
    report = import_batch(str(db_path), provider.discover(DiscoveryRequest(source_data=make_csv([update]))),
                          update_existing=True)
    assert report.updated_count == 1
    saved = get_influencer(db_path, creator_id)
    assert saved["business_email"] == "Not Found"
    assert saved["follower_count"] == 12000
    assert get_field_provenance(db_path, creator_id)["contact_email"]["verification_status"] == "MISSING"


def test_invalid_profile_url_is_rejected_without_persisting(tmp_path: Path) -> None:
    db_path = tmp_path / "invalid.sqlite3"
    initialize_database(db_path)
    row = valid_test_row(profile_url="javascript:alert(1)")
    batch = CSVDiscoveryProvider().discover(DiscoveryRequest(source_data=make_csv([row])))
    report = import_batch(str(db_path), batch)

    assert report.imported_count == 0
    assert report.rejected_count == 1
    assert "absolute HTTP or HTTPS" in report.rejected[0].reason
    assert count_influencers(db_path) == 0


@pytest.mark.parametrize(
    "column,value",
    [
        ("profile_url", "http://bad host/path"),
        ("profile_url", "http://.com"),
        ("profile_url", "https://example.com:99999/a"),
        ("profile_url", " https://example.com/profile "),
        ("source_url", "https://example.com:99999/source"),
        ("website", "http://bad host/site"),
    ],
)
def test_malformed_profile_source_and_website_urls_are_rejected(
    tmp_path: Path, column: str, value: str
) -> None:
    db_path = tmp_path / "bad-url.sqlite3"
    initialize_database(db_path)
    row = valid_test_row(**{column: value})
    batch = CSVDiscoveryProvider().discover(DiscoveryRequest(source_data=make_csv([row])))
    report = import_batch(str(db_path), batch)
    assert report.imported_count == 0
    assert report.rejected_count == 1
    assert count_influencers(db_path) == 0


@pytest.mark.parametrize(
    "email",
    ["bad..email@example.com", "foo@.com", "foo@example..com", "not-an-email"],
)
def test_malformed_emails_are_rejected_by_csv_import(tmp_path: Path, email: str) -> None:
    db_path = tmp_path / "bad-email.sqlite3"
    initialize_database(db_path)
    row = valid_test_row(
        business_email=email,
        contact_email_status="UNVERIFIED",
        contact_email_source_url="",
    )
    batch = CSVDiscoveryProvider().discover(DiscoveryRequest(source_data=make_csv([row])))
    report = import_batch(str(db_path), batch)
    assert report.imported_count == 0
    assert report.rejected_count == 1


@pytest.mark.parametrize("value", ["", "  ", "nOt FoUnD"])
def test_direct_database_insert_normalizes_missing_email(value: str, tmp_path: Path) -> None:
    db_path = tmp_path / "missing-email.sqlite3"
    initialize_database(db_path)
    influencer_id = add_influencer(
        db_path,
        name="Test Creator",
        platform="youtube",
        profile_url="https://youtube.com/@email-test",
        business_email=value,
    )
    saved = get_influencer(db_path, influencer_id)
    assert saved is not None
    assert saved["business_email"] == "Not Found"


def test_direct_database_insert_rejects_malformed_email(tmp_path: Path) -> None:
    db_path = tmp_path / "direct-bad-email.sqlite3"
    initialize_database(db_path)
    with pytest.raises(ValueError, match="invalid local part"):
        add_influencer(
            db_path,
            name="Test Creator",
            platform="youtube",
            profile_url="https://youtube.com/@direct-email-test",
            business_email="bad..email@example.com",
        )


def test_valid_business_email_is_accepted_and_domain_is_normalized(tmp_path: Path) -> None:
    db_path = tmp_path / "valid-email.sqlite3"
    initialize_database(db_path)
    row = valid_test_row(
        business_email="Business.Contact@Example.COM",
        contact_email_status="UNVERIFIED",
        contact_email_source_url="https://example.com/contact",
    )
    batch = CSVDiscoveryProvider().discover(DiscoveryRequest(source_data=make_csv([row])))
    report = import_batch(str(db_path), batch)
    assert report.imported_count == 1
    saved = get_influencer(db_path, report.imported[0].influencer_id)
    assert saved is not None
    assert saved["business_email"] == "Business.Contact@example.com"


def test_date_only_collection_timestamp_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "date-only.sqlite3"
    initialize_database(db_path)
    row = valid_test_row(collected_at="2026-10-31")
    batch = CSVDiscoveryProvider().discover(DiscoveryRequest(source_data=make_csv([row])))
    report = import_batch(str(db_path), batch)
    assert report.imported_count == 0
    assert "YYYY-MM-DDTHH:MM:SS" in report.rejected[0].reason


@pytest.mark.parametrize("value", ["1,2", "1.5", "-1", "12,34"])
def test_invalid_follower_count_formats_are_rejected(tmp_path: Path, value: str) -> None:
    db_path = tmp_path / "bad-followers.sqlite3"
    initialize_database(db_path)
    row = valid_test_row(follower_count=value)
    batch = CSVDiscoveryProvider().discover(DiscoveryRequest(source_data=make_csv([row])))
    report = import_batch(str(db_path), batch)
    assert report.imported_count == 0
    assert report.rejected_count == 1


@pytest.mark.parametrize("value", ["1,2", "-0.1", "100.01", "NaN", "Infinity"])
def test_invalid_engagement_rates_are_rejected(tmp_path: Path, value: str) -> None:
    db_path = tmp_path / "bad-engagement.sqlite3"
    initialize_database(db_path)
    row = valid_test_row(engagement_rate=value)
    batch = CSVDiscoveryProvider().discover(DiscoveryRequest(source_data=make_csv([row])))
    report = import_batch(str(db_path), batch)
    assert report.imported_count == 0
    assert report.rejected_count == 1


def test_duplicate_normalization_is_platform_aware(tmp_path: Path) -> None:
    db_path = tmp_path / "platform-duplicates.sqlite3"
    initialize_database(db_path)
    provider = CSVDiscoveryProvider()
    first = valid_test_row(
        platform="Instagram",
        profile_url="https://WWW.Instagram.com/CreatorName/",
        follower_count="",
        follower_count_status="",
        engagement_rate="",
        engagement_rate_status="",
        content_evidence="",
        content_evidence_status="",
        content_evidence_source_url="",
    )
    same_instagram = dict(first, profile_url="https://www.instagram.com/creatorname")
    different_platform = dict(first, platform="Other", profile_url="https://www.instagram.com/CreatorName")
    batch = provider.discover(
        DiscoveryRequest(source_data=make_csv([first, same_instagram, different_platform]))
    )
    report = import_batch(str(db_path), batch)
    assert report.imported_count == 2
    assert report.duplicate_count == 1


def test_generic_platform_preserves_case_sensitive_profile_paths(tmp_path: Path) -> None:
    db_path = tmp_path / "case-sensitive.sqlite3"
    initialize_database(db_path)
    provider = CSVDiscoveryProvider()
    upper = valid_test_row(platform="Generic", profile_url="https://example.com/CreatorA")
    lower = valid_test_row(platform="Generic", profile_url="https://example.com/creatora")
    batch = provider.discover(DiscoveryRequest(source_data=make_csv([upper, lower])))
    report = import_batch(str(db_path), batch)
    assert report.imported_count == 2
    assert report.duplicate_count == 0


def test_verified_field_requires_its_own_source_and_statuses_must_match_values(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "field-source.sqlite3"
    initialize_database(db_path)
    no_specific_source = valid_test_row(follower_count_source_url="")
    contradictory = valid_test_row(
        engagement_rate="",
        engagement_rate_status="VERIFIED",
        engagement_rate_source_url="https://example.invalid/rate",
    )
    batch = CSVDiscoveryProvider().discover(
        DiscoveryRequest(source_data=make_csv([no_specific_source, contradictory]))
    )
    report = import_batch(str(db_path), batch)
    assert report.imported_count == 0
    assert report.rejected_count == 2
    assert "field-specific source URL" in report.rejected[0].reason
    assert "unavailable and must use MISSING" in report.rejected[1].reason


def test_unverified_field_does_not_inherit_general_source_url(tmp_path: Path) -> None:
    db_path = tmp_path / "separate-sources.sqlite3"
    initialize_database(db_path)
    row = valid_test_row(
        follower_count_status="UNVERIFIED",
        follower_count_source_url="",
        engagement_rate="",
        engagement_rate_status="",
        content_evidence="",
        content_evidence_status="",
        content_evidence_source_url="",
    )
    batch = CSVDiscoveryProvider().discover(DiscoveryRequest(source_data=make_csv([row])))
    report = import_batch(str(db_path), batch)
    provenance = get_field_provenance(db_path, report.imported[0].influencer_id)
    assert provenance["follower_count"]["source_url"] is None
    assert provenance["follower_count"]["verification_status"] == "UNVERIFIED"


def test_header_only_csv_has_clear_rejection_message() -> None:
    batch = CSVDiscoveryProvider().discover(
        DiscoveryRequest(source_data=generate_csv_template())
    )
    assert not batch.records
    assert batch.rejected[0].reason == "CSV contains headers but no influencer records."


def test_empty_csv_is_reported_without_crashing() -> None:
    batch = CSVDiscoveryProvider().discover(DiscoveryRequest(source_data=b""))
    assert not batch.records
    assert batch.rejected[0].reason == "CSV file is empty or has no header row."


def test_strict_csv_rejects_malformed_quoting_gracefully() -> None:
    malformed = (
        b'name,platform,profile_url,source_url,collected_at\n'
        b'"unterminated,YouTube,https://youtube.com/@x,https://youtube.com,2026-10-31T10:00:00Z\n'
    )
    batch = CSVDiscoveryProvider().discover(DiscoveryRequest(source_data=malformed))
    assert not batch.records
    assert batch.rejected
    assert "Malformed CSV data" in batch.rejected[0].reason


def test_oversized_csv_field_is_reported_without_crashing() -> None:
    row = valid_test_row(content_evidence="x" * 200_000)
    batch = CSVDiscoveryProvider().discover(DiscoveryRequest(source_data=make_csv([row])))
    assert not batch.records
    assert batch.rejected
    assert "field larger than field limit" in batch.rejected[0].reason


def test_repeated_import_is_reported_as_duplicate(tmp_path: Path) -> None:
    db_path = tmp_path / "repeat.sqlite3"
    initialize_database(db_path)
    provider = CSVDiscoveryProvider()
    request = DiscoveryRequest(source_data=make_csv([valid_test_row()]))
    first = import_batch(str(db_path), provider.discover(request))
    second = import_batch(str(db_path), provider.discover(request))
    assert first.imported_count == 1
    assert second.imported_count == 0
    assert second.duplicate_count == 1
    assert count_influencers(db_path) == 1


def test_custom_environment_thresholds_apply_to_new_csv_import_and_history(tmp_path: Path, monkeypatch) -> None:
    from outreach.qualification.service import evaluation_history

    monkeypatch.setenv("MIN_FOLLOWERS", "15000")
    monkeypatch.setenv("MAX_FOLLOWERS", "50000")
    monkeypatch.setenv("MIN_ENGAGEMENT_RATE", "3.0")
    db_path = tmp_path / "custom-import-threshold.sqlite3"
    initialize_database(db_path)
    row = valid_test_row(
        follower_count="12000", follower_count_status="VERIFIED",
        engagement_rate="2.5", engagement_rate_status="VERIFIED",
        content_evidence="I publish detailed Python programming tutorials for beginners and experienced software developers.",
        content_evidence_status="VERIFIED",
    )
    report = import_batch(str(db_path), CSVDiscoveryProvider().discover(
        DiscoveryRequest(source_data=make_csv([row]))))
    creator_id = report.imported[0].influencer_id
    assert get_influencer(db_path, creator_id)["classification_status"] == "FAIL"
    snapshot = json.loads(evaluation_history(db_path, creator_id)[0]["rules_snapshot"])
    assert snapshot == {"min_followers": 15000, "max_followers": 50000, "min_engagement_rate": 3.0}


def test_custom_environment_thresholds_apply_to_update_import_and_bulk_re_evaluation(
    tmp_path: Path, monkeypatch
) -> None:
    from outreach.qualification.service import evaluate_all, evaluation_history

    monkeypatch.setenv("MIN_FOLLOWERS", "5000")
    monkeypatch.setenv("MAX_FOLLOWERS", "100000")
    monkeypatch.setenv("MIN_ENGAGEMENT_RATE", "1.0")
    db_path = tmp_path / "custom-update-threshold.sqlite3"
    initialize_database(db_path)
    provider = CSVDiscoveryProvider()
    row = valid_test_row(
        follower_count="12000", follower_count_status="VERIFIED",
        engagement_rate="2.5", engagement_rate_status="VERIFIED",
        content_evidence="I publish detailed Python programming tutorials for beginners and experienced software developers.",
        content_evidence_status="VERIFIED",
    )
    first = import_batch(str(db_path), provider.discover(DiscoveryRequest(source_data=make_csv([row]))))
    creator_id = first.imported[0].influencer_id
    assert get_influencer(db_path, creator_id)["classification_status"] == "PASS"
    monkeypatch.setenv("MIN_FOLLOWERS", "13000")
    partial = valid_test_row(follower_count="", follower_count_status="", follower_count_source_url="",
                             engagement_rate="", engagement_rate_status="", engagement_rate_source_url="",
                             content_evidence="", content_evidence_status="", content_evidence_source_url="")
    update = import_batch(str(db_path), provider.discover(DiscoveryRequest(source_data=make_csv([partial]))),
                          update_existing=True)
    assert update.updated_count == 1
    assert get_influencer(db_path, creator_id)["classification_status"] == "FAIL"
    assert json.loads(evaluation_history(db_path, creator_id)[0]["rules_snapshot"])["min_followers"] == 13000
    monkeypatch.setenv("MIN_FOLLOWERS", "11000")
    assert evaluate_all(str(db_path)) == 1
    assert get_influencer(db_path, creator_id)["classification_status"] == "PASS"
    assert json.loads(evaluation_history(db_path, creator_id)[0]["rules_snapshot"])["min_followers"] == 11000


def test_failed_provenance_write_rolls_back_whole_record(tmp_path: Path) -> None:
    db_path = tmp_path / "rollback.sqlite3"
    initialize_database(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """CREATE TRIGGER fail_provenance BEFORE INSERT ON influencer_field_provenance
               BEGIN SELECT RAISE(ABORT, 'forced test failure'); END"""
        )
    batch = CSVDiscoveryProvider().discover(
        DiscoveryRequest(source_data=make_csv([valid_test_row()]))
    )
    report = import_batch(str(db_path), batch)
    assert report.imported_count == 0
    assert report.rejected_count == 1
    assert count_influencers(db_path) == 0


def test_missing_optional_fields_are_marked_missing_and_never_pass(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.sqlite3"
    initialize_database(db_path)
    row = valid_test_row(
        follower_count="",
        follower_count_status="",
        engagement_rate="",
        engagement_rate_status="",
        business_email="",
        contact_email_status="",
        content_evidence="",
        content_evidence_status="",
        content_evidence_source_url="",
    )
    batch = CSVDiscoveryProvider().discover(DiscoveryRequest(source_data=make_csv([row])))
    report = import_batch(str(db_path), batch)

    assert report.imported_count == 1
    saved = get_influencer(db_path, report.imported[0].influencer_id)
    assert saved is not None
    assert saved["follower_count"] is None
    assert saved["engagement_rate"] is None
    assert saved["business_email"] == "Not Found"
    assert saved["content_evidence"] is None
    assert saved["classification_status"] == "REVIEW_REQUIRED"
    provenance = get_field_provenance(db_path, report.imported[0].influencer_id)
    assert {entry["verification_status"] for entry in provenance.values()} == {"MISSING"}


def test_duplicate_rows_are_reported_and_skipped(tmp_path: Path) -> None:
    db_path = tmp_path / "duplicates.sqlite3"
    initialize_database(db_path)
    first = valid_test_row()
    duplicate = valid_test_row(
        name="Another label for same test profile",
        profile_url="https://www.youtube.com/@test_creator?utm_source=directory",
    )
    batch = CSVDiscoveryProvider().discover(DiscoveryRequest(source_data=make_csv([first, duplicate])))
    report = import_batch(str(db_path), batch)

    assert report.imported_count == 1
    assert report.duplicate_count == 1
    assert report.duplicates[0].row_number == 3
    assert count_influencers(db_path) == 1


def test_required_fields_and_bad_metrics_are_reported_by_row(tmp_path: Path) -> None:
    db_path = tmp_path / "malformed.sqlite3"
    initialize_database(db_path)
    missing_name = valid_test_row(name="")
    bad_metric = valid_test_row(follower_count="many")
    provider = CSVDiscoveryProvider()
    batch = provider.discover(
        DiscoveryRequest(source_data=make_csv([missing_name, bad_metric]))
    )
    report = import_batch(str(db_path), batch)

    assert report.imported_count == 0
    assert report.rejected_count == 2
    assert report.rejected[0].row_number == 2
    assert "name is required" in report.rejected[0].reason
    assert report.rejected[1].row_number == 3
    assert "non-negative integer" in report.rejected[1].reason


def test_template_contains_headers_only() -> None:
    template = generate_csv_template().decode("utf-8")
    rows = list(csv.reader(io.StringIO(template)))
    assert rows == [list(CSV_COLUMNS)]


def test_v1_database_migrates_without_losing_records(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE influencers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                platform TEXT NOT NULL,
                profile_url TEXT NOT NULL,
                follower_count INTEGER,
                engagement_rate REAL,
                niche TEXT,
                content_themes TEXT NOT NULL DEFAULT '[]',
                business_email TEXT NOT NULL DEFAULT 'Not Found',
                website TEXT,
                audience_age TEXT,
                audience_gender TEXT,
                audience_geography TEXT,
                classification_status TEXT NOT NULL DEFAULT 'REVIEW_REQUIRED',
                classification_reasons TEXT NOT NULL DEFAULT '[]',
                verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
                source_url TEXT,
                collected_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(platform, profile_url)
            );
            INSERT INTO influencers (name, platform, profile_url, follower_count,
                engagement_rate, business_email, source_url, collected_at, created_at, updated_at)
            VALUES ('Legacy Test Creator', 'YOUTUBE', 'https://youtube.com/@legacy-test',
                9000, NULL, ' nOt FoUnD ', 'https://youtube.com/@legacy-test/about',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
            PRAGMA user_version = 1;
            """
        )

    initialize_database(db_path)

    saved = get_influencer(db_path, 1)
    assert saved is not None
    assert saved["name"] == "Legacy Test Creator"
    assert saved["follower_count"] == 9000
    assert saved["business_email"] == "Not Found"
    assert saved["content_evidence"] is None
    provenance = get_field_provenance(db_path, 1)
    assert provenance["follower_count"]["verification_status"] == "UNVERIFIED"
    assert provenance["engagement_rate"]["verification_status"] == "MISSING"
    assert provenance["contact_email"]["verification_status"] == "MISSING"
    assert provenance["content_evidence"]["verification_status"] == "MISSING"


def test_v2_database_migration_normalizes_legacy_missing_email(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-v2.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
            CREATE TABLE influencers (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, platform TEXT NOT NULL,
                profile_url TEXT NOT NULL, follower_count INTEGER, engagement_rate REAL, niche TEXT,
                content_themes TEXT NOT NULL DEFAULT '[]', business_email TEXT NOT NULL DEFAULT 'Not Found',
                website TEXT, audience_age TEXT, audience_gender TEXT, audience_geography TEXT,
                classification_status TEXT NOT NULL DEFAULT 'REVIEW_REQUIRED', classification_reasons TEXT NOT NULL DEFAULT '[]',
                verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED', source_url TEXT, collected_at TEXT NOT NULL,
                content_evidence TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(platform,profile_url));
            CREATE TABLE influencer_field_provenance (
                influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
                field_name TEXT NOT NULL CHECK(field_name IN ('follower_count','engagement_rate','contact_email','content_evidence')),
                verification_status TEXT NOT NULL CHECK(verification_status IN ('MISSING','UNVERIFIED','VERIFIED')),
                source_url TEXT, collected_at TEXT, evidence_text TEXT, PRIMARY KEY(influencer_id,field_name));
            INSERT INTO influencers(id,name,platform,profile_url,business_email,collected_at,created_at,updated_at)
                VALUES(42,'Legacy v2','YOUTUBE','https://youtube.com/@legacy',' nOt FoUnD ','t','t','t');
            INSERT INTO influencer_field_provenance VALUES(42,'contact_email','UNVERIFIED',NULL,'t',NULL);
            PRAGMA user_version=2;
        """)

    initialize_database(db_path)
    saved = get_influencer(db_path, 42)
    assert saved is not None
    assert saved["business_email"] == "Not Found"
    provenance = get_field_provenance(db_path, 42)
    assert provenance["contact_email"]["verification_status"] == "MISSING"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
