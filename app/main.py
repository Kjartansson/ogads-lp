"""FastAPI app: server-side OGAds proxy, visitor catalog, and funnel tracking.

Route map
    GET  /                    review index
    GET  /review/{slug}       the review article -- one video points here
    GET  /o/{slug}            legacy alias, 301s to /review/{slug}
    GET  /go/{offer_id}       tracked click, 302 to the attributed smartlink
    GET  /api/offers          JSON proxy (the API key never reaches a browser)
    GET  /api/offers/{id}     JSON, single offer
    GET  /postback            OGAds conversion callback (IP-allowlisted)
    GET  /healthz             liveness + upstream state
    GET  /admin               funnel dashboard        (token)
    GET  /admin/stats         same numbers as JSON    (token)
    GET  /admin/postback-url  the exact URL to paste into OGAds (token)
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
import uuid
from urllib.parse import quote
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import httpx

from . import campaigns, editorial, media as media_mod, store, visitor as visitor_mod
from . import config
from .config import BASE_DIR, settings
from .models import Offer
from .ogads import OgAdsError, client

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("ogads.app")

# OGAds sends conversion postbacks from this fixed set of hosts. A postback
# endpoint that trusts its query string is a free-money endpoint for anyone
# who guesses the URL, so callers are checked before anything is recorded.
OGADS_POSTBACK_IPS = {
    "50.18.215.132", "50.18.215.133", "50.18.215.134", "50.18.215.135",
    "107.21.28.235", "107.21.36.214", "107.23.2.46", "107.23.2.50",
    "54.64.15.176", "54.64.21.195",
    "54.94.179.76", "54.207.34.180", "54.207.36.218",
    "54.246.166.8", "54.246.166.9", "54.246.166.12", "54.246.166.17",
    "209.170.120.242", "209.170.120.243", "209.170.120.244",
}

SESSION_COOKIE = "sid"
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init()
    await client.startup()
    log.info("started | endpoint=%s mock=%s configured=%s debug=%s",
             settings.endpoint, settings.mock, settings.configured, settings.debug)
    yield
    await client.shutdown()


app = FastAPI(title="OGAds Offer Service", lifespan=lifespan, docs_url="/api/docs")
from .routes_creators import router as creators_router

app.include_router(creators_router)


@app.middleware("http")
async def custom_domain_routing(request: Request, call_next):
    """Serve a creator's page at the root of their own verified domain.

    Only the page itself and its click-out are remapped; /static, /admin and
    the API keep working on every host so the page can load its stylesheet
    and so an operator is never locked out by a DNS mistake.
    """
    host = (request.headers.get("host") or "").split(":")[0].lower()
    path = request.url.path
    if host and not path.startswith(("/static", "/admin", "/api", "/u/", "/healthz",
                                     "/postback", "/dashboard", "/login", "/signup")):
        own = config.base_url().split("://")[-1].split(":")[0].lower()
        if host not in (own, "localhost", "127.0.0.1", "testserver"):
            creator = store.get_creator_by_domain(host)
            if creator is not None:
                prefix = f"/u/{creator['username']}"
                request.scope["path"] = prefix if path == "/" else prefix + path
    return await call_next(request)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


# ------------------------------------------------------------------ helpers
def _session_id(request: Request) -> str:
    return request.cookies.get(SESSION_COOKIE) or uuid.uuid4().hex


def _set_session(response: Response, sid: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, sid, max_age=60 * 60 * 24 * 30,
        httponly=True, samesite="lax", secure=config.base_url().startswith("https"),
    )


def _source(request: Request) -> str:
    """Which video/campaign sent this visitor.

    Sanitised hard because this string is round-tripped through OGAds as
    aff_sub4 and later rendered in the dashboard.
    """
    raw = (request.query_params.get("v") or request.query_params.get("src")
           or request.query_params.get("utm_content") or "").strip()
    return "".join(c for c in raw if c.isalnum() or c in "-_")[:64]


def _track_visit(request: Request, sid: str, source: str, v) -> None:
    """Record first-touch attribution.

    Never allowed to fail a pageview. Analytics is a side effect of serving
    the visitor, not a precondition for it -- a locked or missing database
    should cost us a row, not the page.
    """
    try:
        store.record_visit(
        session_id=sid, ip=v.ip, user_agent=v.user_agent, device=v.device_class,
        landing_path=request.url.path, source=source,
        country=v.country,
            referrer=request.headers.get("referer", ""),
            utm={k: request.query_params.get(k, "")[:100] for k in UTM_KEYS},
        )
    except sqlite3.Error as exc:
        log.error("visit not recorded (%s): %s", type(exc).__name__, exc)


async def _load(request: Request, *, source: str = "", session_id: str = "",
                use_cache: bool = True) -> list[Offer]:
    """Offers for this visitor, targeted to their country and device.

    The targeting is done upstream: OGAds picks the offer set from the ip
    and user_agent we forward, which is why visitor.resolve() matters so
    much. The local filter below only drops anything that slips through, so
    a visitor is never shown an install they physically cannot complete.
    """
    v = visitor_mod.resolve(request)

    def _seen(offers):
        # Absence from a later response is how a capped or paused offer
        # announces itself -- there is no status field to poll.
        try:
            store.record_offers_seen(offers, v.country, v.device_class)
        except sqlite3.Error as exc:
            log.error("availability not recorded: %s", exc)

    offers = await client.get_offers(
        v.ip, v.user_agent, ctype=v.ctype,
        aff_sub4=source, aff_sub5=session_id, use_cache=use_cache, on_fetch=_seen,
    )
    tokens = v.device_tokens
    eligible = [o for o in offers if o.eligible_for(v.country, tokens)]
    if len(eligible) != len(offers):
        log.info("filtered %d/%d offers ineligible for country=%s device=%s",
                 len(offers) - len(eligible), len(offers), v.country or "?", v.device_class)
    return eligible


def _authed(request: Request) -> bool:
    token = request.query_params.get("token") or request.headers.get("x-admin-token", "")
    return bool(settings.admin_token) and token == settings.admin_token


# --------------------------------------------------------------------- JSON
@app.get("/api/offers")
async def api_offers(request: Request):
    source, sid = _source(request), _session_id(request)
    v = visitor_mod.resolve(request)
    _track_visit(request, sid, source, v)
    try:
        offers = await _load(request, source=source, session_id=sid)
    except OgAdsError as exc:
        return JSONResponse(
            {"success": False, "error": exc.visitor_message, "kind": exc.kind},
            status_code=503)

    payload = {
        "success": True, "count": len(offers), "device": v.device_class,
        "offers": [dict(o.public_dict(), url=f"/go/{o.id}" + (f"?v={source}" if source else ""))
                   for o in offers],
    }
    if settings.debug:
        payload["_debug"] = {"ip": v.ip, "simulated": v.simulated, "ctype": v.ctype}
    resp = JSONResponse(payload)
    _set_session(resp, sid)
    return resp


@app.get("/api/offers/{offer_id}")
async def api_offer(request: Request, offer_id: str):
    try:
        offers = await _load(request, session_id=_session_id(request))
    except OgAdsError as exc:
        return JSONResponse({"success": False, "error": exc.visitor_message}, status_code=503)
    for o in offers:
        if o.id == offer_id:
            return {"success": True, "offer": o.public_dict()}
    return JSONResponse({"success": False, "error": "Offer not available"}, status_code=404)


def _media_for(offer, visitor_country: str, background: BackgroundTasks):
    """Cached media now; a refresh queued if it is missing or stale.

    Deliberately never awaits the lookup inline -- a cold Apple/YouTube call
    would add hundreds of milliseconds to a page that paid traffic lands on.
    """
    if offer is None:
        return media_mod.Media()
    try:
        media, stale = media_mod.cached(offer, visitor_country)
    except sqlite3.Error as exc:
        log.error("media cache read failed: %s", exc)
        return media_mod.Media()
    if stale:
        background.add_task(_refresh_media_safely, offer, visitor_country)
    return media


async def _refresh_media_safely(offer, visitor_country: str) -> None:
    try:
        await media_mod.refresh(offer, visitor_country)
    except Exception:
        # Enrichment must never be able to break request handling.
        log.exception("media refresh failed for offer %s", offer.id)


# --------------------------------------------------------------------- HTML
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    source, sid = _source(request), _session_id(request)
    v = visitor_mod.resolve(request)
    _track_visit(request, sid, source, v)

    offers, error = [], None
    try:
        offers = await _load(request, source=source, session_id=sid)
    except OgAdsError as exc:
        error = exc.visitor_message
        log.warning("index render failed: %s %s", exc.kind, exc.message)

    # Lead with the offer that most people actually complete. EPC is the
    # network's own measure of that, so it doubles as a genuine "editor's
    # pick" signal rather than an arbitrary ordering.
    ranked = sorted(offers, key=lambda o: o.ranking_score, reverse=True)
    reviews = [editorial.build(o) for o in ranked]
    # Real freshness only: see store.freshness for why this can be empty.
    try:
        first_seen = store.freshness([o.id for o in ranked], v.country, v.device_class)
    except sqlite3.Error:
        first_seen = {}
    cutoff = int(time.time()) - 172800

    resp = templates.TemplateResponse(request, "index.html", {
        "site_name": settings.site_name, "reviews": reviews, "error": error,
        "source": source, "visitor": v, "debug": settings.debug,
        "disclosure": editorial.DISCLOSURE,
        "new_ids": {oid for oid, ts in first_seen.items() if ts >= cutoff},
    })
    _set_session(resp, sid)
    return resp


@app.get("/o/{slug}")
async def legacy_offer_alias(request: Request, slug: str):
    """Links printed in already-published videos must keep working."""
    q = request.url.query
    return RedirectResponse(f"/review/{slug}" + (f"?{q}" if q else ""), status_code=301)


@app.get("/review/{slug}", response_class=HTMLResponse)
async def review_page(request: Request, slug: str, background: BackgroundTasks):
    """The review article -- the destination for a single video.

    The slug's leading segment is the offer id and the rest is decoration,
    so an offer renamed upstream never breaks a link already baked into a
    published video caption.
    """
    offer_id = slug.split("-", 1)[0]
    source, sid = _source(request), _session_id(request)
    v = visitor_mod.resolve(request)
    _track_visit(request, sid, source, v)

    offers, error, review, substituted = [], None, None, False
    try:
        offers = await _load(request, source=source, session_id=sid)
        offer = next((o for o in offers if o.id == offer_id), None)
        if offer is None and offers:
            # The offer capped, paused, or never targeted this visitor. A
            # 404 wastes the click, so show the best thing we do have and
            # say plainly that it is not what was linked.
            offer = campaigns.best(offers)
            substituted = True
        if offer is not None:
            review = editorial.build(offer)
    except OgAdsError as exc:
        error = exc.visitor_message

    others = sorted((o for o in offers if o.id != offer_id),
                    key=lambda o: o.epc, reverse=True)[:4]
    resp = templates.TemplateResponse(request, "review.html", {
        "site_name": settings.site_name, "review": review, "error": error,
        "source": source, "alternatives": [editorial.build(o) for o in others],
        "visitor": v, "debug": settings.debug, "disclosure": editorial.DISCLOSURE,
        "substituted": substituted,
        "media": _media_for(review.offer if review else None, v.country, background),
    }, status_code=200 if review else 404)
    _set_session(resp, sid)
    return resp


# -------------------------------------------------------------------- click
@app.get("/go/{offer_id}")
async def go(request: Request, offer_id: str):
    """Record the click, then hand over an attributed smartlink.

    This fetch deliberately bypasses the cache. OGAds bakes the aff_sub
    values from the API REQUEST into the link it returns, so serving a
    cached list -- shared across visitors, carrying whichever aff_subs the
    first caller happened to use -- would credit every conversion to the
    wrong video. Clicks are far rarer than pageviews, so the extra upstream
    call is cheap. Wrong attribution is not.
    """
    source, sid = _source(request), _session_id(request)
    v = visitor_mod.resolve(request)
    try:
        offers = await _load(request, source=source, session_id=sid, use_cache=False)
    except OgAdsError as exc:
        log.warning("click failed offer=%s: %s %s", offer_id, exc.kind, exc.message)
        return RedirectResponse(f"/?err=1{'&v=' + source if source else ''}", status_code=302)

    offer = next((o for o in offers if o.id == offer_id), None)
    if offer is None:
        # Expired, paused, or never eligible for this geo/device. Send them
        # to the best live offer instead of throwing the click away.
        offer = campaigns.best(offers)
        if offer is None:
            return RedirectResponse(f"/?gone={offer_id}", status_code=302)
        log.info("click on unavailable offer %s substituted with %s", offer_id, offer.id)

    # Same rule as visits: a failed write must not strand the visitor on an
    # error page when we already have a good link to send them to.
    try:
        store.record_click(
            offer_id=offer.id, offer_name=offer.name_short, payout=offer.payout,
            ip=v.ip, user_agent=v.user_agent, device=v.device_class,
            source=source, session_id=sid)
    except sqlite3.Error as exc:
        log.error("click NOT recorded for offer=%s source=%s (%s): %s",
                  offer.id, source, type(exc).__name__, exc)

    resp = RedirectResponse(offer.link, status_code=302)
    _set_session(resp, sid)
    return resp


@app.get("/lp/{campaign_id}", response_class=HTMLResponse)
async def ad_landing(request: Request, campaign_id: str, background: BackgroundTasks):
    """Landing page for a paid ad.

    Point every ad here, never at /review/{offer}. The campaign resolves to
    a live offer on each request, so an offer capping mid-flight costs one
    fallback instead of a stream of dead landings billed at CPC.
    """
    campaign_id = "".join(c for c in campaign_id if c.isalnum() or c in "-_")[:64]
    sid = _session_id(request)
    v = visitor_mod.resolve(request)
    source = _source(request) or campaign_id
    _track_visit(request, sid, source, v)

    offers, error = [], None
    try:
        offers = await _load(request, source=source, session_id=sid)
    except OgAdsError as exc:
        error = exc.visitor_message
        log.warning("ad landing %s upstream failure: %s", campaign_id, exc.message)

    campaign = store.get_campaign(campaign_id)
    res = campaigns.resolve(campaign, campaign_id, offers)
    try:
        store.record_campaign_event(campaign_id, res.outcome,
                                    res.offer.id if res.offer else "",
                                    v.country, v.device_class)
    except sqlite3.Error as exc:
        log.error("campaign event not recorded: %s", exc)

    if res.wasted:
        log.warning("AD SPEND WASTED: campaign=%s country=%s device=%s -- no fill",
                    campaign_id, v.country or "?", v.device_class)

    others = sorted((o for o in offers if not res.offer or o.id != res.offer.id),
                    key=lambda o: o.ranking_score, reverse=True)[:4]
    resp = templates.TemplateResponse(request, "review.html", {
        "site_name": settings.site_name,
        "review": editorial.build(res.offer) if res.offer else None,
        "error": error, "source": source,
        "alternatives": [editorial.build(o) for o in others],
        "visitor": v, "debug": settings.debug, "disclosure": editorial.DISCLOSURE,
        "substituted": False, "campaign": res,
        "media": _media_for(res.offer, v.country, background),
    }, status_code=200 if res.offer else 503)
    _set_session(resp, sid)
    return resp


# ----------------------------------------------------------------- postback
@app.get("/postback")
async def postback(request: Request):
    """Conversion callback from OGAds.

    Macro names follow members.ogads.com/tools/postback-url exactly -- they
    are case sensitive there, and any macro OGAds does not recognise is
    stripped from the URL before it ever fires.
    """
    remote = request.client.host if request.client else ""
    caller = visitor_mod._from_headers(request) or remote

    allowed = OGADS_POSTBACK_IPS | set(settings.postback_extra_ips)
    ip_ok = caller in allowed or remote in allowed

    # TRUSTED_PROXY_HOPS=0 declares "nothing sits in front of me". An
    # X-Forwarded-For arriving anyway is either a misconfiguration or a
    # forgery attempt, and in both cases the socket peer can no longer be
    # trusted to identify the caller -- so IP authorisation is withdrawn and
    # only the shared secret will do. (uvicorn compounds this by rewriting
    # request.client.host from that same header unless it is started with
    # --no-proxy-headers, which run.sh does.)
    if not settings.trusted_proxy_hops and "x-forwarded-for" in request.headers:
        if ip_ok:
            log.warning("postback presented X-Forwarded-For with no trusted proxy "
                        "configured; ignoring IP authorisation (from %s)", remote)
        ip_ok = False
    secret_ok = bool(settings.postback_secret) and \
        request.query_params.get("secret") == settings.postback_secret
    # Loopback is trusted only in DEBUG, so the endpoint can be exercised
    # locally with curl without punching a hole in a deployed instance.
    local_ok = settings.debug and remote in ("127.0.0.1", "::1")

    if not (ip_ok or secret_ok or local_ok):
        log.warning("postback REJECTED from %s | %s", caller, dict(request.query_params))
        return JSONResponse({"success": False, "error": "unauthorized"}, status_code=403)

    q = request.query_params
    offer_id = (q.get("offer_id") or "").strip()
    if not offer_id:
        return JSONResponse({"success": False, "error": "offer_id required"}, status_code=400)

    # Every conversion on our account reports our affiliate id. A mismatch
    # means the callback is either forged or belongs to somebody else's
    # account -- crediting it would put revenue in our books that is not
    # ours. The macro is only checked when present, since OGAds strips
    # macros it does not recognise and an operator may omit it.
    reported_aff = (q.get("affiliate_id") or "").strip()
    if settings.affiliate_id and reported_aff and reported_aff != settings.affiliate_id:
        log.warning("postback REJECTED: affiliate_id %s is not ours (%s) | %s",
                    reported_aff, settings.affiliate_id, dict(q))
        return JSONResponse({"success": False, "error": "affiliate mismatch"}, status_code=403)

    try:
        payout = float(q.get("payout") or 0)
    except ValueError:
        payout = 0.0

    fresh = store.record_conversion(
        offer_id=offer_id,
        offer_name=(q.get("offer_name") or "").strip(),
        affiliate_id=(q.get("affiliate_id") or "").strip(),
        ip=(q.get("session_ip") or q.get("ip") or "").strip(),
        # aff_sub4 is what the Offer API lets us set, so it wins. {source}
        # only carries a value on tracking-link integrations.
        source=(q.get("aff_sub4") or q.get("source") or "").strip(),
        session_id=(q.get("aff_sub5") or "").strip(),
        payout=payout,
        converted_at=(q.get("datetime") or q.get("session_timestamp") or "").strip(),
        remote_ip=caller, raw_query=str(q),
    )
    log.info("postback offer=%s source=%s payout=%.2f fresh=%s",
             offer_id, q.get("aff_sub4"), payout, fresh)
    return {"success": True, "duplicate": not fresh}


# --------------------------------------------------------------------- ops
@app.get("/healthz")
async def healthz():
    return {"status": "ok", "ogads": client.health()}


@app.get("/admin/stats")
async def admin_stats(request: Request, days: int = 30):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    return {"stats": store.stats(days=max(1, min(days, 365))), "ogads": client.health()}


def _postback_url() -> str:
    macros = (
        "offer_id={offer_id}&offer_name={offer_name}&affiliate_id={affiliate_id}"
        "&payout={payout}&session_ip={session_ip}&datetime={datetime}"
        "&aff_sub4={aff_sub4}&aff_sub5={aff_sub5}&source={source}"
    )
    url = f"{config.base_url()}/postback?{macros}"
    if settings.postback_secret:
        url += f"&secret={settings.postback_secret}"
    return url


_LOGIN_HINT = (
    "<p style='font:16px system-ui;padding:2rem'>Unauthorized. Append "
    "<code>?token=&lt;ADMIN_TOKEN&gt;</code> — the value is in your <code>.env</code>.</p>"
)


@app.get("/admin/postback-url")
async def admin_postback_url(request: Request):
    """The exact string to paste into the OGAds postback settings."""
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    return {
        "postback_url": _postback_url(),
        "note": "Paste into https://members.ogads.com/tools/postback-url. Macros are "
                "case sensitive and unrecognised ones are stripped. Requires a "
                "publicly reachable SITE_BASE_URL -- localhost will never receive one.",
    }


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, days: int = 30, hours: int = 24):
    if not _authed(request):
        return HTMLResponse(_LOGIN_HINT, status_code=403)
    return _render_admin(request, days, hours,
                         flash=request.query_params.get("flash", "")[:300])


def _render_admin(request: Request, days: int, hours: int, flash: str = ""):
    token = request.query_params.get("token", "")
    return templates.TemplateResponse(request, "admin.html", {
        "site_name": settings.site_name,
        "stats": store.stats(days=max(1, min(days, 365))),
        "campaign_health": store.campaign_health(hours=max(1, min(hours, 720))),
        "availability": store.offer_availability(),
        "ogads": client.health(), "days": days, "hours": hours,
        "postback_url": _postback_url(), "flash": flash,
        "media_rows": store.list_media(60),
        "youtube_ready": bool(settings.youtube_api_key),
        "site_base": config.base_url(),
        "token": token, "debug": settings.debug, "visitor": None, "source": "",
    })


@app.post("/admin/campaigns")
async def admin_save_campaign(
    request: Request,
    campaign_id: str = Form(...),
    label: str = Form(""),
    pinned_offer_id: str = Form(""),
    target_country: str = Form(""),
    target_device: str = Form(""),
):
    """Create or update a campaign. Idempotent on campaign_id."""
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    cid = "".join(c for c in campaign_id.strip() if c.isalnum() or c in "-_")[:64]
    if not cid:
        return _redirect_admin(request, "Campaign id must contain letters or digits.")
    store.upsert_campaign(
        cid, label=label.strip()[:120],
        pinned_offer_id="".join(ch for ch in pinned_offer_id.strip() if ch.isdigit())[:16],
        target_country=target_country.strip()[:2], target_device=target_device.strip()[:12])
    return _redirect_admin(request, f"Saved campaign “{cid}”. Ad URL: {config.base_url()}/lp/{cid}")


@app.post("/admin/campaigns/toggle")
async def admin_toggle_campaign(request: Request, campaign_id: str = Form(...),
                                active: str = Form("0")):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    on = active.strip() in ("1", "true", "on", "yes")
    ok = store.set_campaign_active(campaign_id.strip(), on)
    msg = (f"Campaign “{campaign_id}” {'resumed' if on else 'paused'}."
           if ok else f"No campaign called “{campaign_id}”.")
    if not on:
        msg += " Remember to pause the ad set too — this only stops us counting it as live."
    return _redirect_admin(request, msg)


def _redirect_admin(request: Request, flash: str):
    token = request.query_params.get("token") or request.headers.get("x-admin-token", "")
    return RedirectResponse(
        f"/admin?token={quote(token)}&flash={quote(flash)}", status_code=303)


@app.post("/admin/settings")
async def admin_settings(
    request: Request,
    api_key: str = Form(""),
    endpoint: str = Form(""),
    site_base_url: str = Form(""),
):
    """Update credentials and the landing domain from the panel.

    The OGAds pair is validated with a real API call BEFORE it is written.
    Saving an unverified key would take the whole site down silently: every
    page would render the generic "offers unavailable" notice and nothing
    would say why.
    """
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)

    messages, changed_upstream = [], False

    new_base = site_base_url.strip().rstrip("/")
    if new_base:
        if not re.match(r"^https?://[A-Za-z0-9.\-]+(:\d+)?$", new_base):
            return _redirect_admin(request,
                "Landing domain must look like https://example.com — no path, no trailing slash.")
        config.set_env_value("SITE_BASE_URL", new_base)
        messages.append(f"Landing domain set to {new_base}.")
        if new_base.startswith("http://") and "localhost" not in new_base \
                and "127.0.0.1" not in new_base:
            messages.append("Warning: that is plain HTTP. Session cookies will not be "
                            "marked Secure and OGAds postbacks will be sent unencrypted.")

    candidate_key = api_key.strip() or config.api_key()
    candidate_endpoint = endpoint.strip() or config.endpoint()
    if (api_key.strip() and api_key.strip() != config.api_key()) or \
       (endpoint.strip() and endpoint.strip() != config.endpoint()):
        ok, detail = await _verify_credentials(candidate_key, candidate_endpoint)
        if not ok:
            return _redirect_admin(request, f"Not saved — {detail} Existing credentials left in place.")
        if api_key.strip():
            config.set_env_value("OGADS_API_KEY", candidate_key)
            messages.append(f"API key updated ({config.mask(candidate_key)}).")
        if endpoint.strip():
            config.set_env_value("OGADS_ENDPOINT", candidate_endpoint)
            messages.append(f"Endpoint set to {candidate_endpoint}.")
        messages.append(detail)
        changed_upstream = True

    if changed_upstream:
        # Cached offer lists were fetched with the old credentials.
        client.cache.clear()
        client.last_error = None
        messages.append("Offer cache cleared.")

    return _redirect_admin(request, " ".join(messages) or "Nothing to change.")


async def _verify_credentials(key: str, endpoint_url: str) -> tuple[bool, str]:
    """Try one real request. Returns (ok, human explanation)."""
    if not key:
        return False, "no API key given."
    probe_ip = settings.test_ip or "23.45.21.76"
    ua = settings.test_ua
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.timeout)) as http:
            resp = await http.get(endpoint_url,
                                  params={"ip": probe_ip, "user_agent": ua, "ctype": "1"},
                                  headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        return False, f"could not reach {endpoint_url} ({type(exc).__name__})."
    if resp.status_code in (401, 403):
        return False, "OGAds rejected that key (HTTP %d)." % resp.status_code
    if resp.status_code >= 400:
        return False, f"endpoint returned HTTP {resp.status_code}."
    try:
        payload = resp.json()
    except ValueError:
        return False, "endpoint did not return JSON — check the URL."
    if not payload.get("success"):
        return False, f"OGAds said: {payload.get('error') or 'success=false'}."
    n = len(payload.get("offers") or [])
    return True, f"Verified — the API returned {n} offer(s) for the test IP."


@app.post("/admin/media/pin")
async def admin_pin_video(request: Request, offer_id: str = Form(...),
                          storefront: str = Form("us"), video_url: str = Form(...)):
    """Attach a specific gameplay video to an offer.

    Auto-discovery is a guess; this is the override. A pinned video is never
    replaced automatically, because a human looked at it and a wrong video
    on a review page is worse than no video.
    """
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    ok, msg = await media_mod.pin(offer_id.strip(), storefront.strip().lower()[:2],
                                  video_url.strip())
    return _redirect_admin(request, msg)


@app.post("/admin/media/art")
async def admin_set_art(request: Request, offer_id: str = Form(...),
                        storefront: str = Form("us"), custom_icon: str = Form(""),
                        custom_hero: str = Form("")):
    """Attach your own artwork to an offer.

    Outranks the fetched icon and survives every media refresh, so a designed
    hero or infographic is not quietly replaced the next time the store
    listing is re-read.
    """
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    for url in (custom_icon.strip(), custom_hero.strip()):
        if url and not re.match(r"^(https://|/static/)", url):
            return _redirect_admin(request,
                "Artwork must be an https:// URL or a /static/ path.")
    store.save_custom_art(offer_id.strip(), storefront.strip().lower()[:2],
                          custom_icon.strip(), custom_hero.strip())
    return _redirect_admin(request, f"Artwork saved for offer {offer_id}.")


@app.post("/admin/media/clear")
async def admin_clear_media(request: Request, offer_id: str = Form(...),
                            storefront: str = Form("")):
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    n = store.clear_media(offer_id.strip(), storefront.strip().lower()[:2])
    return _redirect_admin(request,
        f"Cleared {n} cached media row(s) for offer {offer_id}. It will be "
        f"looked up again on the next request.")


@app.get("/admin/health")
async def admin_health(request: Request, hours: int = 24):
    """Machine-readable campaign health, for cron and alerting."""
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    health = store.campaign_health(hours=max(1, min(hours, 720)))
    stop = [c["id"] for c in health if c["status"] in ("no_fill", "degraded")]
    return {
        "window_hours": hours,
        "campaigns": health,
        "stop_spending_on": stop,
        "ok": not stop,
    }
