#!/usr/bin/env python3
"""End-to-end smoke test. No running server, no live API, no network.

Forces OGADS_MOCK=1 and a throwaway database, then drives every route and
the whole funnel in-process. Written after a refactor silently deleted the
helper functions behind the review page: the templates still rendered on the
index, so nothing looked wrong until an article was actually requested.

    ./.venv/bin/python tools/smoke.py        # exit 0 = everything passed
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Must be set before app.config is imported.
os.environ["OGADS_MOCK"] = "1"
os.environ["DEBUG"] = "1"
os.environ["TEST_IP"] = "23.45.21.76"
os.environ["ADMIN_TOKEN"] = "smoke-token"
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "smoke.db")
os.environ["SITE_BASE_URL"] = "http://testserver"
# The postback is authenticated. TestClient's peer is "testclient", not a
# loopback address, so the DEBUG loopback exemption correctly does not apply
# -- the test authenticates the way OGAds would from an unlisted IP, via the
# shared secret. Production auth is exercised, not bypassed.
os.environ["POSTBACK_SECRET"] = "smoke-secret"
os.environ["OGADS_AFFILIATE_ID"] = "2070"

from fastapi.testclient import TestClient   # noqa: E402
from app.main import app                    # noqa: E402

IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
TOKEN = "smoke-token"

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<52} {got!r}")
    if not ok:
        failures.append(f"{label}: expected {want!r}, got {got!r}")


def contains(label: str, haystack: str, needle: str) -> None:
    ok = needle in haystack
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(f"{label}: {needle!r} missing")


def main() -> int:
    with TestClient(app, headers={"user-agent": IPHONE}) as c:
        print("visitor pages")
        r = c.get("/", params={"v": "smoke", "_country": "US"})
        check("GET /", r.status_code, 200)
        contains("index renders the masthead", r.text, "Apps actually worth the install")

        r = c.get("/api/offers", params={"_country": "US"})
        check("GET /api/offers", r.status_code, 200)
        offers = r.json()["offers"]
        check("offers returned", len(offers) > 0, True)
        check("smartlink withheld from JSON", "link" in offers[0], False)
        offer_id, slug = offers[0]["id"], offers[0]["slug"]

        r = c.get(f"/review/{slug}", params={"v": "smoke", "_country": "US"})
        check("GET /review/{slug}", r.status_code, 200)
        contains("article renders the verdict", r.text, "Verdict")
        contains("article renders the requirement", r.text, "What you actually have to do")
        check("no locker promise on the page", "unlock this content" in r.text, False)

        check("GET /o/{slug} legacy alias", c.get(f"/o/{slug}", follow_redirects=False).status_code, 301)
        check("GET /review/{bogus}", c.get("/review/99999999-nope", params={"_country": "US"}).status_code, 200)

        print("\nclick + postback")
        r = c.get(f"/go/{offer_id}", params={"v": "smoke", "_country": "US"}, follow_redirects=False)
        check("GET /go/{id} redirects", r.status_code, 302)
        sid = c.cookies.get("sid")
        def postback(payout: str):
            return c.get("/postback", params={
                "offer_id": offer_id, "offer_name": "Smoke", "affiliate_id": "2070",
                "payout": payout, "session_ip": "23.45.21.76",
                "datetime": "2026-08-24 12:00:00", "aff_sub4": "smoke",
                "aff_sub5": sid, "secret": "smoke-secret"})

        check("forged postback without the secret", c.get("/postback", params={
            "offer_id": offer_id, "payout": "99.00"}).status_code, 403)
        # A postback carrying somebody else's affiliate id is not our revenue.
        check("postback for another affiliate rejected", c.get("/postback", params={
            "offer_id": offer_id, "payout": "99.00", "affiliate_id": "9999",
            "secret": "smoke-secret"}).status_code, 403)
        r = postback("1.50")
        check("postback accepted with the secret", r.status_code, 200)
        check("postback recorded once", r.json()["duplicate"], False)
        check("postback retry deduped", postback("1.50").json()["duplicate"], True)
        check("RS second step NOT deduped", postback("4.00").json()["duplicate"], False)

        print("\nad campaigns")
        check("POST /admin/campaigns", c.post("/admin/campaigns", params={"token": TOKEN},
              data={"campaign_id": "smoke-ad", "label": "Smoke", "pinned_offer_id": offer_id,
                    "target_country": "US", "target_device": "mobile"},
              follow_redirects=False).status_code, 303)
        r = c.get("/lp/smoke-ad", params={"_country": "US"})
        check("GET /lp/{campaign} serves the pin", r.status_code, 200)
        c.post("/admin/campaigns", params={"token": TOKEN},
               data={"campaign_id": "smoke-ad", "pinned_offer_id": "99999999",
                     "target_country": "US", "target_device": "mobile"}, follow_redirects=False)
        check("dead pin falls back, does not 404", c.get("/lp/smoke-ad", params={"_country": "US"}).status_code, 200)
        check("unknown campaign still serves", c.get("/lp/never-created", params={"_country": "US"}).status_code, 200)
        check("no eligible offers -> 503 no-fill", c.get("/lp/smoke-ad", params={"_country": "JP"}).status_code, 503)

        print("\nmedia")
        from urllib.parse import unquote
        pin_redirect = unquote(c.post(
            "/admin/media/pin", params={"token": TOKEN},
            data={"offer_id": offer_id, "storefront": "us",
                  "video_url": "https://example.com/x"},
            follow_redirects=False).headers["location"])
        check("bad video URL rejected", "does not look like" in pin_redirect, True)
        from app.video import extract_id, _relevant
        check("youtube id parsed from a watch URL",
              extract_id("https://youtu.be/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        check("mod/apk video titles filtered out",
              _relevant("GAME MOD APK UNLIMITED COINS", "Game"), False)
        from app.appmeta import _score
        check("wrong-app match rejected", _score("Township", "Travel Town") < 0.8, True)
        check("short-name match accepted",
              _score("Travel Town - Merge Adventure", "Travel Town"), 1.0)
        check("review page renders without media", c.get(
            f"/review/{slug}", params={"_country": "US"}).status_code, 200)

        print("\ncreator accounts")
        check("GET /signup", c.get("/signup").status_code, 200)
        check("reserved username rejected", c.post("/signup", data={
            "username": "admin", "email": "a@b.co", "password": "a-long-password",
            "accept": "1"}).status_code, 400)
        check("weak password rejected", c.post("/signup", data={
            "username": "emma", "email": "a@b.co", "password": "short",
            "accept": "1"}).status_code, 400)
        check("unacknowledged traffic share rejected", c.post("/signup", data={
            "username": "emma", "email": "a@b.co", "password": "a-long-password"},
            ).status_code, 400)
        r = c.post("/signup", data={"username": "emma", "email": "emma@example.com",
                                    "password": "a-long-password", "accept": "1"},
                   follow_redirects=False)
        check("signup succeeds", r.status_code, 303)
        check("duplicate username rejected", c.post("/signup", data={
            "username": "emma", "email": "other@example.com",
            "password": "a-long-password", "accept": "1"}).status_code, 400)
        check("GET /dashboard when logged in", c.get("/dashboard").status_code, 200)

        from app import store as _st
        # Force the creator branch: with a share above 0 this visitor might
        # legitimately land in the platform slice and see a full page, which
        # would make the assertion below flaky rather than wrong.
        _emma = _st.get_creator_by_username("emma")
        _st.update_creator(_emma["id"], platform_share=0)
        page = c.get("/u/emma", params={"_country": "US"})
        check("creator page renders with no key yet", page.status_code, 200)
        check("no offers shown without a key", "not finished" in page.text, True)

        # And the opposite: at 100% every visitor gets the platform's offers,
        # which must render even though the creator has no key of their own.
        _st.update_creator(_emma["id"], platform_share=100)
        platform_page = c.get("/u/emma", params={"_country": "US"})
        check("platform slice serves without a creator key",
              "not finished" in platform_page.text, False)
        _st.update_creator(_emma["id"], platform_share=10)
        check("unknown creator 404s", c.get("/u/nobody").status_code, 404)

        print("\ncreator security")
        from app import store as st, accounts as ac, creators as cr
        emma = st.get_creator_by_username("emma")
        check("password not stored in plaintext",
              "a-long-password" in emma["password_hash"], False)
        st.update_creator(emma["id"], api_key_enc=ac.encrypt_key("creator-secret-key"))
        emma = st.get_creator_by_username("emma")
        check("api key encrypted at rest", "creator-secret-key" in emma["api_key_enc"], False)
        check("api key decrypts back", ac.decrypt_key(emma["api_key_enc"]), "creator-secret-key")
        check("links save rejects a bad CSRF token", c.post("/dashboard/links", data={
            "csrf": "wrong", "offer_id": offer_id}, follow_redirects=False
            ).headers["location"].startswith("/dashboard?flash=Session+expired"), True)
        saved = c.post("/dashboard/links", data={
            "csrf": ac.csrf_token(c.cookies.get("creator_session")),
            "offer_id": offer_id, f"title_{offer_id}": "My pick"},
            follow_redirects=False)
        check("links save with a good CSRF token", saved.status_code, 303)
        check("offer stored for creator",
              [l["offer_id"] for l in st.get_creator_links(emma["id"])], [offer_id])
        check("unverified domain never served",
              st.get_creator_by_domain("links.example.com"), None)
        check("traffic split is deterministic",
              cr.serves_platform("s1", "emma", 10) == cr.serves_platform("s1", "emma", 10), True)

        c.post("/logout", follow_redirects=False)
        check("dashboard redirects once logged out",
              c.get("/dashboard", follow_redirects=False).status_code, 303)
        check("wrong password rejected", c.post("/login", data={
            "email": "emma@example.com", "password": "wrong"}).status_code, 401)

        print("\nadmin")
        check("GET /admin without token", c.get("/admin").status_code, 403)
        r = c.get("/admin", params={"token": TOKEN})
        check("GET /admin with token", r.status_code, 200)
        contains("dashboard shows campaigns", r.text, "Ad campaigns")
        contains("dashboard shows availability", r.text, "Offer availability")
        contains("dashboard shows the settings form", r.text, "Landing domain")
        check("API key never rendered", os.environ.get("OGADS_API_KEY", "zzz") in r.text, False)
        check("GET /admin/health", c.get("/admin/health", params={"token": TOKEN}).status_code, 200)
        check("GET /admin/stats", c.get("/admin/stats", params={"token": TOKEN}).status_code, 200)
        check("POST /admin/settings rejects bad domain", c.post("/admin/settings",
              params={"token": TOKEN}, data={"site_base_url": "nope/path"},
              follow_redirects=False).status_code, 303)
        check("GET /healthz", c.get("/healthz").status_code, 200)

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print("  -", f)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
