"""First-run setup: make sure the Camoufox stealth browser is installed.

The browser (~a few hundred MB) is NOT bundled in the release — it's fetched
once on first launch into the per-user Camoufox cache. Subsequent launches see
it and skip straight to the app.
"""
from __future__ import annotations


def browser_ready() -> bool:
    """True if the Camoufox browser is already installed for this user."""
    try:
        from camoufox.pkgman import installed_verstr
        installed_verstr()
        return True
    except Exception:
        return False


# Cloudflare's interactive Turnstile will not vouch for a Firefox build no real
# user runs. A months-old bundle resolves nothing while looking perfectly
# healthy — every request 200, the challenge simply never satisfied — so say so
# loudly rather than let it present as "the site broke".
STALE_BROWSER_DAYS = 180


def _browser_build() -> tuple[str | None, int | None]:
    """(version, age_in_days) of the browser that will actually launch.

    Both come from the same application.ini next to the binary launch_path()
    resolves to, so the version we name can never disagree with the age we
    measured. BuildID (YYYYMMDDHHMMSS) is the build date, not the download date.
    """
    from datetime import datetime
    version = age = None
    try:
        from camoufox.pkgman import launch_path
        from pathlib import Path
        ini = Path(launch_path()).parent / "application.ini"
        for line in ini.read_text(errors="replace").splitlines():
            if line.startswith("Version="):
                version = line.split("=", 1)[1].strip()
            elif line.startswith("BuildID="):
                stamp = line.split("=", 1)[1].strip()[:8]
                age = (datetime.now() - datetime.strptime(stamp, "%Y%m%d")).days
    except Exception:
        pass
    return version, age


def browser_age_days() -> int | None:
    """Age of the installed browser build in days, or None if undeterminable."""
    return _browser_build()[1]


def browser_staleness_warning() -> str | None:
    """A line to show the user when the installed browser is too old to pass
    Cloudflare, or None when it looks current."""
    ver, age = _browser_build()
    if age is None or age < STALE_BROWSER_DAYS:
        return None
    return (f"Stealth browser {ver or 'unknown'} is {age} days old — Cloudflare will likely "
            "refuse to issue a challenge token to a build this stale, and every "
            "resolve will fail. Run: camoufox sync && camoufox fetch")


def ensure_browser(status_cb=None) -> None:
    """Download + install the Camoufox browser if missing or outdated.

    ``status_cb(str)`` receives human-readable progress lines. Raises on hard
    failure (caller should surface it).
    """
    def say(msg: str) -> None:
        if status_cb:
            try:
                status_cb(msg)
            except Exception:
                pass

    from camoufox.__main__ import CamoufoxUpdate

    upd = CamoufoxUpdate()
    if upd.is_updated_needed():
        say("Downloading the stealth browser (one-time, a few hundred MB)…")
        upd.update()
        say("Browser installed.")
    else:
        say("Browser already installed.")

    # GeoIP DB (optional; flaky download — never fatal)
    try:
        from camoufox.locale import ALLOW_GEOIP, download_mmdb
        if ALLOW_GEOIP:
            say("Fetching GeoIP database…")
            download_mmdb()
    except Exception:
        pass

    # Default addons (best effort)
    try:
        from camoufox.addons import DefaultAddons, maybe_download_addons
        say("Fetching browser add-ons…")
        maybe_download_addons(list(DefaultAddons))
    except Exception:
        pass

    say("Setup complete.")
