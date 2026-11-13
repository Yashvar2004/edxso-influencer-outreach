from __future__ import annotations

import pytest

from outreach.discovery.youtube_api import YouTubeAPIProvider


def test_youtube_provider_collects_real_public_fields_and_never_guesses_email() -> None:
    calls = []

    def fetch(endpoint, params):
        calls.append((endpoint, params))
        if endpoint == "search":
            return {"items": [{"id": {"channelId": "channel-1"},
                               "snippet": {"title": "Programming Studio", "description": ""}}]}
        if endpoint == "channels":
            return {"items": [{"id": "channel-1", "snippet": {
                "title": "Programming Studio",
                "description": "Detailed Python programming tutorials for beginners."},
                "statistics": {"subscriberCount": "12000", "hiddenSubscriberCount": False},
                "contentDetails": {"relatedPlaylists": {"uploads": "uploads-1"}}}]}
        if endpoint == "playlistItems":
            return {"items": [{"contentDetails": {"videoId": "video-1"},
                               "snippet": {"title": "Python tutorial"}}]}
        if endpoint == "videos":
            return {"items": [{"id": "video-1", "statistics": {"likeCount": "120", "commentCount": "12"}}]}
        raise AssertionError(endpoint)

    batch = YouTubeAPIProvider("test-key", fetcher=fetch).discover(target_count=1)
    assert len(batch.records) == 1
    creator = batch.records[0]
    assert creator.name == "Programming Studio"
    assert creator.follower_count == 12000
    assert creator.engagement_rate == pytest.approx(1.1)
    assert creator.business_email == "Not Found"
    assert creator.field_provenance["engagement_rate"].source_url == "https://www.youtube.com/playlist?list=uploads-1"
    assert creator.field_provenance["contact_email"].verification_status == "MISSING"
    assert [endpoint for endpoint, _ in calls].count("search") == 3


def test_youtube_provider_requires_key_and_bounds_target() -> None:
    with pytest.raises(ValueError, match="API key"):
        YouTubeAPIProvider(" ")
    with pytest.raises(ValueError, match="between 1 and 100"):
        YouTubeAPIProvider("test").discover(target_count=101)
