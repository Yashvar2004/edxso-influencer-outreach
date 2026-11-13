"""Qualification-gated, source-grounded message composition and validation."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from outreach.database import DatabaseError, connect
from outreach.personalization.models import (
    EvidenceRef, GenerationError, GroundedMessagePair, InsufficientEvidenceError,
    LLMProvider, ProviderError, ProviderRequest, QualificationGateError,
)
from outreach.personalization.prompts import PROMPT_VERSION, RESPONSE_SCHEMA, SYSTEM_PROMPT, build_evidence_payload


# Straight and curly apostrophes stay inside contractions; quotation marks and
# dashes separate words. Python reads this UTF-8 source as Unicode.
WORD_PATTERN = re.compile(r"\b[\w]+(?:['’][\w]+)*\b", re.UNICODE)
MIN_EXCERPT_WORDS = 3
MAX_EXCERPT_WORDS = 8


def count_words(text: str) -> int:
    return len(WORD_PATTERN.findall(text))


def prepare_verified_context(database_path: str | Path, influencer_id: int
                             ) -> tuple[list[EvidenceRef], list[str], list[dict[str, object]]]:
    """Collect only current verified source-backed evidence cited in latest PASS."""
    try:
        with connect(database_path) as connection:
            profile = connection.execute(
                "SELECT classification_status FROM influencers WHERE id=?", (influencer_id,)
            ).fetchone()
            if not profile:
                raise QualificationGateError("Influencer record does not exist.")
            if profile["classification_status"] != "PASS":
                raise QualificationGateError("Personalized drafts can only be generated for current PASS profiles.")
            evaluation = connection.execute(
                "SELECT outcome,evidence_snapshot FROM qualification_evaluations WHERE influencer_id=? ORDER BY id DESC LIMIT 1",
                (influencer_id,),
            ).fetchone()
            if not evaluation or evaluation["outcome"] != "PASS":
                raise QualificationGateError("The latest qualification evaluation is not PASS.")
            evidence_rows = connection.execute(
                "SELECT * FROM niche_evidence WHERE influencer_id=? AND is_current=1 AND verification_status='VERIFIED' AND source_url IS NOT NULL AND trim(source_url)!=''",
                (influencer_id,),
            ).fetchall()
            theme_rows = connection.execute(
                "SELECT id,evidence_text,source_url FROM niche_evidence WHERE influencer_id=? AND is_current=1 AND evidence_kind='CONTENT_THEME' AND verification_status='VERIFIED' AND source_url IS NOT NULL AND trim(source_url)!='' ORDER BY id",
                (influencer_id,),
            ).fetchall()
    except sqlite3.Error as exc:  # type: ignore[name-defined]
        raise DatabaseError(f"Could not prepare verified generation evidence: {exc}") from exc

    try:
        snapshot = json.loads(str(evaluation["evidence_snapshot"]))
        matches = snapshot.get("niche_matches", [])
    except (ValueError, TypeError, AttributeError) as exc:
        raise InsufficientEvidenceError("The latest qualification has no readable evidence snapshot.") from exc
    by_id = {int(row["id"]): row for row in evidence_rows}
    refs_by_id: dict[int, EvidenceRef] = {}
    niches: set[str] = set()
    for match in matches:
        try:
            evidence_id = int(match["evidence_id"])
            row = by_id[evidence_id]
            excerpt = str(match["excerpt"])
            source = str(match["source_url"])
            niche = str(match["niche"])
        except (KeyError, TypeError, ValueError):
            continue
        text = str(row["evidence_text"])
        if (row["verification_status"] != "VERIFIED" or row["source_url"] != source
                or not excerpt or excerpt not in text or not source):
            continue
        previous = refs_by_id.get(evidence_id)
        ref_niches = tuple(sorted(set((previous.niches if previous else ()) + (niche,))))
        refs_by_id[evidence_id] = EvidenceRef(
            evidence_id, str(row["evidence_kind"]), excerpt, source, "VERIFIED", ref_niches,
        )
        niches.add(niche)
    if not refs_by_id:
        raise InsufficientEvidenceError(
            "No current verified, source-backed content excerpt from the latest PASS evaluation is available."
        )
    # Keep one copy per evidence ID while preserving the evaluation's order.
    themes = [{"evidence_id": int(row["id"]), "excerpt": str(row["evidence_text"]),
               "source_url": str(row["source_url"]), "verification_status": "VERIFIED"}
              for row in theme_rows]
    return list(refs_by_id.values()), sorted(niches), themes


def validate_excerpt(raw: dict[str, Any], *, id_key: str, excerpt_key: str,
                     evidence: dict[int, EvidenceRef]) -> tuple[EvidenceRef, str]:
    evidence_id = raw.get(id_key)
    excerpt = raw.get(excerpt_key)
    if type(evidence_id) is not int or evidence_id not in evidence:
        raise GenerationError(f"{id_key} must reference a supplied verified evidence ID.")
    if not isinstance(excerpt, str) or excerpt != excerpt.strip() or "\n" in excerpt:
        raise GenerationError(f"{excerpt_key} must be a clean exact evidence excerpt.")
    ref = evidence[evidence_id]
    if excerpt not in ref.text:
        raise GenerationError(f"{excerpt_key} is not an exact substring of its cited source evidence.")
    words = count_words(excerpt)
    if not MIN_EXCERPT_WORDS <= words <= MAX_EXCERPT_WORDS:
        raise GenerationError(f"{excerpt_key} must contain {MIN_EXCERPT_WORDS}–{MAX_EXCERPT_WORDS} words.")
    return ref, excerpt


def compose_messages(email_excerpt: str, dm_excerpt: str, niche: str) -> tuple[str, str, str]:
    """Compose fixed low-claim templates around exact, validated creator excerpts."""
    subject = "Collaboration inquiry"
    email = (
        "Hello,\n\n"
        f"Your work on “{email_excerpt}” caught our attention. We’d like to explore a possible collaboration related to {niche}. "
        "If you’re open to considering it, we can share a brief with the proposed concept, deliverables, timing, and terms for your review. "
        "There is no obligation to proceed, and we welcome your questions before making any decisions. "
        "Would you be interested in receiving more details?\n\nBest,\nOutreach Team"
    )
    dm = (
        f"Your work on “{dm_excerpt}” caught our attention. We’d like to share a collaboration idea related to {niche}. "
        "Would you be open to hearing more?"
    )
    return subject, email, dm


def generate_pair(database_path: str | Path, influencer_id: int, provider: LLMProvider) -> GroundedMessagePair:
    evidence_refs, niches, supporting_themes = prepare_verified_context(database_path, influencer_id)
    refs_by_id = {ref.evidence_id: ref for ref in evidence_refs}
    payload = build_evidence_payload([
        {"evidence_id": ref.evidence_id, "kind": ref.kind, "excerpt": ref.text,
         "source_url": ref.source_url, "verification_status": ref.verification_status,
         "niche_labels": list(ref.niches)}
        for ref in evidence_refs
    ], niches, supporting_themes)
    try:
        response = provider.generate(ProviderRequest(SYSTEM_PROMPT, payload, RESPONSE_SCHEMA))
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(f"LLM provider failed: {exc}") from exc
    try:
        output = json.loads(response.content)
    except (TypeError, ValueError) as exc:
        raise GenerationError("LLM returned malformed JSON; no draft was saved.") from exc
    if not isinstance(output, dict) or set(output) != set(RESPONSE_SCHEMA["required"]):
        raise GenerationError("LLM returned an unexpected response shape; no draft was saved.")
    email_ref, email_excerpt = validate_excerpt(
        output, id_key="email_evidence_id", excerpt_key="email_excerpt", evidence=refs_by_id,
    )
    dm_ref, dm_excerpt = validate_excerpt(
        output, id_key="dm_evidence_id", excerpt_key="dm_excerpt", evidence=refs_by_id,
    )
    email_niche = email_ref.niches[0]
    dm_niche = dm_ref.niches[0]
    subject, email_body, _ = compose_messages(email_excerpt, email_excerpt, email_niche)
    _, _, dm_body = compose_messages(dm_excerpt, dm_excerpt, dm_niche)
    email_words, dm_words = count_words(email_body), count_words(dm_body)
    if not 60 <= email_words <= 90:
        raise GenerationError(f"Composed email contains {email_words} words; required range is 60–90.")
    if not 15 <= dm_words <= 30:
        raise GenerationError(f"Composed Instagram DM contains {dm_words} words; required range is 15–30.")
    return GroundedMessagePair(
        email_subject=subject, email_body=email_body, instagram_dm=dm_body,
        email_evidence_id=email_ref.evidence_id, email_excerpt=email_excerpt, email_source_url=email_ref.source_url,
        email_niche=email_niche,
        dm_evidence_id=dm_ref.evidence_id, dm_excerpt=dm_excerpt, dm_source_url=dm_ref.source_url,
        dm_niche=dm_niche,
        email_word_count=email_words, dm_word_count=dm_words,
        provider=response.provider, model=response.model, prompt_version=PROMPT_VERSION,
    )
