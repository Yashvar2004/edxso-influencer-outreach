"""Provider-neutral message-personalization contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    evidence_id: int
    kind: str
    text: str
    source_url: str
    verification_status: str
    niches: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    system_prompt: str
    evidence_payload: str
    response_schema: dict[str, object]


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    content: str
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class GroundedMessagePair:
    email_subject: str
    email_body: str
    instagram_dm: str
    email_evidence_id: int
    email_excerpt: str
    email_source_url: str
    email_niche: str
    dm_evidence_id: int
    dm_excerpt: str
    dm_source_url: str
    dm_niche: str
    email_word_count: int
    dm_word_count: int
    provider: str
    model: str
    prompt_version: str


class GenerationError(RuntimeError):
    """Raised when evidence or generated content cannot safely form drafts."""


class QualificationGateError(GenerationError):
    """Raised when a profile is not currently qualified for personalization."""


class InsufficientEvidenceError(GenerationError):
    """Raised when no current verified and sourced niche evidence is available."""


class LLMProvider(Protocol):
    """Provider contract; adapters return raw structured JSON for validation."""

    provider_name: str

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        """Generate one structured personalization result or raise ProviderError."""


class ProviderError(RuntimeError):
    """Raised for provider configuration, transport, or response failures."""
