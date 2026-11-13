"""Qualification thresholds and deterministic niche phrase rules."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class QualificationRules:
    min_followers: int = 5_000
    max_followers: int = 100_000
    min_engagement_rate: float = 1.0

    def __post_init__(self) -> None:
        if self.min_followers < 0:
            raise ValueError("MIN_FOLLOWERS must be zero or greater.")
        if self.max_followers < self.min_followers:
            raise ValueError("MAX_FOLLOWERS must be greater than or equal to MIN_FOLLOWERS.")
        if not 0 <= self.min_engagement_rate <= 100:
            raise ValueError("MIN_ENGAGEMENT_RATE must be between 0 and 100.")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "QualificationRules":
        env = os.environ if environ is None else environ
        try:
            return cls(
                min_followers=int(env.get("MIN_FOLLOWERS", "5000")),
                max_followers=int(env.get("MAX_FOLLOWERS", "100000")),
                min_engagement_rate=float(env.get("MIN_ENGAGEMENT_RATE", "1.0")),
            )
        except ValueError as exc:
            if str(exc).startswith(("MIN_FOLLOWERS", "MAX_FOLLOWERS", "MIN_ENGAGEMENT_RATE")):
                raise
            raise ValueError("Qualification threshold environment variables have invalid numeric values.") from exc

    def snapshot(self) -> dict[str, int | float]:
        return {
            "min_followers": self.min_followers,
            "max_followers": self.max_followers,
            "min_engagement_rate": self.min_engagement_rate,
        }


# A niche match requires a contextual phrase, not a bare keyword such as "AI".
# The engine searches exact evidence text and saves the exact matching sentence.
NICHE_PHRASES: dict[str, tuple[str, ...]] = {
    "Technology": (
        "technology reviews",
        "consumer technology",
        "technology tutorials",
        "tech product reviews",
        "hardware reviews",
        "software reviews",
        "technology industry",
        "technology news",
        "developer tools",
    ),
    "Artificial Intelligence": (
        "artificial intelligence",
        "generative ai",
        "machine learning",
        "deep learning",
        "large language model",
        "computer vision",
        "natural language processing",
        "neural networks",
        "ai engineering",
        "ai research",
    ),
    "Programming": (
        "programming tutorials",
        "programming education",
        "software development",
        "software engineering",
        "coding tutorials",
        "code walkthroughs",
        "python programming",
        "javascript development",
        "web development",
        "open source development",
    ),
}

MIN_CONTEXT_WORDS = 8
MEANINGFUL_ACTIVITY = re.compile(
    r"\b(?:teach(?:es|ing)?|tutorials?|review(?:s|ing)?|build(?:s|ing)?|develop(?:s|ing)?|"
    r"analy[sz](?:e|es|ing)|research(?:es|ing)?|explain(?:s|ing)?|publish(?:es|ing)?|"
    r"cover(?:s|ing)?|demonstrat(?:e|es|ing)|compar(?:e|es|ing)|writ(?:e|es|ing)|"
    r"creat(?:e|es|ing)|course|walkthroughs?|guides?|projects?|engineering)\b",
    re.IGNORECASE,
)


def sentences(text: str) -> tuple[str, ...]:
    """Return non-empty sentence excerpts without rewriting their source text."""
    chunks = re.split(r"(?<=[.!?\n])\s+", text)
    return tuple(chunk.strip() for chunk in chunks if chunk.strip())


def matches_for_sentence(sentence: str) -> dict[str, tuple[str, ...]]:
    """Find only explicit configured multiword phrases in a sentence."""
    if len(re.findall(r"\b[\w+#.-]+\b", sentence)) < MIN_CONTEXT_WORDS or not MEANINGFUL_ACTIVITY.search(sentence):
        return {}
    folded = sentence.casefold()
    matches: dict[str, tuple[str, ...]] = {}
    for niche, phrases in NICHE_PHRASES.items():
        found = tuple(phrase for phrase in phrases if _contains_phrase(folded, phrase))
        if found:
            matches[niche] = found
    return matches


def categories_in_theme(theme: str) -> dict[str, tuple[str, ...]]:
    """Recognize taxonomy phrases in a verified theme for corroboration only."""
    folded = theme.casefold()
    return {
        niche: tuple(phrase for phrase in phrases if _contains_phrase(folded, phrase))
        for niche, phrases in NICHE_PHRASES.items()
        if any(_contains_phrase(folded, phrase) for phrase in phrases)
    }


def _contains_phrase(text: str, phrase: str) -> bool:
    escaped = re.escape(phrase.casefold()).replace(r"\ ", r"\s+")
    return re.search(rf"(?<![\w]){escaped}(?![\w])", text, flags=re.IGNORECASE) is not None
