"""SQLite persistence for the full funnel: visit -> click -> conversion.

Three tables, joined on two keys that are carried end to end:

    source      aff_sub4 -- WHICH video/campaign sent this person
    session_id  aff_sub5 -- WHO this person is, across the whole funnel

Both are handed to OGAds on the offer request, baked into the smartlink it
returns, and echoed back verbatim in the conversion postback. That round
trip is the only reason a conversion can be attributed to a video at all.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .config import settings

log = logging.getLogger("ogads.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS visits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    INTEGER NOT NULL,
    session_id    TEXT    NOT NULL UNIQUE,   -- first touch only, see record_visit
    ip            TEXT    NOT NULL DEFAULT '',
    user_agent    TEXT    NOT NULL DEFAULT '',
    device        TEXT    NOT NULL DEFAULT '',
    country       TEXT    NOT NULL DEFAULT '',   -- ISO-2 from the CDN edge
    landing_path  TEXT    NOT NULL DEFAULT '',
    source        TEXT    NOT NULL DEFAULT '',
    referrer      TEXT    NOT NULL DEFAULT '',
    referrer_host TEXT    NOT NULL DEFAULT '',
    utm_source    TEXT    NOT NULL DEFAULT '',
    utm_medium    TEXT    NOT NULL DEFAULT '',
    utm_campaign  TEXT    NOT NULL DEFAULT '',
    utm_content   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_visits_created ON visits(created_at);
CREATE INDEX IF NOT EXISTS idx_visits_source  ON visits(source);
CREATE INDEX IF NOT EXISTS idx_visits_ref     ON visits(referrer_host);
CREATE INDEX IF NOT EXISTS idx_visits_country ON visits(country);

CREATE TABLE IF NOT EXISTS clicks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  INTEGER NOT NULL,
    offer_id    TEXT    NOT NULL,
    offer_name  TEXT    NOT NULL DEFAULT '',
    payout      REAL    NOT NULL DEFAULT 0,
    ip          TEXT    NOT NULL DEFAULT '',
    user_agent  TEXT    NOT NULL DEFAULT '',
    device      TEXT    NOT NULL DEFAULT '',
    source      TEXT    NOT NULL DEFAULT '',
    session_id  TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_clicks_offer   ON clicks(offer_id);
CREATE INDEX IF NOT EXISTS idx_clicks_source  ON clicks(source);
CREATE INDEX IF NOT EXISTS idx_clicks_created ON clicks(created_at);
CREATE INDEX IF NOT EXISTS idx_clicks_session ON clicks(session_id);

CREATE TABLE IF NOT EXISTS conversions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   INTEGER NOT NULL,
    offer_id     TEXT    NOT NULL DEFAULT '',
    offer_name   TEXT    NOT NULL DEFAULT '',
    affiliate_id TEXT    NOT NULL DEFAULT '',
    ip           TEXT    NOT NULL DEFAULT '',   -- {session_ip}
    source       TEXT    NOT NULL DEFAULT '',   -- {aff_sub4}, else {source}
    session_id   TEXT    NOT NULL DEFAULT '',   -- {aff_sub5}
    payout       REAL    NOT NULL DEFAULT 0,
    converted_at TEXT    NOT NULL DEFAULT '',   -- {datetime}, advertiser clock
    remote_ip    TEXT    NOT NULL DEFAULT '',
    raw_query    TEXT    NOT NULL DEFAULT '',
    -- OGAds retries postbacks, so identical events must collapse. But
    -- revenue-share offers fire SEVERAL postbacks for one user as they go
    -- deeper into the funnel, each with its own payout -- so payout is part
    -- of the identity. Without it, every RS step after the first is
    -- silently discarded and the offer looks like it barely earns.
    UNIQUE(offer_id, session_id, converted_at, payout)
);
CREATE INDEX IF NOT EXISTS idx_conv_source  ON conversions(source);
CREATE INDEX IF NOT EXISTS idx_conv_created ON conversions(created_at);
CREATE INDEX IF NOT EXISTS idx_conv_offer   ON conversions(offer_id);

-- Which offers OGAds actually returned, per audience. An offer that stops
-- appearing for an audience it used to serve is capped, paused or pulled --
-- there is no "status" field to read, absence IS the signal.
CREATE TABLE IF NOT EXISTS offer_seen (
    offer_id   TEXT NOT NULL,
    country    TEXT NOT NULL DEFAULT '',
    device     TEXT NOT NULL DEFAULT '',
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    seen_count INTEGER NOT NULL DEFAULT 1,
    name       TEXT NOT NULL DEFAULT '',
    payout     REAL NOT NULL DEFAULT 0,
    epc        REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (offer_id, country, device)
);
CREATE INDEX IF NOT EXISTS idx_seen_last ON offer_seen(last_seen);

-- An ad points at a CAMPAIGN, never at an offer id. The campaign resolves
-- to a live offer at request time, so a capped offer costs a fallback
-- rather than a dead landing page bought at CPC.
CREATE TABLE IF NOT EXISTS campaigns (
    id              TEXT PRIMARY KEY,
    label           TEXT    NOT NULL DEFAULT '',
    pinned_offer_id TEXT    NOT NULL DEFAULT '',  -- '' = always best available
    target_country  TEXT    NOT NULL DEFAULT '',
    target_device   TEXT    NOT NULL DEFAULT '',
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      INTEGER NOT NULL
);

-- One row per ad landing, recording what the campaign was able to serve.
-- This is what tells you to pause spend.
CREATE TABLE IF NOT EXISTS campaign_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    campaign   TEXT NOT NULL,
    outcome    TEXT NOT NULL,          -- pinned | fallback | no_fill
    offer_id   TEXT NOT NULL DEFAULT '',
    country    TEXT NOT NULL DEFAULT '',
    device     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cev_campaign ON campaign_events(campaign, created_at);

-- Creators using the hosted link page. Each brings their OWN OGAds key, so
-- their traffic pays their account directly: no payouts to run, no sub-
-- affiliate arrangement on the platform's account.
CREATE TABLE IF NOT EXISTS creators (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash  TEXT NOT NULL,
    created_at     INTEGER NOT NULL,
    last_login     INTEGER NOT NULL DEFAULT 0,
    display_name   TEXT NOT NULL DEFAULT '',
    bio            TEXT NOT NULL DEFAULT '',
    api_key_enc    TEXT NOT NULL DEFAULT '',   -- Fernet ciphertext, never plaintext
    -- Percentage of VISITORS served the platform's offers instead of this
    -- creator's. Per creator so it can be waived or raised individually.
    platform_share INTEGER NOT NULL DEFAULT 10,
    suspended      INTEGER NOT NULL DEFAULT 0,
    suspend_reason TEXT NOT NULL DEFAULT '',
    -- Which ready-made landing page layout their traffic lands on.
    page_template  TEXT NOT NULL DEFAULT 'links',
    -- Optional vanity host. Only served once verified: an unverified entry
    -- would let anyone claim a domain they do not control and have us serve
    -- their content on it.
    custom_domain  TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    domain_verified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_creator_domain ON creators(custom_domain);

CREATE TABLE IF NOT EXISTS creator_links (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id INTEGER NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    offer_id   TEXT    NOT NULL,
    title      TEXT    NOT NULL DEFAULT '',   -- creator's own wording, optional
    UNIQUE(creator_id, offer_id)
);
CREATE INDEX IF NOT EXISTS idx_links_creator ON creator_links(creator_id, position);

-- One row per visitor to a creator page, recording which side was served.
-- This is what makes the platform share an auditable number the creator can
-- see, rather than something taken quietly.
CREATE TABLE IF NOT EXISTS creator_visits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    creator_id INTEGER NOT NULL,
    session_id TEXT    NOT NULL DEFAULT '',
    served     TEXT    NOT NULL DEFAULT 'creator',   -- 'creator' | 'platform'
    country    TEXT    NOT NULL DEFAULT '',
    device     TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cvisits ON creator_visits(creator_id, created_at);

-- Store listing + gameplay video for an offer, per storefront. Cached
-- because the review page must not pay a third-party round trip on every
-- render, and because these sources rate-limit.
CREATE TABLE IF NOT EXISTS offer_media (
    offer_id    TEXT NOT NULL,
    storefront  TEXT NOT NULL,            -- ISO-2, lowercase; the Apple storefront
    app_json    TEXT NOT NULL DEFAULT '', -- normalised AppMeta, '' = looked and found none
    video_id    TEXT NOT NULL DEFAULT '',
    video_json  TEXT NOT NULL DEFAULT '',
    pinned      INTEGER NOT NULL DEFAULT 0,  -- 1 = a human chose this video, never auto-replace
    app_at      INTEGER NOT NULL DEFAULT 0,
    video_at    INTEGER NOT NULL DEFAULT 0,
    -- Artwork supplied by us, which outranks anything fetched. This is the
    -- slot for a designed hero or infographic; it is never overwritten by a
    -- refresh, unlike the fetched fields above.
    custom_icon TEXT NOT NULL DEFAULT '',
    custom_hero TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (offer_id, storefront)
);
"""


# Columns added after the first release. Applied on every start so an
# existing data/ogads.db is upgraded in place instead of being wiped.
MIGRATIONS = {
    "visits": {"country": "TEXT NOT NULL DEFAULT ''"},
    # Which creator's page produced this click, so a creator's own stats and
    # the platform's own traffic stay separable.
    "clicks": {"creator_id": "INTEGER NOT NULL DEFAULT 0",
               "served": "TEXT NOT NULL DEFAULT ''"},
    "offer_media": {"custom_icon": "TEXT NOT NULL DEFAULT ''",
                    "custom_hero": "TEXT NOT NULL DEFAULT ''"},
    "creators": {"page_template": "TEXT NOT NULL DEFAULT 'links'",
                 "custom_domain": "TEXT NOT NULL DEFAULT ''",
                 "domain_verified": "INTEGER NOT NULL DEFAULT 0"},
}


def _migrate_conversions_unique(conn) -> bool:
    """Rebuild `conversions` if it still carries the pre-RS unique key.

    SQLite cannot alter a table constraint in place, so the table is copied.
    Detection reads the stored DDL rather than a version number, which keeps
    this correct for a database created at any point.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='conversions'"
    ).fetchone()
    if not row or not row["sql"]:
        return False
    ddl = " ".join(row["sql"].split())
    if "UNIQUE(offer_id, session_id, converted_at, payout)" in ddl:
        return False
    if "UNIQUE(offer_id, session_id, converted_at)" not in ddl:
        return False
    conn.executescript("""
        ALTER TABLE conversions RENAME TO conversions_old;
    """)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO conversions (id, created_at, offer_id, offer_name, affiliate_id, "
        "ip, source, session_id, payout, converted_at, remote_ip, raw_query) "
        "SELECT id, created_at, offer_id, offer_name, affiliate_id, ip, source, "
        "session_id, payout, converted_at, remote_ip, raw_query FROM conversions_old")
    conn.execute("DROP TABLE conversions_old")
    return True


def init() -> None:
    path: Path = settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    with connect() as conn:
        conn.executescript(SCHEMA)
        if _migrate_conversions_unique(conn):
            log.warning("conversions table rebuilt: unique key now includes payout "
                        "so revenue-share offers record every step")
        for table, columns in MIGRATIONS.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    if fresh:
        # Rows hold visitor IPs and user agents; keep the file off world-read.
        os.chmod(path, 0o600)


@contextmanager
def connect():
    conn = sqlite3.connect(settings.db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
        conn.commit()
    finally:
        conn.close()


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


# ------------------------------------------------------------------- writes
def record_visit(
    *, session_id: str, ip: str, user_agent: str, device: str, country: str,
    landing_path: str, source: str, referrer: str, utm: dict[str, str],
) -> bool:
    """First-touch attribution: only the session's FIRST landing is stored.

    A visitor who lands from a video and then browses three offer pages is
    one acquisition, not four, and the video that earned them must not be
    overwritten by an internal navigation with no referrer.
    """
    with connect() as conn:
        try:
            conn.execute(
                "INSERT INTO visits (created_at, session_id, ip, user_agent, device, "
                "country, landing_path, source, referrer, referrer_host, utm_source, "
                "utm_medium, utm_campaign, utm_content) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(time.time()), session_id, ip, user_agent[:400], device,
                 country[:2], landing_path[:200], source, referrer[:400], host_of(referrer),
                 utm.get("utm_source", ""), utm.get("utm_medium", ""),
                 utm.get("utm_campaign", ""), utm.get("utm_content", "")),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def record_click(
    *, offer_id: str, offer_name: str, payout: float, ip: str, user_agent: str,
    device: str, source: str, session_id: str, creator_id: int = 0, served: str = "",
) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO clicks (created_at, offer_id, offer_name, payout, ip, "
            "user_agent, device, source, session_id, creator_id, served) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), offer_id, offer_name, payout, ip,
             user_agent[:400], device, source, session_id, creator_id, served),
        )
        return int(cur.lastrowid or 0)


def record_conversion(
    *, offer_id: str, offer_name: str, affiliate_id: str, ip: str, source: str,
    session_id: str, payout: float, converted_at: str, remote_ip: str, raw_query: str,
) -> bool:
    """Returns False when this postback duplicates one already stored."""
    with connect() as conn:
        try:
            conn.execute(
                "INSERT INTO conversions (created_at, offer_id, offer_name, affiliate_id, "
                "ip, source, session_id, payout, converted_at, remote_ip, raw_query) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (int(time.time()), offer_id, offer_name[:200], affiliate_id, ip, source,
                 session_id, payout, converted_at, remote_ip, raw_query[:1000]),
            )
            return True
        except sqlite3.IntegrityError:
            return False


# -------------------------------------------------------------------- reads
def _rows(conn, sql: str, args=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


# --------------------------------------------------- availability tracking
def record_offers_seen(offers, country: str, device: str) -> None:
    """Upsert the live offer set for one audience.

    Called only on a genuine upstream fetch, not on every cache hit, so the
    write volume tracks OGAds calls rather than pageviews.
    """
    now = int(time.time())
    with connect() as conn:
        conn.executemany(
            "INSERT INTO offer_seen (offer_id, country, device, first_seen, last_seen, "
            "seen_count, name, payout, epc) VALUES (?,?,?,?,?,1,?,?,?) "
            "ON CONFLICT(offer_id, country, device) DO UPDATE SET "
            "  last_seen=excluded.last_seen, seen_count=seen_count+1, "
            "  name=excluded.name, payout=excluded.payout, epc=excluded.epc",
            [(o.id, country, device, now, now, o.name_short, o.payout, o.epc) for o in offers],
        )


def offer_availability(stale_after: int = 3600) -> list[dict]:
    """Every offer we have ever served, and whether it is still appearing."""
    now = int(time.time())
    with connect() as conn:
        rows = _rows(conn,
            "SELECT offer_id, name, country, device, payout, epc, seen_count, "
            "first_seen, last_seen FROM offer_seen ORDER BY last_seen DESC LIMIT 200")
    for r in rows:
        r["age_seconds"] = now - r["last_seen"]
        r["live"] = r["age_seconds"] <= stale_after
    return rows


def freshness(offer_ids: list[str], country: str, device: str,
              new_within: int = 172800) -> dict[str, int]:
    """When we FIRST saw each of these offers for this audience.

    Returns {offer_id: first_seen_epoch}, and an empty dict when we have not
    been watching this audience for longer than `new_within`. That guard
    matters: on a fresh database every offer was "first seen" seconds ago,
    so without it every single offer would be badged NEW on day one, which
    is exactly the kind of manufactured freshness that makes a site look
    fake. Real signal or no signal.
    """
    if not offer_ids:
        return {}
    now = int(time.time())
    marks = ",".join("?" * len(offer_ids))
    with connect() as conn:
        oldest = conn.execute(
            "SELECT MIN(first_seen) m FROM offer_seen WHERE country=? AND device=?",
            (country, device)).fetchone()["m"]
        if not oldest or (now - oldest) < new_within:
            return {}
        rows = conn.execute(
            f"SELECT offer_id, first_seen FROM offer_seen "
            f"WHERE country=? AND device=? AND offer_id IN ({marks})",
            (country, device, *offer_ids)).fetchall()
    return {r["offer_id"]: r["first_seen"] for r in rows}


def traffic_countries(limit: int = 12) -> list[str]:
    """Countries we have actually had visitors from, busiest first.

    Used to prioritise which storefronts are worth pre-caching: a MultiGEO
    offer can target 200 countries, and resolving all of them is hundreds of
    pointless API calls for storefronts no one will ever load.
    """
    with connect() as conn:
        return [r["country"].lower() for r in conn.execute(
            "SELECT country, COUNT(*) n FROM visits WHERE country != '' "
            "GROUP BY country ORDER BY n DESC LIMIT ?", (limit,))]


# -------------------------------------------------------------- creators
def create_creator(*, username: str, email: str, password_hash: str,
                   display_name: str = "", platform_share: int = 10) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO creators (username, email, password_hash, created_at, "
            "display_name, platform_share) VALUES (?,?,?,?,?,?)",
            (username.lower(), email.lower(), password_hash, int(time.time()),
             display_name[:60], platform_share))
        return int(cur.lastrowid or 0)


def get_creator(creator_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM creators WHERE id=?", (creator_id,)).fetchone()
    return dict(row) if row else None


def get_creator_by_username(username: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM creators WHERE username=?",
                           (username.lower(),)).fetchone()
    return dict(row) if row else None


def get_creator_by_email(email: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM creators WHERE email=?",
                           (email.lower(),)).fetchone()
    return dict(row) if row else None


def get_creator_by_domain(host: str) -> dict | None:
    """Only ever returns a VERIFIED domain owner."""
    h = (host or "").split(":")[0].lower().removeprefix("www.")
    if not h:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM creators WHERE custom_domain=? AND domain_verified=1 "
            "AND suspended=0", (h,)).fetchone()
    return dict(row) if row else None


def domain_taken(host: str, exclude_creator_id: int = 0) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM creators WHERE custom_domain=? AND id!=?",
            (host.lower(), exclude_creator_id)).fetchone()
    return row is not None


def touch_login(creator_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE creators SET last_login=? WHERE id=?",
                     (int(time.time()), creator_id))


def update_creator(creator_id: int, **fields) -> None:
    allowed = {"display_name", "bio", "api_key_enc", "platform_share",
               "suspended", "suspend_reason", "password_hash",
               "page_template", "custom_domain", "domain_verified"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    assigns = ", ".join(f"{k}=?" for k in sets)
    with connect() as conn:
        conn.execute(f"UPDATE creators SET {assigns} WHERE id=?",
                     (*sets.values(), creator_id))


def list_creators(limit: int = 200) -> list[dict]:
    with connect() as conn:
        return _rows(conn,
            "SELECT c.id, c.username, c.email, c.created_at, c.last_login, "
            "  c.platform_share, c.suspended, c.suspend_reason, "
            "  c.page_template, c.custom_domain, c.domain_verified, "
            "  CASE WHEN c.api_key_enc='' THEN 0 ELSE 1 END AS has_key, "
            "  (SELECT COUNT(*) FROM creator_links l WHERE l.creator_id=c.id) links, "
            "  (SELECT COUNT(*) FROM creator_visits v WHERE v.creator_id=c.id) visits "
            "FROM creators c ORDER BY c.created_at DESC LIMIT ?", (limit,))


# ---------------------------------------------------------- creator links
def get_creator_links(creator_id: int) -> list[dict]:
    with connect() as conn:
        return _rows(conn,
            "SELECT offer_id, title, position FROM creator_links "
            "WHERE creator_id=? ORDER BY position, id", (creator_id,))


def set_creator_links(creator_id: int, entries: list[tuple[str, str]]) -> None:
    """Replace a creator's chosen offers. `entries` is [(offer_id, title)]."""
    with connect() as conn:
        conn.execute("DELETE FROM creator_links WHERE creator_id=?", (creator_id,))
        conn.executemany(
            "INSERT INTO creator_links (creator_id, position, offer_id, title) "
            "VALUES (?,?,?,?)",
            [(creator_id, i, oid, title[:80]) for i, (oid, title) in enumerate(entries)])


# --------------------------------------------------------- creator traffic
def record_creator_visit(*, creator_id: int, session_id: str, served: str,
                         country: str, device: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO creator_visits (created_at, creator_id, session_id, served, "
            "country, device) VALUES (?,?,?,?,?,?)",
            (int(time.time()), creator_id, session_id, served, country[:2], device))


def creator_stats(creator_id: int, days: int = 30) -> dict:
    """What a creator sees about their own page -- including, explicitly, how
    many of their visitors were served the platform's offers."""
    since = int(time.time()) - days * 86400
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) total, "
            "  SUM(CASE WHEN served='platform' THEN 1 ELSE 0 END) platform "
            "FROM creator_visits WHERE creator_id=? AND created_at>=?",
            (creator_id, since)).fetchone()
        clicks = conn.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN served='platform' THEN 1 ELSE 0 END) p "
            "FROM clicks WHERE creator_id=? AND created_at>=?",
            (creator_id, since)).fetchone()
        by_offer = _rows(conn,
            "SELECT offer_id, MAX(offer_name) offer_name, COUNT(*) clicks "
            "FROM clicks WHERE creator_id=? AND created_at>=? AND served!='platform' "
            "GROUP BY offer_id ORDER BY clicks DESC LIMIT 20", (creator_id, since))
    total = row["total"] or 0
    platform = row["platform"] or 0
    return {
        "window_days": days,
        "visits": total,
        "visits_platform": platform,
        "visits_yours": total - platform,
        "platform_share_actual": round(platform / total, 4) if total else 0.0,
        "clicks": clicks["n"] or 0,
        "clicks_platform": clicks["p"] or 0,
        "by_offer": by_offer,
    }


# ----------------------------------------------------------------- media
def get_media(offer_id: str, storefront: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM offer_media WHERE offer_id=? AND storefront=?",
                           (offer_id, storefront)).fetchone()
    return dict(row) if row else None


def save_app_meta(offer_id: str, storefront: str, app_json: str) -> None:
    """Store the app lookup result. An empty string is a real answer -- it
    records that we looked and found nothing, so we do not re-query on
    every pageview for an app that simply is not on that storefront."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO offer_media (offer_id, storefront, app_json, app_at) VALUES (?,?,?,?) "
            "ON CONFLICT(offer_id, storefront) DO UPDATE SET "
            "  app_json=excluded.app_json, app_at=excluded.app_at",
            (offer_id, storefront, app_json, int(time.time())))


def save_video(offer_id: str, storefront: str, video_id: str, video_json: str,
               pinned: bool = False) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO offer_media (offer_id, storefront, video_id, video_json, pinned, video_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(offer_id, storefront) DO UPDATE SET "
            "  video_id=excluded.video_id, video_json=excluded.video_json, "
            "  pinned=excluded.pinned, video_at=excluded.video_at",
            (offer_id, storefront, video_id, video_json, 1 if pinned else 0, int(time.time())))


def save_custom_art(offer_id: str, storefront: str, icon: str, hero: str) -> None:
    """Attach our own artwork to an offer. Survives every media refresh."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO offer_media (offer_id, storefront, custom_icon, custom_hero) "
            "VALUES (?,?,?,?) ON CONFLICT(offer_id, storefront) DO UPDATE SET "
            "  custom_icon=excluded.custom_icon, custom_hero=excluded.custom_hero",
            (offer_id, storefront, icon[:500], hero[:500]))


def clear_media(offer_id: str, storefront: str = "") -> int:
    with connect() as conn:
        if storefront:
            cur = conn.execute("DELETE FROM offer_media WHERE offer_id=? AND storefront=?",
                               (offer_id, storefront))
        else:
            cur = conn.execute("DELETE FROM offer_media WHERE offer_id=?", (offer_id,))
        return cur.rowcount


def list_media(limit: int = 200) -> list[dict]:
    with connect() as conn:
        return _rows(conn,
            "SELECT offer_id, storefront, video_id, pinned, app_at, video_at, "
            "  custom_icon, custom_hero, "
            "  CASE WHEN app_json='' THEN 0 ELSE 1 END AS has_app "
            "FROM offer_media ORDER BY app_at DESC, video_at DESC LIMIT ?", (limit,))


# ------------------------------------------------------------- campaigns
def upsert_campaign(campaign_id: str, *, label: str = "", pinned_offer_id: str = "",
                    target_country: str = "", target_device: str = "",
                    active: bool = True) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO campaigns (id, label, pinned_offer_id, target_country, "
            "target_device, active, created_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET label=excluded.label, "
            "  pinned_offer_id=excluded.pinned_offer_id, "
            "  target_country=excluded.target_country, "
            "  target_device=excluded.target_device, active=excluded.active",
            (campaign_id, label, pinned_offer_id, target_country.upper(),
             target_device.lower(), 1 if active else 0, int(time.time())))


def get_campaign(campaign_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    return dict(row) if row else None


def list_campaigns() -> list[dict]:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM campaigns ORDER BY created_at DESC")


def set_campaign_active(campaign_id: str, active: bool) -> bool:
    with connect() as conn:
        cur = conn.execute("UPDATE campaigns SET active=? WHERE id=?",
                           (1 if active else 0, campaign_id))
        return cur.rowcount > 0


def record_campaign_event(campaign: str, outcome: str, offer_id: str,
                          country: str, device: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO campaign_events (created_at, campaign, outcome, offer_id, "
            "country, device) VALUES (?,?,?,?,?,?)",
            (int(time.time()), campaign, outcome, offer_id, country, device))


def campaign_health(hours: int = 24) -> list[dict]:
    """Per-campaign fill outcomes, newest window first.

    `no_fill` is the number that matters: it means paid traffic landed and
    there was nothing to show them. Any non-zero value is money burning.
    """
    since = int(time.time()) - hours * 3600
    out = []
    with connect() as conn:
        campaigns = _rows(conn, "SELECT * FROM campaigns ORDER BY created_at DESC")
        for c in campaigns:
            counts = {r["outcome"]: r["n"] for r in conn.execute(
                "SELECT outcome, COUNT(*) n FROM campaign_events "
                "WHERE campaign=? AND created_at>=? GROUP BY outcome", (c["id"], since))}
            pinned = counts.get("pinned", 0)
            fallback = counts.get("fallback", 0)
            no_fill = counts.get("no_fill", 0)
            total = pinned + fallback + no_fill
            if not c["active"]:
                status = "paused"
            elif total == 0:
                status = "no_traffic"
            elif no_fill == total:
                status = "no_fill"          # nothing servable at all -> stop ads
            elif no_fill:
                status = "degraded"         # some landings wasted
            elif c["pinned_offer_id"] and fallback == total:
                status = "pinned_dead"      # pinned offer capped, alternates serving
            elif fallback:
                status = "fallback"
            else:
                status = "ok"
            out.append(dict(c, window_hours=hours, pinned=pinned, fallback=fallback,
                            no_fill=no_fill, landings=total, status=status,
                            no_fill_rate=round(no_fill / total, 4) if total else 0.0))
    return out


def stats(days: int = 30) -> dict:
    since = int(time.time()) - days * 86400
    with connect() as conn:
        totals = conn.execute(
            "SELECT (SELECT COUNT(*) FROM visits WHERE created_at>=?)      AS visits,"
            "       (SELECT COUNT(*) FROM clicks WHERE created_at>=?)      AS clicks,"
            "       (SELECT COUNT(*) FROM conversions WHERE created_at>=?) AS conversions,"
            "       (SELECT COALESCE(SUM(payout),0) FROM conversions WHERE created_at>=?) AS revenue",
            (since, since, since, since),
        ).fetchone()

        # Per-source funnel: the table that answers "which video makes money".
        by_source = _rows(conn,
            "SELECT s.source,"
            "  (SELECT COUNT(*) FROM visits v WHERE v.source=s.source AND v.created_at>=?) visits,"
            "  (SELECT COUNT(*) FROM clicks c WHERE c.source=s.source AND c.created_at>=?) clicks,"
            "  (SELECT COUNT(*) FROM conversions x WHERE x.source=s.source AND x.created_at>=?) conversions,"
            "  (SELECT COALESCE(SUM(payout),0) FROM conversions x WHERE x.source=s.source AND x.created_at>=?) revenue "
            "FROM (SELECT source FROM visits WHERE created_at>=? "
            "      UNION SELECT source FROM clicks WHERE created_at>=?) s "
            "GROUP BY s.source ORDER BY revenue DESC, clicks DESC LIMIT 50",
            (since,) * 6)

        by_offer = _rows(conn,
            "SELECT c.offer_id, MAX(c.offer_name) offer_name, COUNT(*) clicks,"
            "  (SELECT COUNT(*) FROM conversions x WHERE x.offer_id=c.offer_id AND x.created_at>=?) conversions,"
            "  (SELECT COALESCE(SUM(payout),0) FROM conversions x WHERE x.offer_id=c.offer_id AND x.created_at>=?) revenue "
            "FROM clicks c WHERE c.created_at>=? GROUP BY c.offer_id "
            "ORDER BY revenue DESC, clicks DESC LIMIT 50",
            (since, since, since))

        by_referrer = _rows(conn,
            "SELECT CASE WHEN referrer_host='' THEN '(direct / in-app)' ELSE referrer_host END host,"
            "       COUNT(*) visits FROM visits WHERE created_at>=? "
            "GROUP BY host ORDER BY visits DESC LIMIT 25", (since,))

        by_country = _rows(conn,
            "SELECT CASE WHEN country='' THEN '(unknown)' ELSE country END country, "
            "COUNT(*) visits FROM visits WHERE created_at>=? "
            "GROUP BY country ORDER BY visits DESC LIMIT 25", (since,))

        by_device = _rows(conn,
            "SELECT CASE WHEN device='' THEN 'unknown' ELSE device END device, COUNT(*) visits "
            "FROM visits WHERE created_at>=? GROUP BY device ORDER BY visits DESC", (since,))

        recent = _rows(conn,
            "SELECT created_at, offer_id, offer_name, source, payout FROM conversions "
            "WHERE created_at>=? ORDER BY created_at DESC LIMIT 20", (since,))

    v, c, x = totals["visits"], totals["clicks"], totals["conversions"]
    return {
        "window_days": days,
        "totals": {
            "visits": v, "clicks": c, "conversions": x,
            "revenue": round(totals["revenue"], 2),
            "click_through_rate": round(c / v, 4) if v else 0.0,
            "conversion_rate": round(x / c, 4) if c else 0.0,
            "revenue_per_visit": round(totals["revenue"] / v, 4) if v else 0.0,
        },
        "by_source": by_source,
        "by_offer": by_offer,
        "by_referrer": by_referrer,
        "by_country": by_country,
        "by_device": by_device,
        "recent_conversions": recent,
    }
