#!/usr/bin/env python3
"""Export the current offer list for video production and content planning.

One video per offer is the plan, so this dumps what each video needs: the
hook line, the exact conversion requirement, the landing URL with its
per-video ?v= tag already attached, and the app icon.

It also flags offers whose terms conflict with a self-made short video --
either a social-traffic ban or a "Custom Creatives" restriction, since the
video itself is a custom creative. Those conversions can be reversed after
they are credited: the payout looks fine in the dashboard and then vanishes
at invoice time.

    ./.venv/bin/python tools/export_offers.py --country US --device iphone
    ./.venv/bin/python tools/export_offers.py --icons --format csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import BASE_DIR, settings          # noqa: E402
from app.ogads import OgAdsError, client           # noqa: E402

DEVICE_UA = {
    "iphone": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "ipad":   "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "android": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
    "desktop": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/126.0.0.0 Safari/537.36",
}

FIELDS = ["offer_id", "name_short", "payout", "epc", "countries", "devices",
          "requirement", "hook", "landing_url", "video_tag",
          "video_restricted", "restrictions", "picture"]


def video_tag(offer) -> str:
    """Stable per-offer ?v= value, so one video maps to one row in /admin."""
    slug = re.sub(r"[^a-z0-9]+", "-", offer.name_short.lower()).strip("-")
    return f"tt-{offer.id}-{slug}"[:64]


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", default=settings.test_ip,
                    help="visitor IP to target (defaults to TEST_IP from .env)")
    ap.add_argument("--country", default="", help="filter to this ISO-2 country")
    ap.add_argument("--device", default="iphone", choices=sorted(DEVICE_UA))
    ap.add_argument("--format", default="json", choices=["json", "csv", "table"])
    ap.add_argument("--out", default="", help="output file (default: stdout)")
    ap.add_argument("--icons", action="store_true", help="also download app icons")
    ap.add_argument("--include-restricted", action="store_true",
                    help="include offers whose terms conflict with a self-made video")
    args = ap.parse_args()

    if not args.ip:
        print("No IP to target. Pass --ip or set TEST_IP in .env.\n"
              "OGAds picks offers from the visitor IP; without one you get nothing.",
              file=sys.stderr)
        return 2

    await client.startup()
    try:
        offers = await client.get_offers(
            args.ip, DEVICE_UA[args.device], ctype=settings.ctype_mobile, use_cache=False)
    except OgAdsError as exc:
        print(f"OGAds error [{exc.kind}]: {exc.message}", file=sys.stderr)
        return 1
    finally:
        await client.shutdown()

    tokens = {args.device, "ios" if args.device in ("iphone", "ipad") else args.device}
    offers = [o for o in offers if o.eligible_for(args.country.upper(), tokens)]
    if not args.include_restricted:
        offers = [o for o in offers if not o.video_restricted]

    rows = [{
        "offer_id": o.id,
        "name_short": o.name_short,
        "payout": o.payout,
        "epc": o.epc,
        "countries": ",".join(o.countries),
        "devices": ",".join(o.devices),
        "requirement": o.requirement,
        "hook": o.adcopy or o.summary,
        "landing_url": f"{settings.base_url}/review/{o.slug}?v={video_tag(o)}",
        "video_tag": video_tag(o),
        "video_restricted": o.video_restricted,
        "restrictions": "; ".join(o.restrictions),
        "picture": o.picture,
    } for o in sorted(offers, key=lambda x: x.epc, reverse=True)]

    if args.icons and rows:
        import httpx
        icons = BASE_DIR / "exports" / "icons"
        icons.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
            for r in rows:
                if not r["picture"]:
                    continue
                ext = Path(r["picture"].split("?")[0]).suffix or ".png"
                try:
                    resp = await http.get(r["picture"])
                    resp.raise_for_status()
                    (icons / f"{r['offer_id']}{ext}").write_bytes(resp.content)
                except (httpx.HTTPError, OSError) as exc:
                    print(f"  icon {r['offer_id']} failed: {exc}", file=sys.stderr)
        print(f"icons -> {icons}", file=sys.stderr)

    out = open(args.out, "w", newline="") if args.out else sys.stdout
    try:
        if args.format == "json":
            json.dump(rows, out, indent=2)
            out.write("\n")
        elif args.format == "csv":
            w = csv.DictWriter(out, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)
        else:
            print(f"{'ID':<8}{'EPC':>7}{'PAYOUT':>8}  {'NAME':<28}REQUIREMENT", file=out)
            for r in rows:
                flag = " [CREATIVE/TRAFFIC RESTRICTED]" if r["video_restricted"] else ""
                print(f"{r['offer_id']:<8}{r['epc']:>7.3f}{r['payout']:>8.2f}  "
                      f"{r['name_short'][:27]:<28}{r['requirement'][:44]}{flag}", file=out)
    finally:
        if args.out:
            out.close()
            print(f"wrote {len(rows)} offers -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
