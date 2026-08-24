# OGAds review site

An app-review blog that serves live OGAds app-install offers, targeted to
each visitor's country and device, with full funnel tracking from the
TikTok video that sent them through to the paid conversion.

One video → one review page → one tracked click → one attributed postback.

## Run it

```bash
cp .env.example .env      # then paste your API key in, see below
./run.sh                  # http://127.0.0.1:8080
```

`OGADS_MOCK=1` serves `fixtures/offers_sample.json` and never calls OGAds,
so the whole site works offline.

## The one thing that breaks every local OGAds setup

**OGAds picks the offers from the visitor's IP and User-Agent.** There is no
`country` or `device` parameter — those are derived from what you forward.
From localhost the IP is `127.0.0.1`, which matches no country, and the API
returns an empty list. Nothing is wrong; you just told it nobody is there.

`TEST_IP` in `.env` fixes this: when the real client IP is private, that one
is sent instead. Your own public IP works (`curl -s https://api.ipify.org`).

With `DEBUG=1` you can also impersonate a visitor per request:

```
/?_ip=23.45.21.76&_ua=<urlencoded UA>&_country=GB
```

All three are ignored unless `DEBUG=1`, so they cannot be used against a
deployed instance.

## Layout

| File | Role |
|---|---|
| `app/ogads.py` | The only module that sees the API key. Retries, typed errors, TTL cache. |
| `app/models.py` | Normalises the raw offer. Strips the HTML in `description`, splits out the Conversion / Traffic Restrictions / Targeting sections. |
| `app/editorial.py` | Turns an offer into review copy. Derives, never invents — see below. |
| `app/visitor.py` | Resolves IP, User-Agent, device class and country. Decides what OGAds is told. |
| `app/store.py` | SQLite: `visits` → `clicks` → `conversions`. |
| `app/main.py` | Routes. |
| `tools/export_offers.py` | Dumps the offer list for video planning. |

## Tracking

Two values are carried end to end. Both are set on the offer API request,
baked into the smartlink OGAds returns, and echoed back in the postback:

| | | |
|---|---|---|
| `aff_sub4` | `source` | which video sent them — the `?v=` on your link |
| `aff_sub5` | `session_id` | who they are, via a first-party cookie |

`GET /go/{id}` deliberately bypasses the offer cache. OGAds bakes the
aff_subs from the *request* into the link it returns, so serving a cached
list would credit every conversion to whichever video happened to warm the
cache first.

Country comes from the CDN edge (`CF-IPCountry`). Without a CDN in front it
stays unknown; everything else still works.

### Postback

Get the exact string from the running app:

```bash
curl -s "http://127.0.0.1:8080/admin/postback-url?token=$(grep -oP '^ADMIN_TOKEN=\K.*' .env)"
```

Paste it into <https://members.ogads.com/tools/postback-url>. Macros there
are case sensitive and unrecognised ones are silently stripped.

**The endpoint is authenticated**, because anything that writes revenue rows
from a query string is a free-money endpoint otherwise. A caller must be in
the published OGAds IP allowlist, or present `POSTBACK_SECRET`. Loopback is
accepted only while `DEBUG=1`.

Postbacks need a publicly reachable `SITE_BASE_URL`. Localhost will never
receive one — expect the conversions table to stay empty until you deploy.

## Ads: point them at a campaign, never at an offer

OGAds offers cap, pause and get pulled with no notice and **no status field**
— an offer just stops appearing in the API response. An ad pointed straight
at `/review/{offer}` keeps billing you for clicks after that moment.

So ads point at `/lp/{campaign}`, which resolves at request time:

| | |
|---|---|
| pinned offer still live | serve it — `pinned` |
| pinned offer gone | serve the best eligible alternative — `fallback` |
| nothing eligible at all | `no_fill` — **stop spending** |

Create and pin campaigns in `/admin`. `/review/{slug}` and `/go/{id}` also
substitute the best live offer rather than 404ing, so organic traffic from an
older video does not dead-end when its offer caps.

Guard the spend from cron:

```bash
*/15 * * * * cd /path/to/OGADS && ./.venv/bin/python tools/check_campaigns.py --quiet
```

Exit 1 means at least one campaign is degraded or dead. `/admin/health`
returns the same judgement as JSON with a `stop_spending_on` list.

### Revenue-share (RS) offers

OGAds tags these with a trailing ` RS` in the offer name. They pay
**progressively** — several postbacks for one user as they go deeper — so:

- the `payout` field is the **first step only**, and is frequently `0.00` on
  offers that do earn well. Ranking on payout buries them, so ranking uses
  EPC (`Offer.ranking_score`).
- the conversions table deduplicates on
  `(offer_id, session_id, converted_at, payout)`. Payout is part of the key
  precisely so a second RS step is not mistaken for a retry of the first.
- conversions can legitimately exceed clicks on an RS-heavy mix.

## Gameplay video and store data

Each review can carry real gameplay footage and the app's real App Store
listing — rating, developer, size, age rating, screenshots.

**Store data** comes from Apple's public Search API: keyless, and resolved
**per storefront**, because the same app genuinely differs by country. Travel
Town is 4.7 from 2,524 ratings in Belgium and 4.6 from 4,662 in the
Netherlands, so the cache key is `(offer, storefront)` and a visitor sees
the listing they would actually get.

Everything shown is attributed. The rating is rendered as "4.7 from 2,169
ratings on the DK App Store", screenshots are labelled as the developer's,
and the page states we have not rated the app ourselves. Nothing here
invents a review.

Matching is strict — a wrong match would put another game's screenshots on
a review, so the offer's short name must be fully contained in the store
title (`Travel Town` → `Travel Town - Merge Adventure` scores 1.0;
`Township` scores 0.0).

**Android is a gap.** Google Play has no public JSON API and scraping it is
fragile and against its terms, so Android-only offers get no store panel
rather than an iOS listing passed off as an Android one.

**Video** is always *embedded*, never re-hosted — embedding is free, legal,
and keeps the uploader's attribution and view count. (This is also why
voomreel is not the tool for it: it fetches a known URL, which is a
different problem, and self-hosting the file would be republishing someone
else's footage.)

- **Pinned** — paste a YouTube URL in `/admin`. Title and channel come from
  YouTube's keyless oEmbed, so attribution works with **no API key today**.
- **Automatic** — needs `YOUTUBE_API_KEY`. Results are filtered by
  `videoEmbeddable`, `safeSearch=strict`, a title-relevance check, and a
  blocklist for `mod`/`apk`/`hack`/`free gems` titles.

One caveat worth knowing: oEmbed rejects private and removed videos but
**cannot** detect an uploader who disabled embedding — that only appears as
a "Video unavailable" box in the player. Open the review page once after
pinning. Automatic picks are safe because the Data API filters on it.

Warm the cache (a pageview never blocks on these APIs — a cold offer renders
without media and schedules the lookup in the background):

```bash
./.venv/bin/python tools/prefetch_media.py --all-storefronts
```

Storefront breadth is capped per offer and prioritised by the countries you
actually get traffic from, because a MultiGEO offer targets ~200 countries
and resolving all of them is hundreds of calls for storefronts nobody loads.

Note the two different senses of "all geos": storefront coverage is complete
per offer, but *which offers exist* is decided by the IP you call from — from
a Danish IP you cannot see the US pool. Pass `--ip` once per geo you target.

## Creator link pages (multi-tenant)

Creators sign up at `/signup`, connect **their own** OGAds key, pick 3-6
offers, and get a hosted page for their TikTok/YouTube bio at `/u/<username>`.

**Why creator-owned keys.** Their traffic pays their account directly, so the
platform never holds their money, never runs payouts, and is not acting as a
sub-affiliate network on one OGAds account — which is the arrangement CPA
networks typically prohibit and which would put *your* account at risk.

**The fee is taken in traffic, not cash.** A fixed share of a creator's
visitors (default `PLATFORM_SHARE_PCT=10`) is served the platform's offers
instead of theirs. Two properties make that a fee rather than a trick:

- *Deterministic* — the bucket hashes `(visitor session, creator)`, so one
  person gets a consistent page instead of a list that re-rolls on reload.
  Measured distribution over 10,000 visitors lands on 10.0% for a 10% rate.
- *Auditable* — every visit records which side was served, and the creator's
  dashboard shows agreed rate and **measured** rate side by side. It is also
  spelled out at signup behind a required checkbox.

**Page layouts** — creators pick one, so they never have to build a lander:
`links` (bio-link buttons), `spotlight` (one offer, full review treatment
with video and App Store data), `grid` (app cards).

**Custom domains** — a creator can attach `links.theirdomain.com`. Nothing is
served on it until DNS demonstrably resolves to this server (`SERVER_IPS`),
because an unverified entry would let anyone claim a hostname they do not
control. Host-based routing maps the domain root to their page; `/admin`,
`/static` and the API stay reachable on every host so a DNS mistake cannot
lock you out. TLS needs a reverse proxy that issues certs on demand (Caddy
does this with `on_demand_tls`).

### Security notes

- Passwords: scrypt, per-user salt. Never logged.
- Creator OGAds keys: Fernet-encrypted at rest with `CREDENTIAL_KEY`, so a
  leaked database backup does not hand out live API keys. **Losing that key
  makes every stored key unreadable** and every creator page stops earning.
- Sessions: signed with `APP_SECRET`; forms carry CSRF tokens.
- Login is rate limited per account *and* per IP.
- Login failures return one message for both wrong-password and unknown-email,
  so the form cannot enumerate accounts.
- The offer cache is keyed by a hash of the API key. Without that, one
  creator's smartlink could be served to another's visitor and pay the wrong
  person.

## Admin

`/admin?token=…` is the control panel:

- **Ad campaigns** — create, pin, pause, and see fill status per campaign.
- **Funnel** — visits, clicks, conversions, revenue; by source, offer,
  referrer, country and device.
- **Offer availability** — every offer we have served and when it was last
  seen live for that audience. This is how you spot a cap.
- **Gameplay videos & store data** — what is cached, pin or clear a video.
- **Connection & domain** — change the OGAds API key, the API endpoint, and
  the public landing domain. A new key is **tested against the live API
  before it is saved**; if it fails, nothing changes. Changes are written
  back to `.env` atomically and take effect without a restart.

The API key is never rendered into the page — only a mask like `4711…241c`.

## Tests

```bash
./.venv/bin/python tools/smoke.py
```

Runs the whole funnel in-process against fixtures — no server, no network,
throwaway database. It exists because a refactor once deleted the helpers
behind the review page and the index still rendered fine, so nothing looked
broken until an article was requested.

## Deploying

`run.sh` passes `--no-proxy-headers` on purpose. uvicorn defaults to
`proxy_headers=True`, which rewrites `request.client.host` from the caller's
own `X-Forwarded-For` *before any application code runs* — that silently
defeats the postback IP allowlist. Behind a real proxy, turn it back on and
pin the proxy:

```
--proxy-headers --forwarded-allow-ips=<proxy ip>      # + TRUSTED_PROXY_HOPS=1
```

Never set `TRUSTED_PROXY_HOPS` higher than the number of proxies you
actually run; below that point the header is attacker-controlled.

## What the editorial layer will and won't write

`app/editorial.py` builds each review from the advertiser's own published
data: the description, the exact conversion requirement, the device and
country targeting, the OS floor and download size. Effort ratings, pros and
cons are derived from those facts.

It does not generate star ratings, review counts, user testimonials, or any
first-person claim of having used the app, because none of that would be
true. Fabricated endorsements are what gets affiliate sites delisted and are
what the FTC's endorsement rules are actually about. The affiliate
disclosure in the footer is there for the same reason.

`tools/export_offers.py` flags offers whose terms conflict with a self-made
short video — a social-traffic ban, or a "Custom Creatives" restriction,
since your video *is* a custom creative. Those conversions can be reversed
after they are credited. The flag is a warning; the offer's page in the
OGAds dashboard is the authority.

## Video planning

```bash
./.venv/bin/python tools/export_offers.py --country US --device iphone --format table
./.venv/bin/python tools/export_offers.py --country US --device android --icons --format csv --out exports/plan.csv
```

Each row carries the landing URL with its per-video `?v=` tag already
attached, so a video's performance shows up as its own row in `/admin`.

## Dashboard

`/admin?token=…` — visits, clicks, conversions and revenue, broken down by
source, offer, referrer, country and device. Same numbers as JSON at
`/admin/stats`.
