"""Signup, login, dashboard, and the public creator link page."""
from __future__ import annotations

import logging
import sqlite3
import uuid

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import (accounts, config, creators, editorial, media as media_mod,
               store, visitor as visitor_mod)
from .config import BASE_DIR, settings
from .ogads import OgAdsError, client

log = logging.getLogger("ogads.creator_routes")
router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

CREATOR_COOKIE = "creator_session"
SESSION_COOKIE = "sid"


# ------------------------------------------------------------------ helpers
def current_creator(request: Request) -> dict | None:
    creator_id = accounts.read_session(request.cookies.get(CREATOR_COOKIE, ""))
    if not creator_id:
        return None
    creator = store.get_creator(int(creator_id))
    if creator and creator["suspended"]:
        return None
    return creator


def _visitor_session(request: Request) -> str:
    return request.cookies.get(SESSION_COOKIE) or uuid.uuid4().hex


def _set_visitor_session(response: Response, sid: str) -> None:
    response.set_cookie(SESSION_COOKIE, sid, max_age=60 * 60 * 24 * 30,
                        httponly=True, samesite="lax",
                        secure=config.base_url().startswith("https"))


def _login_redirect(creator_id: int, target: str = "/dashboard") -> RedirectResponse:
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(CREATOR_COOKIE, accounts.issue_session(creator_id),
                    max_age=accounts.SESSION_MAX_AGE, httponly=True, samesite="lax",
                    secure=config.base_url().startswith("https"))
    return resp


def _page(request: Request, template: str, ctx: dict, status: int = 200):
    base = {"site_name": settings.site_name, "debug": settings.debug,
            "visitor": None, "source": "", "disclosure": ""}
    return templates.TemplateResponse(request, template, {**base, **ctx}, status_code=status)


async def _offers_for(request: Request, api_key: str, aff_sub4: str, aff_sub5: str,
                      use_cache: bool = True):
    v = visitor_mod.resolve(request)
    offers = await client.get_offers(
        v.ip, v.user_agent, ctype=v.ctype, aff_sub4=aff_sub4, aff_sub5=aff_sub5,
        use_cache=use_cache, api_key=api_key)
    tokens = v.device_tokens
    return v, [o for o in offers if o.eligible_for(v.country, tokens)]


# ------------------------------------------------------------------- signup
@router.get("/signup", response_class=HTMLResponse)
async def signup_form(request: Request):
    if current_creator(request):
        return RedirectResponse("/dashboard", status_code=303)
    return _page(request, "signup.html", {
        "share": settings.default_platform_share,
        "disclosure_text": creators.DISCLOSURE_SIGNUP.format(
            share=settings.default_platform_share),
        "errors": [], "values": {}})


@router.post("/signup")
async def signup(request: Request, username: str = Form(...), email: str = Form(...),
                 password: str = Form(...), api_key: str = Form(""),
                 accept: str = Form("")):
    values = {"username": username.strip().lower(), "email": email.strip().lower()}
    errors = [e for e in (
        accounts.username_problem(values["username"]),
        accounts.email_problem(values["email"]),
        accounts.password_problem(password),
    ) if e]
    # The traffic share is the whole deal; it must be acknowledged, not buried.
    if accept.strip().lower() not in ("1", "on", "yes", "true"):
        errors.append("Please confirm you understand the traffic share before continuing.")

    if errors:
        return _page(request, "signup.html", {
            "share": settings.default_platform_share,
            "disclosure_text": creators.DISCLOSURE_SIGNUP.format(
                share=settings.default_platform_share),
            "errors": errors, "values": values}, status=400)

    creator_id = store.create_creator(
        username=values["username"], email=values["email"],
        password_hash=accounts.hash_password(password),
        platform_share=settings.default_platform_share)
    if api_key.strip():
        store.update_creator(creator_id, api_key_enc=accounts.encrypt_key(api_key.strip()))
    log.info("creator signed up: %s (id %d)", values["username"], creator_id)
    return _login_redirect(creator_id)


# -------------------------------------------------------------------- login
@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if current_creator(request):
        return RedirectResponse("/dashboard", status_code=303)
    return _page(request, "login.html", {"error": ""})


@router.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    ident = email.strip().lower()
    client_ip = (request.client.host if request.client else "") or "?"
    # Throttled on both axes: one account being ground down, and one source
    # spraying many accounts.
    if accounts.too_many_attempts(ident) or accounts.too_many_attempts(f"ip:{client_ip}"):
        return _page(request, "login.html", {
            "error": "Too many attempts. Wait a few minutes and try again."}, status=429)

    creator = store.get_creator_by_email(ident)
    ok = creator and accounts.verify_password(password, creator["password_hash"])
    if not ok:
        accounts.record_attempt(ident)
        accounts.record_attempt(f"ip:{client_ip}")
        # One message for both cases, so the form cannot be used to discover
        # which email addresses have accounts.
        return _page(request, "login.html",
                     {"error": "Email or password is incorrect."}, status=401)
    if creator["suspended"]:
        return _page(request, "login.html", {
            "error": f"This account is suspended. {creator['suspend_reason']}"}, status=403)

    accounts.clear_attempts(ident)
    accounts.clear_attempts(f"ip:{client_ip}")
    store.touch_login(creator["id"])
    return _login_redirect(creator["id"])


@router.post("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(CREATOR_COOKIE)
    return resp


# ---------------------------------------------------------------- dashboard
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, flash: str = ""):
    creator = current_creator(request)
    if not creator:
        return RedirectResponse("/login", status_code=303)

    api_key = accounts.decrypt_key(creator["api_key_enc"])
    available, error = [], ""
    if api_key:
        try:
            _, available = await _offers_for(request, api_key, creator["username"], "")
        except OgAdsError as exc:
            error = (f"We could not load offers from your OGAds account ({exc.kind}). "
                     f"Check the API key below.")
    else:
        error = "Add your OGAds API key below to choose offers for your page."

    chosen = store.get_creator_links(creator["id"])
    chosen_ids = [c["offer_id"] for c in chosen]
    titles = {c["offer_id"]: c["title"] for c in chosen}
    return _page(request, "dashboard.html", {
        "creator": creator, "available": available, "chosen_ids": chosen_ids,
        "titles": titles, "error": error, "flash": flash[:300],
        "stats": store.creator_stats(creator["id"]),
        "has_key": bool(api_key),
        "page_url": f"{config.base_url()}/u/{creator['username']}",
        "min_links": creators.MIN_LINKS, "max_links": creators.MAX_LINKS,
        "templates": creators.TEMPLATES,
        "server_ips": ", ".join(settings.server_ips),
        "csrf": accounts.csrf_token(request.cookies.get(CREATOR_COOKIE, "")),
    })


@router.post("/dashboard/links")
async def save_links(request: Request):
    creator = current_creator(request)
    if not creator:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    if not accounts.csrf_ok(request.cookies.get(CREATOR_COOKIE, ""), form.get("csrf", "")):
        return RedirectResponse("/dashboard?flash=Session+expired,+please+try+again.",
                                status_code=303)

    picked = [v for v in form.getlist("offer_id")][:creators.MAX_LINKS]
    entries = [(oid, (form.get(f"title_{oid}") or "").strip()) for oid in picked]
    store.set_creator_links(creator["id"], entries)
    note = ""
    if len(entries) < creators.MIN_LINKS:
        note = (f" Your page works, but {creators.MIN_LINKS}-{creators.MAX_LINKS} "
                f"offers is the sweet spot.")
    return RedirectResponse(f"/dashboard?flash=Saved+{len(entries)}+offers.{note.replace(' ', '+')}",
                            status_code=303)


@router.post("/dashboard/settings")
async def save_settings(request: Request, display_name: str = Form(""),
                        bio: str = Form(""), api_key: str = Form(""),
                        csrf: str = Form("")):
    creator = current_creator(request)
    if not creator:
        return RedirectResponse("/login", status_code=303)
    if not accounts.csrf_ok(request.cookies.get(CREATOR_COOKIE, ""), csrf):
        return RedirectResponse("/dashboard?flash=Session+expired.", status_code=303)

    fields = {"display_name": display_name.strip()[:60], "bio": bio.strip()[:280]}
    message = "Profile saved."
    if api_key.strip():
        # Verified before storing: a bad key means a blank page for every
        # visitor they send, with nothing on screen explaining why.
        from .main import _verify_credentials
        ok, detail = await _verify_credentials(api_key.strip(), config.endpoint())
        if not ok:
            return RedirectResponse(
                f"/dashboard?flash=API+key+not+saved+%E2%80%94+{detail.replace(' ', '+')}",
                status_code=303)
        fields["api_key_enc"] = accounts.encrypt_key(api_key.strip())
        message = f"Profile saved. {detail}"
    store.update_creator(creator["id"], **fields)
    return RedirectResponse(f"/dashboard?flash={message.replace(' ', '+')}", status_code=303)


@router.post("/dashboard/template")
async def choose_template(request: Request, page_template: str = Form(...),
                          csrf: str = Form("")):
    creator = current_creator(request)
    if not creator:
        return RedirectResponse("/login", status_code=303)
    if not accounts.csrf_ok(request.cookies.get(CREATOR_COOKIE, ""), csrf):
        return RedirectResponse("/dashboard?flash=Session+expired.", status_code=303)
    key = page_template if page_template in creators.TEMPLATES else creators.DEFAULT_TEMPLATE
    store.update_creator(creator["id"], page_template=key)
    return RedirectResponse(
        f"/dashboard?flash=Layout+set+to+{creators.TEMPLATES[key]['name'].replace(' ', '+')}.",
        status_code=303)


@router.post("/dashboard/domain")
async def set_domain(request: Request, custom_domain: str = Form(""), csrf: str = Form("")):
    """Attach or re-check a vanity domain.

    Nothing is served on a domain until it demonstrably points at this
    server -- otherwise anyone could type someone else's hostname here and
    have us answer for it.
    """
    creator = current_creator(request)
    if not creator:
        return RedirectResponse("/login", status_code=303)
    if not accounts.csrf_ok(request.cookies.get(CREATOR_COOKIE, ""), csrf):
        return RedirectResponse("/dashboard?flash=Session+expired.", status_code=303)

    host = creators.normalise_domain(custom_domain)
    if not host:
        store.update_creator(creator["id"], custom_domain="", domain_verified=0)
        return RedirectResponse("/dashboard?flash=Custom+domain+removed.", status_code=303)

    problem = creators.domain_problem(host)
    if problem:
        return RedirectResponse(f"/dashboard?flash={problem.replace(' ', '+')}", status_code=303)
    if store.domain_taken(host, exclude_creator_id=creator["id"]):
        return RedirectResponse("/dashboard?flash=That+domain+is+already+in+use.",
                                status_code=303)

    ok, detail = creators.verify_domain(host, set(settings.server_ips))
    store.update_creator(creator["id"], custom_domain=host, domain_verified=1 if ok else 0)
    prefix = "Connected." if ok else "Saved, but not live yet:"
    return RedirectResponse(
        f"/dashboard?flash={(prefix + ' ' + detail).replace(' ', '+')}", status_code=303)


# ------------------------------------------------------- public link page
@router.get("/u/{username}", response_class=HTMLResponse)
async def creator_page(request: Request, username: str):
    creator = store.get_creator_by_username(username)
    if not creator or creator["suspended"]:
        return _page(request, "creator_missing.html", {"username": username}, status=404)

    sid = _visitor_session(request)
    to_platform = creators.serves_platform(sid, creator["username"], creator["platform_share"])
    creator_key = accounts.decrypt_key(creator["api_key_enc"])

    # The platform slice runs on the platform's own key; the creator slice on
    # theirs. Never mixed -- that is what keeps each side's conversions
    # landing in the right account.
    api_key = "" if to_platform else creator_key
    aff_sub4 = "platform-share" if to_platform else creator["username"]

    v, offers, error = visitor_mod.resolve(request), [], ""
    if not to_platform and not creator_key:
        error = "This page is not finished yet."
    else:
        try:
            v, offers = await _offers_for(request, api_key, aff_sub4, sid)
        except OgAdsError as exc:
            error = exc.visitor_message
            log.warning("creator page %s upstream %s: %s", username, exc.kind, exc.message)

    if to_platform:
        picks = creators.platform_picks(offers)
    else:
        picks = creators.ordered_links(store.get_creator_links(creator["id"]), offers)

    try:
        store.record_creator_visit(
            creator_id=creator["id"], session_id=sid,
            served=creators.SERVED_PLATFORM if to_platform else creators.SERVED_CREATOR,
            country=v.country, device=v.device_class)
    except sqlite3.Error as exc:
        log.error("creator visit not recorded: %s", exc)

    layout = creators.template_for(creator["page_template"])
    if layout["template"] == "creator_spotlight.html":
        picks = picks[:1]
    reviews = [(editorial.build(o), title) for o, title in picks]

    # Every card gets its artwork, not just the first -- a list of lettered
    # tiles is what made these pages look like placeholders.
    media_by_offer = {}
    for offer, _ in picks:
        try:
            media_by_offer[offer.id] = media_mod.cached(offer, v.country)[0]
        except sqlite3.Error:
            pass

    resp = _page(request, layout["template"], {
        "creator": creator, "picks": picks, "reviews": reviews, "error": error,
        "visitor": v, "served_platform": to_platform, "site_name": settings.site_name,
        "media_by_offer": media_by_offer,
        "media": media_by_offer.get(picks[0][0].id) if picks else None,
    })
    _set_visitor_session(resp, sid)
    return resp


@router.get("/u/{username}/go/{offer_id}")
async def creator_click(request: Request, username: str, offer_id: str):
    creator = store.get_creator_by_username(username)
    if not creator or creator["suspended"]:
        return RedirectResponse("/", status_code=302)

    sid = _visitor_session(request)
    to_platform = creators.serves_platform(sid, creator["username"], creator["platform_share"])
    api_key = "" if to_platform else accounts.decrypt_key(creator["api_key_enc"])
    aff_sub4 = "platform-share" if to_platform else creator["username"]

    try:
        # Uncached, so the smartlink carries this visitor's own aff_subs --
        # the same reason /go does it. See main.go for the full explanation.
        v, offers = await _offers_for(request, api_key, aff_sub4, sid, use_cache=False)
    except OgAdsError:
        return RedirectResponse(f"/u/{username}", status_code=302)

    offer = next((o for o in offers if o.id == offer_id), None)
    if offer is None:
        return RedirectResponse(f"/u/{username}", status_code=302)

    # A creator's click must never mint a link on OUR account: that would pay
    # us for their traffic, which is the exact failure this whole design
    # exists to avoid. It happens if they paste the platform key, or if key
    # handling regresses. Loud, because it is silently profitable for us and
    # therefore the kind of bug nobody notices.
    if (not to_platform and settings.affiliate_id
            and f"aff_id={settings.affiliate_id}" in offer.link):
        log.error("KEY LEAK: creator %s click on offer %s minted a link on the "
                  "PLATFORM account (aff_id=%s). Their key is wrong or missing.",
                  username, offer.id, settings.affiliate_id)

    try:
        store.record_click(
            offer_id=offer.id, offer_name=offer.name_short, payout=offer.payout,
            ip=v.ip, user_agent=v.user_agent, device=v.device_class,
            source=aff_sub4, session_id=sid, creator_id=creator["id"],
            served=creators.SERVED_PLATFORM if to_platform else creators.SERVED_CREATOR)
    except sqlite3.Error as exc:
        log.error("creator click not recorded: %s", exc)

    resp = RedirectResponse(offer.link, status_code=302)
    _set_visitor_session(resp, sid)
    return resp
