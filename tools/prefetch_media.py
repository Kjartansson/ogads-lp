#!/usr/bin/env python3
"""Warm the store-listing and gameplay-video cache.

Two dimensions of "all geo" and they are not the same thing:

  storefront   Each offer is resolved against EVERY country it targets, so a
               FR visitor gets the FR listing (French description, FR rating
               count) and a US visitor gets the US one. This tool covers
               that completely on its own.

  offer pool   Which offers OGAds returns is decided by the IP you call
               from. From a Danish IP you cannot see the US pool at all.
               Pass --ip once per geo you actually target.

    ./.venv/bin/python tools/prefetch_media.py --ip 23.45.21.76 --ip 198.51.100.7
    ./.venv/bin/python tools/prefetch_media.py --all-storefronts --limit 40

Run it after adding a YOUTUBE_API_KEY, and on a daily cron so new offers
are enriched before anyone lands on them.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import appmeta, media as media_mod, store          # noqa: E402
from app.config import settings                             # noqa: E402
from app.ogads import OgAdsError, client                    # noqa: E402

UAS = {
    "iphone": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "android": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
}


def _storefronts(offer, args, priority: list[str]) -> list[str]:
    """Which storefronts to resolve for this offer, best-value first.

    Countries we actually receive traffic from come first, then the offer's
    own order. Capped, because breadth here is nearly free to request and
    expensive to serve.
    """
    # Storefront breadth only buys anything for iOS offers, because Apple's
    # listing is the only per-country thing we fetch. An Android-only offer
    # gets one row -- its video is not country-specific, and expanding a
    # MultiGEO Android offer across 200 countries writes 200 identical
    # empty rows.
    if not appmeta.targets_ios(offer):
        return [appmeta.storefront_for(offer)]
    targets = [("gb" if c.upper() == "UK" else c.lower()) for c in offer.countries]
    if not targets:
        return ["us"]
    if not args.all_storefronts:
        return [appmeta.storefront_for(offer)]
    ranked = [c for c in priority if c in targets]
    ranked += [c for c in targets if c not in ranked]
    return list(dict.fromkeys(ranked))[:max(1, args.max_storefronts)]


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", action="append", default=[],
                    help="visitor IP to discover offers from; repeat per geo "
                         "(default: TEST_IP from .env)")
    ap.add_argument("--all-storefronts", action="store_true",
                    help="resolve more than the primary storefront per offer, capped "
                         "by --max-storefronts")
    ap.add_argument("--max-storefronts", type=int, default=3,
                    help="ceiling per offer (default 3). MultiGEO offers target ~200 "
                         "countries; resolving them all is hundreds of calls for "
                         "storefronts nobody loads")
    ap.add_argument("--limit", type=int, default=0, help="stop after N lookups")
    ap.add_argument("--force", action="store_true", help="refresh even if cached and fresh")
    args = ap.parse_args()

    ips = args.ip or ([settings.test_ip] if settings.test_ip else [])
    if not ips:
        print("No IP to discover offers from. Pass --ip or set TEST_IP in .env.",
              file=sys.stderr)
        return 2

    store.init()
    if not settings.youtube_api_key:
        print("note: YOUTUBE_API_KEY is not set — store listings will be cached, "
              "gameplay videos will not be discovered automatically.\n", file=sys.stderr)

    priority = store.traffic_countries()
    if priority:
        print(f"prioritising storefronts by observed traffic: {', '.join(priority)}\n")
    await client.startup()
    seen: set[tuple[str, str]] = set()
    done = ok_app = ok_video = 0
    try:
        for ip in ips:
            for device, ua in UAS.items():
                try:
                    offers = await client.get_offers(ip, ua, ctype=settings.ctype_mobile,
                                                     use_cache=False)
                except OgAdsError as exc:
                    print(f"  {ip}/{device}: [{exc.kind}] {exc.message}", file=sys.stderr)
                    continue
                print(f"{ip} / {device}: {len(offers)} offers")
                for offer in offers:
                    fronts = _storefronts(offer, args, priority)
                    for front in fronts:
                        if (offer.id, front) in seen:
                            continue
                        seen.add((offer.id, front))
                        if args.limit and done >= args.limit:
                            print(f"\nstopped at --limit {args.limit}")
                            raise SystemExit(_report(done, ok_app, ok_video))
                        if not args.force:
                            _, stale = media_mod.cached(offer, front.upper())
                            if not stale:
                                print(f"  {offer.id:>7} {front}  cached")
                                continue
                        m = await media_mod.refresh(offer, front.upper())
                        done += 1
                        ok_app += bool(m.app)
                        ok_video += bool(m.video)
                        bits = []
                        if m.app:
                            bits.append(f"app: {m.app.title[:34]} ({m.app.rating:.1f}★ "
                                        f"{m.app.rating_count:,})")
                        if m.video:
                            bits.append(f"video: {m.video.channel[:22]}")
                        print(f"  {offer.id:>7} {front}  " +
                              ("  |  ".join(bits) if bits else "nothing found"))
                        await asyncio.sleep(0.25)   # be polite to Apple
    finally:
        await client.shutdown()
    return _report(done, ok_app, ok_video)


def _report(done: int, ok_app: int, ok_video: int) -> int:
    print(f"\nresolved {done} offer/storefront pairs — "
          f"{ok_app} store listings, {ok_video} videos")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
