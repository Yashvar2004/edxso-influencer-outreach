"""Deterministic qualification based exclusively on verified, sourced evidence."""

from __future__ import annotations

from outreach.qualification.models import (
    EmailOutreachReadiness, NicheMatch, QualificationDecision, QualificationEvidence,
    QualificationResult,
)
from outreach.qualification.rules import QualificationRules, categories_in_theme, matches_for_sentence, sentences


def evaluate(profile: dict[str, object], provenance: dict[str, dict[str, object]],
             evidence: list[dict[str, object]], rules: QualificationRules) -> QualificationResult:
    reasons: list[str] = []
    failed = False
    for field, value, label, predicate, description in (
        ("follower_count", profile.get("follower_count"), "Follower count",
         lambda n: rules.min_followers <= n <= rules.max_followers,
         f"must be between {rules.min_followers:,} and {rules.max_followers:,}"),
        ("engagement_rate", profile.get("engagement_rate"), "Engagement rate",
         lambda n: n >= rules.min_engagement_rate,
         f"must be at least {rules.min_engagement_rate:g}%"),
    ):
        record = provenance.get(field, {})
        status = record.get("verification_status", "MISSING")
        if value is None or status == "MISSING":
            reasons.append(f"{label} is missing.")
        elif status != "VERIFIED" or not record.get("source_url"):
            reasons.append(f"{label} is present but not verified with a field-specific source.")
        elif not predicate(float(value)):
            reasons.append(f"{label} {value} {description}.")
            failed = True
        else:
            reasons.append(f"{label} meets the verified threshold.")

    snapshots = tuple(QualificationEvidence(
        id=int(item["id"]), kind=str(item["evidence_kind"]), text=str(item["evidence_text"]),
        source_url=str(item["source_url"]) if item.get("source_url") else None,
        verification_status=str(item["verification_status"]),
        collected_at=str(item["collected_at"]) if item.get("collected_at") else None,
    ) for item in evidence if int(item.get("is_current", 1)) == 1)
    matches: list[NicheMatch] = []
    verified_themes = [item.text for item in snapshots
                       if item.kind == "CONTENT_THEME" and item.verification_status == "VERIFIED"
                       and item.source_url]
    theme_categories = {theme: set(categories_in_theme(theme)) for theme in verified_themes}
    for item in snapshots:
        if item.kind not in {"PROFILE_DESCRIPTION", "CONTENT_EVIDENCE"}:
            continue
        if item.verification_status != "VERIFIED" or not item.source_url:
            continue
        for excerpt in sentences(item.text):
            for niche, terms in matches_for_sentence(excerpt).items():
                corroboration = tuple(theme for theme, categories in theme_categories.items() if niche in categories)
                matches.append(NicheMatch(niche, terms, excerpt, item.source_url, item.id, corroboration))
    if matches:
        niches = ", ".join(sorted({match.niche for match in matches}))
        reasons.append(f"Niche relevance supported by verified content evidence: {niches}.")
    else:
        reasons.append("No sufficiently contextual, verified, source-backed Technology, AI, or Programming evidence was found.")

    if failed:
        outcome = QualificationDecision.FAIL
    elif any("missing" in reason.lower() or "not verified" in reason.lower() for reason in reasons):
        outcome = QualificationDecision.REVIEW_REQUIRED
    elif matches:
        outcome = QualificationDecision.PASS
    else:
        outcome = QualificationDecision.REVIEW_REQUIRED
    return QualificationResult(outcome, tuple(reasons), tuple(matches), snapshots, rules.snapshot())


def email_readiness(profile: dict[str, object], provenance: dict[str, dict[str, object]]) -> EmailOutreachReadiness:
    from outreach.database import normalize_business_email
    email = normalize_business_email(str(profile.get("business_email") or "Not Found"))
    available = email != "Not Found"
    record = provenance.get("contact_email", {})
    ready = available and record.get("verification_status") == "VERIFIED" and bool(record.get("source_url"))
    if not available:
        reason = "No public business email is available."
    elif not ready:
        reason = "Business email exists but lacks verified, field-specific provenance."
    else:
        reason = "Business email is verified and ready for manual email review."
    return EmailOutreachReadiness(available, ready, reason)
