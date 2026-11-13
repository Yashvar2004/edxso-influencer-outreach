"""Construct configured personalization providers."""

from outreach.config import Settings
from outreach.personalization.groq_provider import GroqProvider
from outreach.personalization.models import LLMProvider, ProviderError


def create_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider != "groq":
        raise ProviderError(
            f"LLM provider '{settings.llm_provider}' is not installed. Groq is the configured initial provider."
        )
    if not settings.llm_api_key:
        raise ProviderError("Groq API key is not configured. Set GROQ_API_KEY in the environment.")
    if not settings.llm_model:
        raise ProviderError("LLM model is not configured. Set LLM_MODEL in the environment.")
    return GroqProvider(api_key=settings.llm_api_key, model=settings.llm_model)
