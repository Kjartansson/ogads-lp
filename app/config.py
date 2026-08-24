"""Configuration, loaded once from .env at import time.

Every OGAds credential lives here and nowhere else -- no module outside
ogads.py should ever need to touch the API key, and it is never rendered
into a template or returned by an endpoint.
"""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _csv(name: str) -> list[str]:
    return [p.strip() for p in _str(name).split(",") if p.strip()]


@dataclass(frozen=True)
class Settings:
    # --- OGAds ---------------------------------------------------------
    api_key: str = field(default_factory=lambda: _str("OGADS_API_KEY"))
    # The current documented endpoint. OGAds rotates its locker domains but
    # keeps old ones working, so an existing integration never has to move;
    # a NEW one should use this.
    endpoint: str = field(
        default_factory=lambda: _str("OGADS_ENDPOINT", "https://lockerpreview.com/api/v2")
    )
    # ctype is a BITWISE FLAG, not an enum -- add the categories you want:
    #     1 CPI (app installs)   2 CPA   4 PIN submit   8 VID
    # so 3 = CPI+CPA, 12 = PIN+VID, and 0/absent = every type.
    # This project is an app-install catalog, so the default is 1.
    # Per OGAds: ctype has NO effect when the device is desktop.
    ctype_mobile: str = field(default_factory=lambda: _str("OGADS_CTYPE", "1"))
    max_offers: int = field(default_factory=lambda: _int("OGADS_MAX", 0))  # 0 = no cap
    # `min` backfills the list when too few offers match -- but OGAds warns it
    # may then include offers you have explicitly blocked. Off by default.
    min_offers: int = field(default_factory=lambda: _int("OGADS_MIN", 0))
    timeout: float = field(default_factory=lambda: float(_int("OGADS_TIMEOUT", 10)))
    mock: bool = field(default_factory=lambda: _bool("OGADS_MOCK", False))
    # Your OGAds affiliate id. Every smartlink minted by your key carries it
    # as `aff_id`, and every postback for your account reports it. Knowing it
    # lets us tell your traffic from a creator's -- and a forged postback
    # from a real one.
    affiliate_id: str = field(default_factory=lambda: _str("OGADS_AFFILIATE_ID"))

    # --- Media ---------------------------------------------------------
    # Optional. Without it, gameplay videos can still be PINNED by hand in
    # the admin panel (attribution comes from YouTube's keyless oEmbed);
    # only automatic discovery needs the key.
    youtube_api_key: str = field(default_factory=lambda: _str("YOUTUBE_API_KEY"))

    # --- Creator link pages ---------------------------------------------
    # Percentage of a creator's VISITORS served this site's offers instead of
    # theirs -- the hosting fee, taken in traffic. Applied to new signups;
    # per-creator overrides live in the creators table.
    default_platform_share: int = field(
        default_factory=lambda: max(0, min(100, _int("PLATFORM_SHARE_PCT", 10))))

    # Public IP(s) of this server, used to verify that a creator's custom
    # domain really points here before we serve their content on it.
    server_ips: list[str] = field(default_factory=lambda: _csv("SERVER_IPS"))

    # --- Site ----------------------------------------------------------
    site_name: str = field(default_factory=lambda: _str("SITE_NAME", "App Offers"))
    base_url: str = field(
        default_factory=lambda: _str("SITE_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
    )
    cache_ttl: int = field(default_factory=lambda: _int("CACHE_TTL", 300))

    # --- Local testing --------------------------------------------------
    # OGAds selects offers from the VISITOR's IP and User-Agent. From
    # localhost the IP is 127.0.0.1, which geo-resolves to nothing and
    # returns an empty offer list -- that is the single most common reason
    # a local OGAds setup "doesn't work". These let a dev box impersonate
    # a real visitor. DEBUG additionally enables ?_ip= / ?_ua= overrides.
    debug: bool = field(default_factory=lambda: _bool("DEBUG", False))
    test_ip: str = field(default_factory=lambda: _str("TEST_IP"))
    test_ua: str = field(
        default_factory=lambda: _str(
            "TEST_UA",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
        )
    )
    # Number of proxy hops we trust in X-Forwarded-For. 0 = trust none
    # (use the socket peer). Behind Cloudflare only, this is 1.
    trusted_proxy_hops: int = field(default_factory=lambda: _int("TRUSTED_PROXY_HOPS", 0))

    # --- Postback -------------------------------------------------------
    postback_extra_ips: list[str] = field(default_factory=lambda: _csv("POSTBACK_EXTRA_IPS"))
    postback_secret: str = field(default_factory=lambda: _str("POSTBACK_SECRET"))

    # --- Admin ----------------------------------------------------------
    admin_token: str = field(default_factory=lambda: _str("ADMIN_TOKEN"))

    db_path: Path = field(
        default_factory=lambda: Path(_str("DB_PATH", str(BASE_DIR / "data" / "ogads.db")))
    )

    @property
    def configured(self) -> bool:
        return bool(self.api_key) or self.mock


settings = Settings()


# --------------------------------------------------------------- runtime edits
# The API key and endpoint can be changed from the admin panel without a
# restart. `settings` stays frozen and authoritative for everything loaded at
# boot; these overrides layer on top and are persisted back to .env so the
# change survives a restart. Only credentials are editable this way -- the
# rest of the config is deliberately deploy-time only.
_overrides: dict[str, str] = {}


def api_key() -> str:
    return _overrides.get("OGADS_API_KEY") or settings.api_key


def endpoint() -> str:
    return _overrides.get("OGADS_ENDPOINT") or settings.endpoint


def base_url() -> str:
    """Public origin of this site.

    Editable at runtime because the postback URL and every landing URL are
    built from it, and it changes the day a real domain is pointed here.
    """
    return (_overrides.get("SITE_BASE_URL") or settings.base_url).rstrip("/")


def mask(secret: str) -> str:
    """Render a credential safe to display. Never returns the secret."""
    if not secret:
        return "(not set)"
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}…{secret[-4:]} ({len(secret)} chars)"


def set_env_value(name: str, value: str) -> None:
    """Persist one key to .env, preserving comments, order and every other line.

    Written to a temp file in the same directory and then renamed, so an
    interrupted write cannot leave a truncated .env behind and lock you out
    of your own credentials.
    """
    if not re.fullmatch(r"[A-Z0-9_]+", name):
        raise ValueError(f"refusing to write unexpected env key {name!r}")
    if "\n" in value or "\r" in value:
        raise ValueError("env values cannot contain newlines")

    path = BASE_DIR / ".env"
    lines = path.read_text().splitlines() if path.exists() else []
    replaced = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(f"{name}="):
            lines[i] = f"{name}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{name}={value}")

    tmp = path.with_name(".env.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    _overrides[name] = value
    os.environ[name] = value
