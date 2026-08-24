"""Gameplay video for an offer.

Videos are **embedded**, never re-hosted. An embed is what YouTube's player
is for: it is free, it keeps the uploader's view count and attribution
intact, and it does not copy someone else's footage onto our server. (This
is also why voomreel is not the tool for this job — it fetches a known URL,
which is a different problem, and self-hosting the result would be
republishing another creator's work.)

Two paths, deliberately separate:

  pinned    A human pastes a YouTube URL in the admin panel. Title and
            channel come from YouTube's public oEmbed endpoint, so full
            attribution works with NO API key. Available today.
  auto      Search YouTube for likely gameplay footage. Needs a YouTube
            Data API key. Absent one, the site simply shows no video rather
            than guessing.

Auto-discovery is a suggestion, not a verdict: a wrong video on a review is
worse than no video, so matches are filtered hard and a pinned choice always
wins over an automatic one.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, asdict

import httpx

log = logging.getLogger("ogads.video")

OEMBED_URL = "https://www.youtube.com/oembed"
SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"

_ID_PATTERNS = (
    re.compile(r"(?:youtube\.com/watch\?(?:.*&)?v=)([A-Za-z0-9_-]{11})"),
    re.compile(r"(?:youtu\.be/)([A-Za-z0-9_-]{11})"),
    re.compile(r"(?:youtube\.com/embed/)([A-Za-z0-9_-]{11})"),
    re.compile(r"(?:youtube\.com/shorts/)([A-Za-z0-9_-]{11})"),
    re.compile(r"^([A-Za-z0-9_-]{11})$"),
)

_WORD = re.compile(r"[a-z0-9]+")
# Titles that are about acquiring the game for free rather than playing it.
_BAD_TITLE = re.compile(
    r"\b(mod|modded|apk|hack|hacked|cheat|cheats|generator|unlimited|free\s+"
    r"(?:gems|coins|money|cash|spins)|crack|inject)\b", re.I)


def extract_id(url_or_id: str) -> str:
    """Pull an 11-character video id out of any common YouTube URL form."""
    text = (url_or_id or "").strip()
    for pattern in _ID_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1)
    return ""


@dataclass(frozen=True)
class Video:
    id: str
    title: str
    channel: str
    thumbnail: str
    pinned: bool = False

    @property
    def embed_url(self) -> str:
        # nocookie host, no autoplay, no related-video spray at the end.
        return f"https://www.youtube-nocookie.com/embed/{self.id}?rel=0"

    @property
    def watch_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(blob: str) -> "Video | None":
        if not blob:
            return None
        try:
            return Video(**json.loads(blob))
        except (ValueError, TypeError):
            return None


async def describe(client: httpx.AsyncClient, video_id: str, pinned: bool = False) -> Video | None:
    """Title and channel for a known video id, via keyless oEmbed.

    A failure here also tells us the video is unusable — oEmbed 404s for
    removed, private, and embedding-disabled videos, which is exactly the
    set we must not put on a page.
    """
    if not video_id:
        return None
    try:
        resp = await client.get(OEMBED_URL, params={
            "url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"})
        if resp.status_code != 200:
            log.info("video %s not embeddable (oEmbed HTTP %d)", video_id, resp.status_code)
            return None
        d = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("oEmbed failed for %s: %s", video_id, exc)
        return None
    return Video(id=video_id, title=d.get("title", ""), channel=d.get("author_name", ""),
                 thumbnail=d.get("thumbnail_url", ""), pinned=pinned)


def _relevant(title: str, app_name: str) -> bool:
    """Does this title plausibly show the game being played?"""
    if _BAD_TITLE.search(title):
        return False
    wanted = {w for w in _WORD.findall(app_name.lower()) if len(w) > 2}
    got = set(_WORD.findall(title.lower()))
    return bool(wanted) and len(wanted & got) / len(wanted) >= 0.6


async def search(client: httpx.AsyncClient, app_name: str, api_key: str) -> Video | None:
    """Best gameplay match for an app name. Requires a YouTube Data API key."""
    if not api_key or not app_name:
        return None
    try:
        resp = await client.get(SEARCH_URL, params={
            "part": "snippet", "q": f"{app_name} gameplay", "type": "video",
            "videoEmbeddable": "true", "safeSearch": "strict",
            "maxResults": "10", "key": api_key})
        if resp.status_code != 200:
            log.warning("YouTube search HTTP %d: %s", resp.status_code, resp.text[:160])
            return None
        items = resp.json().get("items") or []
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("YouTube search failed for %r: %s", app_name, exc)
        return None

    for item in items:
        snippet = item.get("snippet") or {}
        title = snippet.get("title", "")
        vid = (item.get("id") or {}).get("videoId", "")
        if not vid or not _relevant(title, app_name):
            continue
        return Video(id=vid, title=title,
                     channel=snippet.get("channelTitle", ""),
                     thumbnail=((snippet.get("thumbnails") or {}).get("high") or {}).get("url", ""),
                     pinned=False)
    log.info("no relevant gameplay video found for %r among %d results", app_name, len(items))
    return None
