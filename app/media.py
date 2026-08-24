"""Cache and orchestration for an offer's store listing and gameplay video.

Design rule: **a pageview never waits on a third-party API.** A cold offer
renders without media and schedules the lookup in the background, so the
next visitor gets the full page. `tools/prefetch_media.py` warms every live
offer across every geo you target, which means in practice paid traffic
lands on a warm page.

Cached per (offer, storefront) because store listings are per-country:
ratings, price, and description language all differ between storefronts.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from . import appmeta, store, video as video_mod
from .appmeta import AppMeta
from .config import settings
from .models import Offer
from .video import Video

log = logging.getLogger("ogads.media")

APP_TTL = 7 * 86400        # store listings drift slowly
VIDEO_TTL = 30 * 86400     # a good gameplay video stays good


@dataclass(frozen=True)
class Media:
    app: AppMeta | None = None
    video: Video | None = None
    custom_icon: str = ""
    custom_hero: str = ""

    def __bool__(self) -> bool:
        return bool(self.app or self.video or self.custom_icon or self.custom_hero)

    @property
    def icon(self) -> str:
        """Best available square artwork.

        Our own art wins, then Apple's 512px icon, then whatever OGAds
        serves -- their thumbnail is only 100px, which looks soft on a
        retina phone, so it is the last resort rather than the default.
        """
        if self.custom_icon:
            return self.custom_icon
        if self.app and self.app.icon:
            return self.app.icon
        return ""

    @property
    def hero(self) -> str:
        """Wide artwork for the top of a page. Only ever ours."""
        return self.custom_hero

    @property
    def shots(self) -> list[str]:
        return list(self.app.screenshots) if self.app else []


def cached(offer: Offer, visitor_country: str = "") -> tuple[Media, bool]:
    """Return (media, needs_refresh) without touching the network."""
    storefront = appmeta.storefront_for(offer, visitor_country)
    row = store.get_media(offer.id, storefront)
    now = int(time.time())

    if row is None:
        return Media(), True

    app = AppMeta.from_json(row["app_json"])
    vid = Video.from_json(row["video_json"])
    custom_icon = row["custom_icon"] if "custom_icon" in row.keys() else ""
    custom_hero = row["custom_hero"] if "custom_hero" in row.keys() else ""
    stale = (now - (row["app_at"] or 0) > APP_TTL)
    if not row["pinned"] and (now - (row["video_at"] or 0) > VIDEO_TTL):
        stale = True
    return Media(app=app, video=vid, custom_icon=custom_icon,
                 custom_hero=custom_hero), stale


async def refresh(offer: Offer, visitor_country: str = "") -> Media:
    """Do the lookups and store them. Safe to call concurrently."""
    storefront = appmeta.storefront_for(offer, visitor_country)
    row = store.get_media(offer.id, storefront)
    pinned = bool(row and row["pinned"])

    app = None
    async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                 headers={"Accept": "application/json"}) as client:
        # Apple only lists iOS apps. Passing off an iOS listing as an
        # Android one would be quietly wrong, so we do not look.
        if appmeta.targets_ios(offer):
            app = await appmeta.lookup(client, offer.name_short, storefront)
        store.save_app_meta(offer.id, storefront, app.to_json() if app else "")

        if pinned:
            # A human chose this one; never overwrite it automatically.
            return Media(app=app, video=Video.from_json(row["video_json"]))

        name = app.title if app else offer.name_short
        vid = await video_mod.search(client, name, settings.youtube_api_key)
        if vid is not None:
            # Confirm it is actually embeddable before we commit to it.
            confirmed = await video_mod.describe(client, vid.id)
            vid = confirmed or None
        store.save_video(offer.id, storefront, vid.id if vid else "",
                         vid.to_json() if vid else "", pinned=False)

    log.info("media refreshed offer=%s storefront=%s app=%s video=%s",
             offer.id, storefront, bool(app), bool(vid))
    return Media(app=app, video=vid)


async def pin(offer_id: str, storefront: str, url_or_id: str) -> tuple[bool, str]:
    """Attach a specific YouTube video chosen by a human.

    Verified through oEmbed first, which rejects private and removed
    videos. It cannot detect an uploader who has disabled embedding -- that
    only shows up as a "Video unavailable" box in the player -- so check the
    review page after pinning.
    """
    vid_id = video_mod.extract_id(url_or_id)
    if not vid_id:
        return False, "That does not look like a YouTube URL or video id."
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        vid = await video_mod.describe(client, vid_id, pinned=True)
    if vid is None:
        return False, ("YouTube would not return that video. It may be private, "
                       "removed, or have embedding disabled.")
    store.save_video(offer_id, storefront, vid.id, vid.to_json(), pinned=True)
    return True, f"Pinned “{vid.title}” by {vid.channel}."
