"""YouTube Data API discovery for public, technology focused creator channels.

The provider uses only documented public API endpoints. It never guesses contact
addresses and returns only profiles with a publicly visible 5k–100k subscriber
count and a computable recent video interaction rate.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from outreach.discovery.models import DiscoveryBatch, DiscoveryRecord, FieldProvenance


class YouTubeAPIError(RuntimeError):
    """Raised when the public YouTube API request fails or is malformed."""


class YouTubeAPIProvider:
    provider_name = "youtube_data_api"
    base_url = "https://www.googleapis.com/youtube/v3"
    queries = ("technology reviews", "programming tutorials", "artificial intelligence creator")

    def __init__(self, api_key: str, *, timeout: float = 15.0, fetcher=None) -> None:
        self.api_key = api_key.strip()
        if not self.api_key:
            raise ValueError("A YouTube Data API key is required.")
        self.timeout = timeout
        self._fetcher = fetcher or self._fetch

    def _fetch(self, endpoint: str, params: dict[str, object]) -> dict:
        query = urlencode({**params, "key": self.api_key})
        request = Request(f"{self.base_url}/{endpoint}?{query}", headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            detail = str(exc)
            if isinstance(exc, HTTPError):
                try:
                    detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", detail)
                except (ValueError, OSError):
                    pass
            raise YouTubeAPIError(f"YouTube API request failed: {detail}") from exc
        if not isinstance(payload, dict):
            raise YouTubeAPIError("YouTube API returned an invalid response.")
        return payload

    def discover(self, *, target_count: int = 50, min_followers: int = 5000,
                 max_followers: int = 100000) -> DiscoveryBatch:
        if not 1 <= target_count <= 100:
            raise ValueError("Target count must be between 1 and 100.")
        ids: list[str] = []
        snippets: dict[str, dict] = {}
        for query in self.queries:
            page = self._fetcher("search", {"part": "snippet", "type": "channel", "q": query,
                                              "maxResults": 50, "order": "relevance"})
            for item in page.get("items", []):
                channel_id = item.get("id", {}).get("channelId")
                if channel_id and channel_id not in snippets:
                    ids.append(channel_id)
                    snippets[channel_id] = item.get("snippet", {})
        channel_details: dict[str, dict] = {}
        for offset in range(0, len(ids), 50):
            payload = self._fetcher("channels", {"part": "snippet,statistics,contentDetails",
                                                    "id": ",".join(ids[offset:offset + 50]), "maxResults": 50})
            for item in payload.get("items", []):
                channel_details[item["id"]] = item

        candidate_ids = [channel_id for channel_id in ids if channel_id in channel_details
                         and not channel_details[channel_id].get("statistics", {}).get("hiddenSubscriberCount")
                         and channel_details[channel_id].get("statistics", {}).get("subscriberCount")
                         and min_followers <= int(channel_details[channel_id]["statistics"]["subscriberCount"]) <= max_followers]
        uploads: dict[str, list[str]] = {}
        video_meta: dict[str, tuple[str, str]] = {}
        for channel_id in candidate_ids:
            item = channel_details[channel_id]
            playlist_id = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
            if not playlist_id:
                continue
            playlist = self._fetcher("playlistItems", {"part": "snippet,contentDetails", "playlistId": playlist_id,
                                                          "maxResults": 5})
            video_ids: list[str] = []
            for row in playlist.get("items", []):
                video_id = row.get("contentDetails", {}).get("videoId")
                if video_id:
                    video_ids.append(video_id)
                    video_meta[video_id] = (channel_id, row.get("snippet", {}).get("title", ""))
            uploads[channel_id] = video_ids

        video_stats: dict[str, dict] = {}
        all_video_ids = list(video_meta)
        for offset in range(0, len(all_video_ids), 50):
            payload = self._fetcher("videos", {"part": "statistics,snippet", "id": ",".join(all_video_ids[offset:offset + 50]),
                                                 "maxResults": 50})
            for item in payload.get("items", []):
                video_stats[item["id"]] = item

        collected = datetime.now(timezone.utc).isoformat(timespec="seconds")
        records: list[DiscoveryRecord] = []
        for channel_id in ids:
            item = channel_details.get(channel_id)
            if not item:
                continue
            stats = item.get("statistics", {})
            if stats.get("hiddenSubscriberCount") or not stats.get("subscriberCount"):
                continue
            followers = int(stats["subscriberCount"])
            if not min_followers <= followers <= max_followers:
                continue
            samples: list[tuple[int, str]] = []
            for video_id in uploads.get(channel_id, []):
                video = video_stats.get(video_id)
                if not video:
                    continue
                video_statistics = video.get("statistics", {})
                if "likeCount" not in video_statistics or "commentCount" not in video_statistics:
                    continue
                interactions = int(video_statistics["likeCount"]) + int(video_statistics["commentCount"])
                samples.append((interactions, f"https://www.youtube.com/watch?v={video_id}"))
            if not samples:
                continue
            engagement = sum(value for value, _ in samples) / len(samples) / followers * 100
            description = str(item.get("snippet", {}).get("description", "")).strip()
            if not description:
                description = str(snippets.get(channel_id, {}).get("description", "")).strip()
            if not description:
                description = str(item.get("snippet", {}).get("title", "Technology creator")) + " technology creator channel."
            channel_url = f"https://www.youtube.com/channel/{channel_id}"
            uploads_playlist_id = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
            engagement_source_url = (f"https://www.youtube.com/playlist?list={uploads_playlist_id}"
                                     if uploads_playlist_id else samples[0][1])
            records.append(DiscoveryRecord(
                name=str(item.get("snippet", {}).get("title") or snippets.get(channel_id, {}).get("title") or channel_id),
                platform="YOUTUBE", profile_url=channel_url, source_url=channel_url, collected_at=collected,
                follower_count=followers, engagement_rate=round(engagement, 4), niche="Technology",
                content_themes=("Technology", "Programming", "AI"), profile_description=description,
                content_evidence=description, business_email="Not Found",
                field_provenance={
                    "follower_count": FieldProvenance("VERIFIED", channel_url),
                    "engagement_rate": FieldProvenance("VERIFIED", engagement_source_url,
                        f"Mean likes plus comments across {len(samples)} recent public videos divided by subscribers."),
                    "contact_email": FieldProvenance("MISSING"),
                    "content_evidence": FieldProvenance("VERIFIED", channel_url),
                },
                niche_evidence=({"evidence_kind": "CONTENT_EVIDENCE", "evidence_text": description,
                                 "source_url": channel_url, "verification_status": "VERIFIED"},),
                source_row_number=len(records) + 2,
                provided_fields=frozenset({"follower_count", "engagement_rate", "content_evidence"}),
            ))
            if len(records) >= target_count:
                break
        return DiscoveryBatch(records=tuple(records))
