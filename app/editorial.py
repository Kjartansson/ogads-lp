"""Turns an Offer into review-article content.

Everything here is DERIVED, never invented. The advertiser supplies the
description, the exact conversion requirement, the device and country
targeting and the OS floor; this module reorganises those facts into the
shape of a review and picks the wording. What it deliberately does not do
is manufacture the things a reader would take as first-hand testimony --
star ratings, download counts, user quotes, or an author claiming to have
played the game. Those would be fabricated endorsements, and a review site
that carries them is one complaint away from being worthless.

The tone is positive because the site recommends apps. The facts under the
tone are the advertiser's own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Offer, strip_locker_clause

# Country codes -> reader-facing names. OGAds writes UK, not GB.
COUNTRY_NAMES = {
    "US": "the US", "CA": "Canada", "UK": "the UK", "GB": "the UK", "AU": "Australia",
    "NZ": "New Zealand", "IE": "Ireland", "DE": "Germany", "FR": "France",
    "ES": "Spain", "IT": "Italy", "NL": "the Netherlands", "SE": "Sweden",
    "NO": "Norway", "DK": "Denmark", "FI": "Finland", "PL": "Poland",
    "CZ": "Czechia", "RU": "Russia", "BR": "Brazil", "MX": "Mexico",
    "JP": "Japan", "KR": "South Korea", "IN": "India", "ZA": "South Africa",
    "BE": "Belgium", "AT": "Austria", "CH": "Switzerland", "PT": "Portugal",
    "GR": "Greece", "HU": "Hungary", "RO": "Romania", "BG": "Bulgaria",
    "HR": "Croatia", "SI": "Slovenia", "SK": "Slovakia", "LT": "Lithuania",
    "LV": "Latvia", "EE": "Estonia", "IS": "Iceland", "LU": "Luxembourg",
    "CY": "Cyprus", "MT": "Malta", "TR": "Turkey", "IL": "Israel",
    "AE": "the UAE", "SA": "Saudi Arabia", "KW": "Kuwait", "QA": "Qatar",
    "AR": "Argentina", "CL": "Chile", "CO": "Colombia", "PE": "Peru",
    "TH": "Thailand", "VN": "Vietnam", "PH": "the Philippines",
    "ID": "Indonesia", "MY": "Malaysia", "SG": "Singapore",
    "HK": "Hong Kong", "TW": "Taiwan", "PR": "Puerto Rico",
}

# Requirement wording -> how much the app actually asks of the reader.
_EFFORT_RULES = (
    (re.compile(r"\b(purchase|buy|subscribe|deposit|payment|credit card|trial)\b", re.I),
     "High", "asks for a purchase or payment details"),
    (re.compile(r"\b(level|tutorial|complete|reach|race|stage|round|wave|chapter)\b", re.I),
     "Medium", "wants you to actually play for a bit"),
    (re.compile(r"\b(sign\s?up|register|profile|survey|questions?)\b", re.I),
     "Medium", "needs a short sign-up"),
    (re.compile(r"\b(install|open|launch|run|download)\b", re.I),
     "Low", "is done as soon as the app opens"),
)

_HEADLINES = (
    "{name} review: {angle}",
    "{name}: {angle}",
    "Is {name} worth it? {angle_cap}",
    "{name} reviewed — {angle}",
)


def _sentence(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if text[-1] not in ".!?":
        text += "."
    return text[0].upper() + text[1:]


def _countries(codes: list[str]) -> str:
    if not codes:
        return "most regions"
    named = [COUNTRY_NAMES.get(c.upper(), c.upper()) for c in codes]
    seen: list[str] = []
    for n in named:                      # UK and GB can both appear
        if n not in seen:
            seen.append(n)
    if len(seen) == 1:
        return seen[0]
    if len(seen) == 2:
        return f"{seen[0]} and {seen[1]}"
    if len(seen) <= 4:
        return ", ".join(seen[:-1]) + f" and {seen[-1]}"
    return f"{', '.join(seen[:3])} and {len(seen) - 3} more countries"


def _devices(devices: list[str]) -> str:
    d = {x.lower() for x in devices}
    if not d:
        return "phones and tablets"
    if {"iphone", "ipad"} <= d:
        return "iPhone and iPad"
    if "iphone" in d and "android" in d:
        return "iPhone and Android"
    if "iphone" in d:
        return "iPhone"
    if "ipad" in d:
        return "iPad"
    if "android" in d:
        return "Android"
    return ", ".join(devices)


def _platform_note(name: str) -> str:
    """Size / OS floor that OGAds packs into the offer's long name.

    Live names look like "(Android, Free, INCENT, US, 156MB, 5.0)", so the
    size is a real published fact, just badly placed.
    """
    m = re.search(r"\b(\d+(?:\.\d+)?)\s?(MB|GB|M|G)\b", name, re.I)
    if not m:
        return ""
    unit = m.group(2).upper()
    # Offer names abbreviate inconsistently: "156MB" and "113M" mean the same.
    return f"{m.group(1)}{ {'M': 'MB', 'G': 'GB'}.get(unit, unit) }".replace(" ", "")


@dataclass(frozen=True)
class Review:
    offer: Offer
    headline: str
    standfirst: str
    what_it_is: str
    what_you_do: str
    effort: str
    effort_note: str
    availability: str
    device_line: str
    download_size: str
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    verdict: str = ""
    read_minutes: int = 2

    @property
    def effort_class(self) -> str:
        return self.effort.lower()


def build(offer: Offer) -> Review:
    devices = _devices(offer.devices)
    countries = _countries(offer.countries)
    size = _platform_note(offer.name)
    requirement = _sentence(strip_locker_clause(offer.requirement or offer.adcopy))

    effort, effort_note = "Low", "is quick to get through"
    for pattern, level, note in _EFFORT_RULES:
        if pattern.search(offer.requirement or offer.adcopy or ""):
            effort, effort_note = level, note
            break

    # The lead angle is picked from what is genuinely notable about THIS
    # offer, so two reviews on the page do not open the same way.
    if effort == "Low":
        angle = "a couple of taps and you're done"
    elif "survey" in offer.name_short.lower() or "match" in offer.name_short.lower():
        angle = "a few questions, then straight to the apps"
    elif offer.countries and len(offer.countries) >= 5:
        angle = f"available across {len(offer.countries)} countries"
    else:
        angle = f"worth the few minutes it asks for on {devices}"

    headline = _HEADLINES[int(offer.id) % len(_HEADLINES)].format(
        name=offer.name_short, angle=angle, angle_cap=angle[0].upper() + angle[1:])

    what_it_is = _sentence(offer.summary) or _sentence(offer.adcopy) or \
        f"{offer.name_short} is a free app available on {devices}."

    standfirst = (
        f"{offer.name_short} runs on {devices} in {countries}, and "
        f"{effort_note}. Here's what to expect before you tap install."
    )

    pros = [f"Free to download on {devices}"]
    if effort == "Low":
        pros.append("Counts as soon as you open it — no grinding")
    if not re.search(r"purchase|buy|subscribe|deposit|payment", requirement, re.I):
        pros.append("No purchase needed")
    if len(offer.countries) >= 4:
        pros.append(f"Works in {len(offer.countries)} countries")
    if offer.summary:
        pros.append("The developer is clear about what the app does")

    cons = []
    if len(offer.countries) == 1:
        cons.append(f"Only available in {countries}")
    if len(offer.devices) == 1:
        cons.append(f"{devices} only — no version for the other platform")
    if offer.targeting:
        cons.append(f"Needs {offer.targeting}")
    if effort in ("Medium", "High"):
        cons.append(f"You have to finish the step first: {requirement.rstrip('.')}")
    if size:
        cons.append(f"{size} download — worth being on Wi-Fi")
    if not cons:
        cons.append("Availability changes through the day, so it may not show for everyone")

    verdict = (
        f"If you're on {devices} in {countries}, {offer.name_short} is an easy yes: "
        f"{effort.lower()} effort, nothing to pay, and you know up front exactly what "
        f"it takes to qualify."
    ) if effort == "Low" else (
        f"{offer.name_short} asks a little more than a plain install — "
        f"{requirement.rstrip('.').lower()} — but it's clearly stated up front, "
        f"which is more than a lot of apps manage. Worth it if the genre appeals to you."
    )

    words = len(f"{what_it_is} {standfirst} {verdict}".split()) + 120
    return Review(
        offer=offer, headline=headline, standfirst=standfirst,
        what_it_is=what_it_is, what_you_do=requirement,
        effort=effort, effort_note=effort_note,
        availability=countries, device_line=devices, download_size=size,
        pros=pros[:4], cons=cons[:4], verdict=verdict,
        read_minutes=max(1, round(words / 200)),
    )


DISCLOSURE = (
    "We earn a commission when you install an app through a link on this page. "
    "It costs you nothing and it does not change what we write — the "
    "requirements listed for each app are the advertiser's own published terms."
)
