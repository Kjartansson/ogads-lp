"""Normalisation of raw OGAds offer dicts into a stable internal shape.

Two things about the live payload drive this module:

1. `description` is an HTML fragment, not text. It arrives with unbalanced
   <b> tags and \\r\\n runs, so rendering it through an autoescaping
   template shows the visitor literal "</b>" characters.
2. That description packs several labelled sections into one string --
   "Conversion:", "Traffic Restrictions:", "Targeting:". Only the first is
   meant for a visitor. Traffic Restrictions is meant for US, and matters:
   offers routinely forbid the exact traffic type this project produces.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, asdict, field

_SPLIT = re.compile(r"[,/|]")
# OGAds tags revenue-share offers by suffixing the long name with " RS".
# Configurable because the tagging convention is theirs to change.
_RS_TAG = re.compile(r"\bRS\b\s*$")
_TAG = re.compile(r"<[^>]*>")
_WS = re.compile(r"[ \t]+")
# Section labels seen in live descriptions, tolerant of the stray tags and
# spacing that sit between the label and its colon.
# OGAds adcopy is written for a content LOCKER ("...to unlock this content").
# This site locks nothing, so that clause is meaningless here and reads as a
# promise we never make. It is trimmed, never reworded -- the rest of the
# advertiser's sentence is left exactly as written.
_LOCKER_TAIL = re.compile(
    r"\s*,?\s*(?:in order )?(?:to|and)\s+(?:unlock|access|continue|get|receive|claim)\s+"
    r"(?:this|the|your)?\s*(?:content|file|download|reward|video|page)?\s*[.!]?\s*$",
    re.I,
)

_LABEL = re.compile(
    r"\b(Conversion|Traffic\s+Restrictions|Restrictions|Targeting|Requirements?)\s*:",
    re.I,
)


def _clean(raw_html: str) -> str:
    text = _TAG.sub(" ", raw_html or "")
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WS.sub(" ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()


def _sections(description_html: str) -> tuple[str, dict[str, str]]:
    """Split a description into its lead paragraph and labelled sections."""
    text = _clean(description_html)
    if not text:
        return "", {}
    marks = list(_LABEL.finditer(text))
    if not marks:
        return text, {}
    intro = text[: marks[0].start()].strip()
    out: dict[str, str] = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        key = re.sub(r"\s+", "_", m.group(1).strip().lower())
        out[key] = text[m.end() : end].strip(" \n:")
    return intro, out


def strip_locker_clause(text: str) -> str:
    """Remove a trailing '...to unlock this content' from advertiser copy."""
    return _LOCKER_TAIL.sub("", (text or "").strip()).strip()


def _text(raw: dict, *keys: str) -> str:
    for k in keys:
        v = raw.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _money(raw: dict, key: str) -> float:
    v = raw.get(key)
    if v in (None, ""):
        return 0.0
    try:
        return round(float(str(v).replace("$", "").strip()), 4)
    except ValueError:
        return 0.0


def _tokens(raw: dict, key: str) -> list[str]:
    v = raw.get(key)
    if v in (None, ""):
        return []
    parts = [str(p) for p in v] if isinstance(v, list) else _SPLIT.split(str(v))
    return [p.strip() for p in parts if p.strip()]


@dataclass(frozen=True)
class Offer:
    id: str
    name: str
    name_short: str
    summary: str            # visitor-safe lead paragraph, tags stripped
    requirement: str        # what the user must actually do to convert
    adcopy: str
    picture: str
    payout: float
    payout_display: str
    countries: list[str] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)
    link: str = ""
    epc: float = 0.0
    restrictions: list[str] = field(default_factory=list)
    targeting: str = ""
    # Undocumented but live fields. The published example response has none
    # of these, which is the whole reason parsing here is permissive.
    cvr: float = 0.0          # conversion rate, percent
    boosted: bool = False     # network-promoted offer
    category: str = ""        # "CPI" / "CPA" / "PIN" / "VID"
    is_rs: bool = False       # revenue share: pays progressively, see below

    @classmethod
    def from_api(cls, raw: dict) -> "Offer | None":
        offer_id = _text(raw, "offerid", "offer_id", "id")
        link = _text(raw, "link", "url")
        # No id or no link means the card cannot be tracked or clicked.
        if not offer_id or not link:
            return None

        intro, sections = _sections(_text(raw, "description"))
        adcopy = _clean(_text(raw, "adcopy"))
        payout = _money(raw, "payout")
        name = _text(raw, "name") or _text(raw, "name_short") or f"Offer {offer_id}"
        restrictions_raw = sections.get("traffic_restrictions") or sections.get("restrictions", "")

        return cls(
            id=offer_id,
            name=name,
            name_short=_text(raw, "name_short") or name,
            summary=intro,
            # The advertiser's own "Conversion:" line is the precise
            # requirement; adcopy is the marketing paraphrase of it.
            requirement=strip_locker_clause(
                sections.get("conversion") or sections.get("requirement")
                or sections.get("requirements") or adcopy),
            adcopy=adcopy,
            picture=_text(raw, "picture", "image"),
            payout=payout,
            payout_display=f"${payout:.2f}",
            countries=_tokens(raw, "country"),
            devices=_tokens(raw, "device"),
            link=link,
            epc=_money(raw, "epc"),
            restrictions=[r.strip() for r in restrictions_raw.split(";") if r.strip()],
            targeting=sections.get("targeting", ""),
            # Undocumented but live. Read defensively -- the published example
            # response contains none of these, so their presence, names and
            # types are all subject to change without notice.
            cvr=_money(raw, "cvr"),
            boosted=str(raw.get("boosted", "")).strip().lower() in ("true", "1", "yes"),
            category=_text(raw, "ctype").upper(),
            is_rs=bool(_RS_TAG.search(name)),
        )

    @property
    def blurb(self) -> str:
        """Best one-liner for a list card."""
        return self.adcopy or self.requirement or self.summary or self.name

    @property
    def slug(self) -> str:
        s = re.sub(r"[^a-z0-9]+", "-", self.name_short.lower()).strip("-")
        return f"{self.id}-{s}"[:80] if s else self.id

    def eligible_for(self, country: str, device_tokens: set[str]) -> bool:
        """Does this offer actually target this visitor?

        OGAds already targets by the ip/user_agent we send, so in normal
        operation everything it returns is eligible. This is a safety net
        for the cases where it is not: a stale cache entry, a visitor whose
        User-Agent is unusual, or a desktop request where ctype is ignored.
        Showing an Android-only offer to an iPhone is a guaranteed
        non-conversion and a wasted click.

        An offer with no stated targeting is treated as open.
        """
        if self.devices:
            if not {d.lower() for d in self.devices} & device_tokens:
                return False
        if country and self.countries:
            wanted = {c.upper() for c in self.countries}
            # OGAds writes the United Kingdom as "UK"; Cloudflare's
            # CF-IPCountry sends the ISO code "GB". Without this alias every
            # UK visitor is filtered out of every UK offer.
            aliases = {country}
            if country == "GB":
                aliases.add("UK")
            elif country == "UK":
                aliases.add("GB")
            if not aliases & wanted:
                return False
        return True

    @property
    def payout_note(self) -> str:
        """How this offer pays, for anywhere a bare payout would mislead.

        Revenue-share offers pay progressively as the user goes deeper into
        the advertiser's funnel, so the `payout` field is the FIRST step's
        value, not the total -- and it is frequently 0.00 on offers that do
        earn. Ranking or dismissing an RS offer on payout alone is wrong.
        """
        if not self.is_rs:
            return ""
        return ("Revenue share: pays progressively as the user completes more "
                "steps. The listed payout is the first step only.")

    @property
    def ranking_score(self) -> float:
        """Sort key that does not punish RS offers for a 0.00 first step.

        EPC already blends payout and conversion rate across real traffic,
        which makes it the honest comparator between a flat CPI and an RS
        offer whose headline payout understates it.
        """
        return self.epc if self.epc else (self.payout * (self.cvr / 100.0))

    @property
    def video_restricted(self) -> bool:
        """True when this offer's terms conflict with a self-made short video.

        Two distinct restrictions both bite here:
          - social traffic bans ("Instagram/Twitter Traffic"), which forbid
            the channel the traffic arrives from;
          - "Custom Creatives", which forbids producing your own ad material
            at all -- and a TikTok video about the app IS a custom creative.

        Either way the conversion can be reversed after it is credited, so
        the export tool flags these instead of quietly planning a video that
        will not be paid for. It is a warning, not a verdict: the offer page
        in the OGAds dashboard is the authority.
        """
        blob = " ".join(self.restrictions).lower()
        return any(term in blob for term in
                   ("instagram", "twitter", "tiktok", "social", "custom creative"))

    def public_dict(self) -> dict:
        """Serialisation for our own JSON API.

        `link` is withheld: the browser gets our /go/{id} redirect instead so
        every click is attributed. Handing out the raw smartlink would let
        clicks bypass tracking entirely.
        """
        d = asdict(self)
        d.pop("link", None)
        d["blurb"] = self.blurb
        d["slug"] = self.slug
        return d


def parse_offers(raw_offers: list) -> list[Offer]:
    out = []
    for raw in raw_offers or []:
        if isinstance(raw, dict):
            offer = Offer.from_api(raw)
            if offer is not None:
                out.append(offer)
    return out
