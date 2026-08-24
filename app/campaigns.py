"""Ad campaign resolution.

The rule this module exists to enforce: **an ad points at a campaign, never
at an offer id.** OGAds offers cap, pause and get pulled with no warning and
no status field — an offer simply stops appearing in the API response. If a
paid ad points straight at one, every click after that moment is money spent
on a dead landing page.

A campaign resolves to a live offer at request time instead:

    pinned offer still live for this visitor   -> serve it        (pinned)
    pinned offer gone, something else eligible -> serve that      (fallback)
    nothing eligible at all                    -> tell the caller (no_fill)

`no_fill` is the signal to stop spending. It is recorded per landing so
`tools/check_campaigns.py` can pause ads from cron.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .models import Offer

log = logging.getLogger("ogads.campaigns")

PINNED, FALLBACK, NO_FILL = "pinned", "fallback", "no_fill"


@dataclass(frozen=True)
class Resolution:
    outcome: str
    offer: Offer | None
    campaign_id: str
    registered: bool          # False when the ad used an id we do not know
    pinned_offer_id: str = ""

    @property
    def wasted(self) -> bool:
        """True when paid traffic landed on nothing."""
        return self.outcome == NO_FILL

    @property
    def note(self) -> str:
        if self.outcome == PINNED:
            return "serving the pinned offer"
        if self.outcome == FALLBACK:
            return ("pinned offer is not available for this visitor; serving the "
                    "best eligible alternative") if self.pinned_offer_id else \
                   "serving the best eligible offer"
        return "nothing eligible for this visitor"


def best(offers: list[Offer]) -> Offer | None:
    """Highest expected value per click.

    Sorted on ranking_score rather than payout so a revenue-share offer,
    whose headline payout is only its first step and is often 0.00, is not
    pushed to the bottom of every list.
    """
    return max(offers, key=lambda o: o.ranking_score, default=None)


def resolve(campaign: dict | None, campaign_id: str, offers: list[Offer]) -> Resolution:
    """Pick what this ad landing should show. `offers` is already filtered
    to what OGAds returned as eligible for this visitor's geo and device."""
    pinned_id = (campaign or {}).get("pinned_offer_id", "") or ""
    registered = campaign is not None

    if not offers:
        log.warning("campaign %s: NO FILL -- no eligible offers for this visitor",
                    campaign_id)
        return Resolution(NO_FILL, None, campaign_id, registered, pinned_id)

    if pinned_id:
        pinned = next((o for o in offers if o.id == pinned_id), None)
        if pinned is not None:
            return Resolution(PINNED, pinned, campaign_id, registered, pinned_id)
        log.info("campaign %s: pinned offer %s unavailable, falling back",
                 campaign_id, pinned_id)

    return Resolution(FALLBACK, best(offers), campaign_id, registered, pinned_id)
