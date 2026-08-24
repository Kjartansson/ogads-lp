"""Real store metadata for the app behind an offer.

Source is Apple's public Search API: keyless, rate-limited but generous, and
**per storefront** — which matters, because OGAds offers are country
targeted and the same app has different ratings, pricing and description
language in each country's store.

Everything surfaced from here is genuine, published, attributable data. The
rating is Apple's, shown with its rating count and storefront so the reader
knows exactly what it is. Nothing on the page claims we played the game.

Android-only offers are a known gap: Google Play has no public JSON API and
scraping it is fragile and against its terms. `resolve` returns None for
them rather than passing off an iOS listing as an Android one.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, asdict, field

import httpx

from .models import Offer

log = logging.getLogger("ogads.appmeta")

SEARCH_URL = "https://itunes.apple.com/search"
_WORD = re.compile(r"[a-z0-9]+")
# Words in an OGAds name_short that carry no matching signal.
_NOISE = {"the", "a", "an", "game", "games", "app", "mobile", "free", "new",
          "play", "official", "hd", "3d", "2d", "io"}


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _NOISE}


@dataclass(frozen=True)
class AppMeta:
    track_id: int
    title: str
    developer: str
    description: str
    genre: str
    content_rating: str
    price: str
    size_mb: int
    version: str
    updated: str
    rating: float
    rating_count: int
    storefront: str
    store_url: str
    icon: str
    screenshots: list[str] = field(default_factory=list)

    @property
    def rating_display(self) -> str:
        """Always shown WITH its provenance -- a bare star count invites the
        reader to think it is ours."""
        if not self.rating or not self.rating_count:
            return ""
        return (f"{self.rating:.1f} from {self.rating_count:,} ratings "
                f"on the {self.storefront.upper()} App Store")

    @property
    def mature(self) -> bool:
        """17+ shows up constantly on these offers; readers deserve to know."""
        return bool(re.match(r"^(1[2-9]|[2-9]\d)\+", self.content_rating or ""))

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(blob: str) -> "AppMeta | None":
        if not blob:
            return None
        try:
            return AppMeta(**json.loads(blob))
        except (ValueError, TypeError):
            return None


def storefront_for(offer: Offer, visitor_country: str = "") -> str:
    """Which country's store to read.

    The visitor's own storefront when the offer serves it -- that is the
    listing they would actually see -- otherwise the offer's first target
    country, falling back to US.
    """
    countries = [c.upper() for c in offer.countries]
    vc = (visitor_country or "").upper()
    if vc and (vc in countries or (vc == "GB" and "UK" in countries)):
        return "gb" if vc == "GB" else vc.lower()
    if countries:
        first = countries[0]
        return "gb" if first == "UK" else first.lower()
    return "us"


def targets_ios(offer: Offer) -> bool:
    return bool({d.lower() for d in offer.devices} & {"iphone", "ipad", "ipod", "ios"})


def _score(candidate_title: str, wanted: str) -> float:
    """How confident are we this listing is the offer's app?

    OGAds shortens names ("Travel Town" for "Travel Town - Merge Adventure"),
    so a prefix or subset match is expected and fine. A wrong match puts the
    wrong game's screenshots and rating on a review, so the bar is a full
    containment of the wanted tokens, not a fuzzy overlap.
    """
    want, got = _tokens(wanted), _tokens(candidate_title)
    if not want or not got:
        return 0.0
    if want <= got:
        return 1.0 if len(want) >= 2 else 0.8
    overlap = len(want & got) / len(want)
    return overlap * 0.6


async def lookup(client: httpx.AsyncClient, name: str, storefront: str,
                 min_score: float = 0.8) -> AppMeta | None:
    try:
        resp = await client.get(SEARCH_URL, params={
            "term": name, "country": storefront, "entity": "software", "limit": 5})
        resp.raise_for_status()
        results = resp.json().get("results") or []
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("app lookup failed for %r/%s: %s", name, storefront, exc)
        return None

    best, best_score = None, 0.0
    for r in results:
        sc = _score(r.get("trackName", ""), name)
        if sc > best_score:
            best, best_score = r, sc
    if best is None or best_score < min_score:
        log.info("no confident app match for %r on %s (best %.2f)", name, storefront, best_score)
        return None

    try:
        size_mb = round(int(best.get("fileSizeBytes") or 0) / 1_000_000)
    except (TypeError, ValueError):
        size_mb = 0

    return AppMeta(
        track_id=int(best.get("trackId") or 0),
        title=best.get("trackName", ""),
        developer=best.get("sellerName", ""),
        description=(best.get("description") or "").strip(),
        genre=best.get("primaryGenreName", ""),
        content_rating=best.get("contentAdvisoryRating", ""),
        price=best.get("formattedPrice", ""),
        size_mb=size_mb,
        version=best.get("version", ""),
        updated=(best.get("currentVersionReleaseDate") or "")[:10],
        rating=float(best.get("averageUserRating") or 0),
        rating_count=int(best.get("userRatingCount") or 0),
        storefront=storefront,
        store_url=best.get("trackViewUrl", ""),
        icon=best.get("artworkUrl512") or best.get("artworkUrl100", ""),
        screenshots=(best.get("screenshotUrls") or [])[:6],
    )
