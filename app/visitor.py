"""Who is asking -- IP, User-Agent, device class.

This module decides what OGAds is told about the visitor, which decides
which offers come back. Getting the IP wrong is the difference between a
full offer list and an empty one.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from fastapi import Request

from .config import settings

_MOBILE = re.compile(
    r"android|iphone|ipad|ipod|iemobile|blackberry|opera mini|opera mobi|"
    r"windows phone|webos|palm|symbian|kindle|silk|mobile safari|fennec",
    re.I,
)
_TABLET = re.compile(r"ipad|tablet|kindle|silk|playbook|nexus (?:7|9|10)", re.I)


def is_public_ip(value: str) -> bool:
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_unspecified
        or addr.is_multicast
    )


def _from_headers(request: Request) -> str:
    """Resolve the client IP, trusting only as many proxy hops as configured.

    X-Forwarded-For is client-writable, so a visitor can prepend anything
    they like. We therefore count from the RIGHT: with N trusted hops the
    real client is the Nth entry from the end, and with 0 trusted hops the
    header is ignored entirely in favour of the socket peer.
    """
    cf = (request.headers.get("cf-connecting-ip") or "").strip()
    if settings.trusted_proxy_hops and is_public_ip(cf):
        return cf

    hops = settings.trusted_proxy_hops
    if hops:
        xff = request.headers.get("x-forwarded-for") or ""
        chain = [p.strip() for p in xff.split(",") if p.strip()]
        if chain:
            idx = max(0, len(chain) - hops)
            candidate = chain[idx]
            if is_public_ip(candidate):
                return candidate

    return (request.client.host if request.client else "") or ""


@dataclass(frozen=True)
class Visitor:
    ip: str
    user_agent: str
    is_mobile: bool
    is_tablet: bool
    simulated: bool  # True when TEST_IP/debug overrides supplied the identity
    country: str = ""  # ISO-2, from the CDN edge; "" when unknown

    @property
    def device_class(self) -> str:
        if self.is_tablet:
            return "tablet"
        return "mobile" if self.is_mobile else "desktop"

    @property
    def device_tokens(self) -> set[str]:
        """Device names as OGAds spells them in an offer's `device` field.

        Live values look like "Android", "iPhone", "iPhone,iPad" or
        "iPhone,Android". An iPad is matched by both "iPad" and the looser
        "iPhone" that some iOS offers use to mean "any iOS".
        """
        ua = self.user_agent.lower()
        if "ipad" in ua:
            return {"ipad", "iphone", "ios", "tablet"}
        if "iphone" in ua or "ipod" in ua:
            return {"iphone", "ios"}
        if "android" in ua:
            return {"android", "tablet"} if self.is_tablet else {"android"}
        if self.is_mobile:
            return {"mobile"}
        return {"desktop"}

    @property
    def ctype(self) -> str:
        """OGAds category filter for this device class.

        OGAds ignores ctype entirely for desktop devices, so sending it
        there is noise -- we omit it and let the network decide.
        """
        return settings.ctype_mobile if (self.is_mobile or self.is_tablet) else ""


def resolve(request: Request) -> Visitor:
    ip = _from_headers(request)
    ua = request.headers.get("user-agent", "")
    simulated = False

    # Debug-only per-request impersonation, for testing several geos/devices
    # against the live API without deploying. Never active unless DEBUG=1.
    if settings.debug:
        override_ip = request.query_params.get("_ip", "").strip()
        override_ua = request.query_params.get("_ua", "").strip()
        if override_ip:
            ip, simulated = override_ip, True
        if override_ua:
            ua, simulated = override_ua, True

    # Fall back to the configured test identity only when the real one is
    # unusable (localhost / LAN). In production this never fires.
    if not is_public_ip(ip) and settings.test_ip:
        ip, simulated = settings.test_ip, True
        if not ua or "curl" in ua.lower() or "python" in ua.lower():
            ua = settings.test_ua

    # Country comes from the CDN edge, which resolves it far more reliably
    # than anything we could do from the IP alone. Cloudflare sets
    # CF-IPCountry; without a CDN in front this stays empty and country
    # filtering simply does not engage.
    country = (request.headers.get("cf-ipcountry") or "").strip().upper()
    if settings.debug:
        country = (request.query_params.get("_country") or country).strip().upper()
    if country in ("XX", "T1", ""):   # CF's unknown/Tor placeholders
        country = ""

    ua_final = ua or settings.test_ua
    return Visitor(
        ip=ip,
        user_agent=ua_final,
        is_mobile=bool(_MOBILE.search(ua_final)),
        is_tablet=bool(_TABLET.search(ua_final)),
        simulated=simulated,
        country=country,
    )
