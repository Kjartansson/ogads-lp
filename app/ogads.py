"""The only module that talks to OGAds, and the only one that sees the key.

Errors are classified into a small set of kinds because they need
different handling: `auth` means our credentials are wrong and retrying is
pointless, `upstream` means the network said no, `network` is worth one
retry, and `empty` is a successful call that simply matched no offers for
this visitor -- which is normal, not a fault, and must never be reported
as an outage.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from .cache import TTLCache
from . import config
from .config import BASE_DIR, settings
from .models import Offer, parse_offers

log = logging.getLogger("ogads.client")

FIXTURE = BASE_DIR / "fixtures" / "offers_sample.json"

ERROR_KINDS = ("auth", "upstream", "network", "bad_response", "not_configured")


class OgAdsError(Exception):
    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status

    @property
    def visitor_message(self) -> str:
        """What a visitor may see. Never leaks upstream detail or the key."""
        if self.kind == "network":
            return "Offers are taking too long to load. Please try again."
        return "Offers are temporarily unavailable. Please try again shortly."

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message, "status": self.status}


class OgAdsClient:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self.cache = TTLCache(ttl=settings.cache_ttl)
        self.last_error: OgAdsError | None = None
        self.last_success_at: float | None = None
        self.calls = 0

    async def startup(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.timeout),
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )

    async def shutdown(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------------- params
    def _params(
        self,
        ip: str,
        user_agent: str,
        ctype: str = "",
        aff_sub4: str = "",
        aff_sub5: str = "",
    ) -> dict[str, str]:
        """Build the query string.

        This is the complete documented parameter set: ip and user_agent are
        required, ctype/max/min optional, and aff_sub4/aff_sub5 are the only
        two pass-through slots the Offer API exposes (aff_sub1-3 and `source`
        exist for tracking links, NOT for this API). Targeting by country or
        device is not a parameter -- OGAds derives both from the ip and
        user_agent we send, which is why sending the visitor's real ones is
        the whole ballgame.
        """
        params = {
            "ip": ip,
            "user_agent": user_agent,
            "ctype": ctype,
            "aff_sub4": aff_sub4,
            "aff_sub5": aff_sub5,
        }
        if settings.max_offers > 0:
            params["max"] = str(settings.max_offers)
        if settings.min_offers > 0:
            params["min"] = str(settings.min_offers)
        # An empty string reads as a filter value upstream, not as "unset",
        # so blanks are stripped rather than sent.
        return {k: str(v) for k, v in params.items() if v not in (None, "")}

    # ------------------------------------------------------------------ call
    async def _request(self, params: dict[str, str], api_key: str = "") -> list[dict]:
        if settings.mock:
            return self._mock_offers()
        key = api_key or config.api_key()
        if not key:
            raise OgAdsError("not_configured", "OGADS_API_KEY is not set")
        if self._client is None:
            await self.startup()

        last_exc: Exception | None = None
        for attempt in (1, 2):
            try:
                self.calls += 1
                resp = await self._client.get(  # type: ignore[union-attr]
                    config.endpoint(),
                    params=params,
                    headers={"Authorization": f"Bearer {key}"},
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt == 1:
                    await asyncio.sleep(0.6)
                    continue
                raise OgAdsError("network", f"{type(exc).__name__}: {exc}") from exc

            if resp.status_code in (401, 403):
                raise OgAdsError(
                    "auth",
                    "OGAds rejected the API key (check the key and that the endpoint "
                    "matches the locker domain issued to that account)",
                    resp.status_code,
                )
            if resp.status_code == 429:
                raise OgAdsError("upstream", "Rate limited by OGAds", 429)
            if resp.status_code >= 500 and attempt == 1:
                await asyncio.sleep(0.6)
                continue
            if resp.status_code >= 400:
                raise OgAdsError("upstream", f"HTTP {resp.status_code}", resp.status_code)

            try:
                payload: Any = resp.json()
            except (json.JSONDecodeError, ValueError) as exc:
                raise OgAdsError(
                    "bad_response", f"Response was not JSON: {resp.text[:200]!r}"
                ) from exc

            if not isinstance(payload, dict):
                raise OgAdsError("bad_response", f"Unexpected payload type {type(payload).__name__}")
            if not payload.get("success"):
                # A false `success` with an error string is the network
                # refusing us (bad key, banned, malformed params) -- upstream,
                # not an empty result.
                raise OgAdsError("upstream", str(payload.get("error") or "OGAds returned success=false"))

            offers = payload.get("offers")
            if not isinstance(offers, list):
                raise OgAdsError("bad_response", "Payload had no `offers` array")
            return offers

        raise OgAdsError("network", f"Exhausted retries: {last_exc}")

    def _mock_offers(self) -> list[dict]:
        with FIXTURE.open() as fh:
            return json.load(fh).get("offers", [])

    # ----------------------------------------------------------------- public
    async def get_offers(
        self,
        ip: str,
        user_agent: str,
        *,
        ctype: str = "",
        aff_sub4: str = "",
        aff_sub5: str = "",
        use_cache: bool = True,
        on_fetch=None,
        api_key: str = "",
    ) -> list[Offer]:
        params = self._params(ip, user_agent, ctype, aff_sub4, aff_sub5)
        # aff_sub values are pass-through tracking data: they change per
        # visitor and per video but never change WHICH offers come back, so
        # they are excluded from the cache key.
        # The cache key MUST include which OGAds account asked. Smartlinks
        # are minted per account, so a shared cache entry would hand one
        # creator's tracking link to another creator's visitor and pay the
        # wrong person. Only a fingerprint is used -- never the key itself,
        # so credentials cannot leak through a debug dump of the cache.
        account = hashlib.sha256((api_key or config.api_key()).encode()).hexdigest()[:16]
        key = (account,) + tuple(
            sorted((k, v) for k, v in params.items() if not k.startswith("aff_sub")))

        async def produce() -> list[Offer]:
            raw = await self._request(params, api_key=api_key)
            offers = parse_offers(raw)
            self.last_success_at = asyncio.get_running_loop().time()
            self.last_error = None
            log.info("ogads ok: %d offers ip=%s ctype=%s", len(offers), ip, ctype or "-")
            # Fires only on a real upstream call, so availability bookkeeping
            # scales with OGAds requests rather than with pageviews.
            if on_fetch is not None:
                try:
                    on_fetch(offers)
                except Exception:
                    log.exception("on_fetch hook failed; offers still served")
            return offers

        try:
            if not use_cache:
                return await produce()
            return await self.cache.get_or_set(key, produce)
        except OgAdsError as exc:
            self.last_error = exc
            log.warning("ogads %s: %s", exc.kind, exc.message)
            raise

    def health(self) -> dict:
        return {
            "configured": bool(config.api_key()) or settings.mock,
            "mock": settings.mock,
            "endpoint": config.endpoint(),
            "api_key_masked": config.mask(config.api_key()),
            "upstream_calls": self.calls,
            "last_error": self.last_error.to_dict() if self.last_error else None,
            "cache": self.cache.stats(),
        }


client = OgAdsClient()
