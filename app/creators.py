"""Hosted link pages for creators.

A creator signs up, connects their OWN OGAds key, and picks 3-6 offers to
feature. Their traffic pays their account directly, so the platform never
holds their money and never acts as a sub-affiliate network on its own
account.

The platform's fee is taken in traffic, not cash: a fixed percentage of
VISITORS see the platform's offers instead of the creator's. Two properties
make that defensible rather than sneaky:

  deterministic  The bucket is a hash of (visitor session, creator), so one
                 person gets a consistent page rather than a list that
                 reshuffles under them on reload.
  auditable      Every visit records which side was served, and the
                 creator's own dashboard shows the real measured percentage.
                 A fee you can watch is a fee, not a trick.
"""
from __future__ import annotations

import hashlib
import logging

from .models import Offer

log = logging.getLogger("ogads.creators")

MIN_LINKS, MAX_LINKS = 3, 6
SERVED_CREATOR, SERVED_PLATFORM = "creator", "platform"


def serves_platform(session_id: str, username: str, share_pct: int) -> bool:
    """Is this visitor in the platform's slice for this creator?

    Hashed rather than random so a reload does not re-roll the dice: the
    same visitor on the same page always gets the same answer, and the
    distribution across many visitors still lands on share_pct.
    """
    share = max(0, min(100, int(share_pct or 0)))
    if share <= 0:
        return False
    if share >= 100:
        return True
    digest = hashlib.sha256(f"{session_id}:{username}".encode()).digest()
    return (int.from_bytes(digest[:4], "big") % 100) < share


def ordered_links(chosen: list[dict], available: list[Offer]) -> list[Offer]:
    """The creator's picks, in their order, minus anything no longer live.

    Offers cap constantly, so a creator's saved selection is a wish list
    checked against reality on every render -- never a promise that all six
    still exist.
    """
    by_id = {o.id: o for o in available}
    out = []
    for link in chosen:
        offer = by_id.get(link["offer_id"])
        if offer is None:
            continue
        title = (link.get("title") or "").strip()
        out.append((offer, title))
    return out


def platform_picks(available: list[Offer], limit: int = MAX_LINKS) -> list[tuple[Offer, str]]:
    """What the platform shows on its slice: simply the best-earning offers."""
    ranked = sorted(available, key=lambda o: o.ranking_score, reverse=True)
    return [(o, "") for o in ranked[:limit]]


DISCLOSURE_SIGNUP = (
    "Your OGAds key stays yours: visitors to your page see offers from your "
    "account and conversions pay you directly. In exchange for hosting, a "
    "share of your visitors — shown on your dashboard and set at {share}% "
    "for your account — are served this site's offers instead. You can see "
    "the exact measured number at any time, and you can delete your page "
    "whenever you like."
)


# ---------------------------------------------------------------- templates
# Ready-made layouts a creator can send traffic to. The point is that they
# never have to build a landing page: they pick offers, pick a look, and
# have something to put in a bio.
TEMPLATES = {
    "links": {
        "name": "Link list",
        "blurb": "The classic bio-link page: your offers stacked as big tappable "
                 "buttons. Best for TikTok and Instagram bios.",
        "template": "creator_page.html",
        "wants": (MIN_LINKS, MAX_LINKS),
    },
    "spotlight": {
        "name": "Single offer spotlight",
        "blurb": "One offer, full review treatment — gameplay video, App Store "
                 "rating, screenshots. Best when a video is about one game.",
        "template": "creator_spotlight.html",
        "wants": (1, 1),
    },
    "grid": {
        "name": "App grid",
        "blurb": "Your picks as a scannable grid of app cards with ratings. Best "
                 "for a YouTube description or a 'my top apps' video.",
        "template": "creator_grid.html",
        "wants": (3, MAX_LINKS),
    },
}

DEFAULT_TEMPLATE = "links"


def template_for(key: str) -> dict:
    return TEMPLATES.get(key or DEFAULT_TEMPLATE, TEMPLATES[DEFAULT_TEMPLATE])


# ------------------------------------------------------------ custom domains
def normalise_domain(raw: str) -> str:
    """Strip scheme, path, port and a leading www. from user input."""
    import re as _re
    host = (raw or "").strip().lower()
    host = _re.sub(r"^https?://", "", host).split("/")[0].split(":")[0]
    return host.removeprefix("www.")


def domain_problem(host: str) -> str:
    import re as _re
    if not host:
        return "Enter a domain."
    if not _re.match(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$", host):
        return "That does not look like a domain name."
    return ""


def verify_domain(host: str, expected_ips: set[str]) -> tuple[bool, str]:
    """Does this domain actually point at us?

    Resolves the domain and compares against the server's public IPs. This
    is deliberately an ownership check, not a formality: without it anyone
    could claim someone else's domain and have us serve content on it.
    """
    import socket
    if not expected_ips:
        return False, ("SERVER_IPS is not configured, so domain ownership cannot be "
                       "checked. Set it to this server's public IP address.")
    try:
        resolved = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror as exc:
        return False, f"{host} does not resolve yet ({exc.strerror or exc})."
    if resolved & expected_ips:
        return True, f"{host} points here. It may take a few minutes to serve HTTPS."
    return False, (f"{host} resolves to {', '.join(sorted(resolved))}, which is not this "
                   f"server. Point an A record at {', '.join(sorted(expected_ips))}.")
