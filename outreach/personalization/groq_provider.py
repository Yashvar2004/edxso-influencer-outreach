"""Initial adapter using the official Groq Python SDK."""

from __future__ import annotations

import json

from outreach.personalization.models import ProviderError, ProviderRequest, ProviderResponse


class GroqProvider:
    provider_name = "groq"

    def __init__(self, *, api_key: str, model: str, timeout_seconds: float = 30.0) -> None:
        if not api_key.strip():
            raise ProviderError("Groq API key is not configured. Set GROQ_API_KEY in the environment.")
        if not model.strip():
            raise ProviderError("LLM model is not configured. Set LLM_MODEL in the environment.")
        self._api_key = api_key
        self._model = model.strip()
        self._timeout_seconds = timeout_seconds

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        try:
            from groq import Groq

            client = Groq(api_key=self._api_key, timeout=self._timeout_seconds, max_retries=0)
            response = client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": (
                        request.system_prompt + "\nReturn JSON matching this schema exactly: "
                        + json.dumps(request.response_schema, ensure_ascii=False)
                    )},
                    {"role": "user", "content": request.evidence_payload},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            content = response.choices[0].message.content
            if not content:
                raise ProviderError("Groq returned an empty response.")
            return ProviderResponse(content=content, provider=self.provider_name, model=self._model)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"Groq generation failed: {exc}") from exc
