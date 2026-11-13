"""Persist validated provider records and classify row-level outcomes."""

from __future__ import annotations

from outreach.database import DatabaseError, DuplicateInfluencerError, add_influencer, connect, normalize_profile_url, update_influencer
from outreach.discovery.models import (
    DiscoveryBatch,
    ImportedRecord,
    ImportIssue,
    ImportReport,
)
from outreach.qualification.service import evaluate_influencer
from outreach.qualification.rules import QualificationRules


def import_batch(database_path: str, batch: DiscoveryBatch, *, update_existing: bool = False,
                 rules: QualificationRules | None = None) -> ImportReport:
    active_rules = rules if rules is not None else QualificationRules.from_env()
    imported: list[ImportedRecord] = []
    updated: list[ImportedRecord] = []
    duplicates: list[ImportIssue] = []
    rejected = list(batch.rejected)

    for fallback_row_number, record in enumerate(batch.records, start=2):
        row_number = record.source_row_number or fallback_row_number
        try:
            if update_existing:
                normalized_url = normalize_profile_url(record.profile_url, record.platform)
                with connect(database_path) as connection:
                    existing = connection.execute(
                        "SELECT id FROM influencers WHERE platform=? AND profile_url=?",
                        (record.platform.strip().upper(), normalized_url),
                    ).fetchone()
                if existing:
                    influencer_id = int(existing["id"])
                    update_influencer(
                        database_path, influencer_id,
                        name=record.name, platform=record.platform, profile_url=record.profile_url,
                        follower_count=record.follower_count, engagement_rate=record.engagement_rate,
                        niche=record.niche, content_themes=list(record.content_themes),
                        business_email=record.business_email, website=record.website,
                        source_url=record.source_url, collected_at=record.collected_at,
                        content_evidence=record.content_evidence, profile_description=record.profile_description,
                        niche_evidence=list(record.niche_evidence),
                        provided_fields=set(record.provided_fields),
                        field_provenance={name: {"verification_status": p.verification_status,
                                                 "source_url": p.source_url, "evidence_text": p.evidence_text}
                                          for name, p in record.field_provenance.items()},
                    )
                    evaluate_influencer(database_path, influencer_id, active_rules)
                    updated.append(ImportedRecord(row_number, influencer_id, record.profile_url))
                    continue
            influencer_id = add_influencer(
                database_path,
                name=record.name,
                platform=record.platform,
                profile_url=record.profile_url,
                follower_count=record.follower_count,
                engagement_rate=record.engagement_rate,
                niche=record.niche,
                content_themes=list(record.content_themes),
                business_email=record.business_email,
                website=record.website,
                source_url=record.source_url,
                collected_at=record.collected_at,
                content_evidence=record.content_evidence,
                profile_description=record.profile_description,
                niche_evidence=list(record.niche_evidence),
                field_provenance={
                    name: {
                        "verification_status": provenance.verification_status,
                        "source_url": provenance.source_url,
                        "evidence_text": provenance.evidence_text,
                    }
                    for name, provenance in record.field_provenance.items()
                },
            )
            imported.append(ImportedRecord(row_number, influencer_id, record.profile_url))
            evaluate_influencer(database_path, influencer_id, active_rules)
        except DuplicateInfluencerError as exc:
            duplicates.append(ImportIssue(row_number, str(exc), record.profile_url))
        except (ValueError, DatabaseError) as exc:
            rejected.append(ImportIssue(row_number, str(exc), record.profile_url))
    return ImportReport(imported=tuple(imported), updated=tuple(updated),
                        duplicates=tuple(duplicates), rejected=tuple(rejected))
