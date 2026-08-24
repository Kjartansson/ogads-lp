#!/usr/bin/env python3
"""Cron guard: stop paying for ads that land on nothing.

OGAds offers cap and pause with no notice. When every landing for a
campaign comes back `no_fill`, each further ad click is spend with zero
chance of revenue. Run this on a schedule and act on the exit code.

    */15 * * * * cd /path/to/OGADS && ./.venv/bin/python tools/check_campaigns.py --quiet

Exit codes:
    0  all active campaigns are filling
    1  at least one campaign is degraded or dead  -> pause that ad set
    2  could not evaluate (no database, bad arguments)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import store                      # noqa: E402
from app.config import settings            # noqa: E402

BAD = {"no_fill", "degraded"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=int, default=6,
                    help="window to judge on (default 6)")
    ap.add_argument("--min-landings", type=int, default=5,
                    help="ignore campaigns with fewer landings than this, so a "
                         "brand-new campaign is not condemned on one bad request")
    ap.add_argument("--quiet", action="store_true", help="only print problems")
    args = ap.parse_args()

    if not settings.db_path.exists():
        print(f"no database at {settings.db_path}", file=sys.stderr)
        return 2

    rows = store.campaign_health(hours=args.hours)
    if not rows:
        if not args.quiet:
            print("no campaigns defined")
        return 0

    problems = []
    for c in rows:
        if not c["active"] or c["landings"] < args.min_landings:
            continue
        if c["status"] in BAD:
            problems.append(c)

    if not args.quiet:
        print(f"{'CAMPAIGN':<24}{'STATUS':<14}{'LAND':>6}{'FILL':>7}{'NOFILL':>8}")
        for c in rows:
            fill = 1 - c["no_fill_rate"]
            print(f"{c['id'][:23]:<24}{c['status']:<14}{c['landings']:>6}"
                  f"{fill*100:>6.0f}%{c['no_fill']:>8}")

    for c in problems:
        print(f"\nPAUSE ADS: campaign '{c['id']}' is {c['status']} — "
              f"{c['no_fill']} of {c['landings']} landings in the last {args.hours}h "
              f"had no offer to show.", file=sys.stderr)
        if c["pinned_offer_id"]:
            print(f"  It is pinned to offer #{c['pinned_offer_id']}. If that offer has "
                  f"capped, clear the pin in /admin so the campaign can fall back.",
                  file=sys.stderr)

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
