"""Runtime configuration: filesystem layout, SEC identity and throttling."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: SEC requires a descriptive User-Agent with a contact address on every request.
IDENTITY_ENV = "EDGAR_IDENTITY"

_DEFAULT_ROOT = Path(os.environ.get("BDC_DATA_DIR", Path.cwd() / "data"))


@dataclass(frozen=True)
class Settings:
    """Paths and knobs shared by every stage of the pipeline."""

    root: Path = _DEFAULT_ROOT
    identity: str | None = os.environ.get(IDENTITY_ENV)

    @property
    def cache_dir(self) -> Path:
        """Where downloaded SEC artefacts (DERA zips, XBRL) are memoised."""
        return self.root / "cache"

    @property
    def dera_dir(self) -> Path:
        return self.cache_dir / "dera"

    @property
    def db_path(self) -> Path:
        return self.root / "bdc.db"

    @property
    def export_dir(self) -> Path:
        return self.root / "export"

    def ensure_dirs(self) -> None:
        for path in (self.root, self.cache_dir, self.dera_dir, self.export_dir):
            path.mkdir(parents=True, exist_ok=True)


SETTINGS = Settings()

#: A cheap, always-present file used to prove we can actually reach the SEC.
PREFLIGHT_URL = "https://www.sec.gov/files/company_tickers.json"


class SecUnreachable(RuntimeError):
    """The SEC cannot be reached, and retrying will not help.

    Raised for policy denials and connection failures — the cases where a
    harvest would otherwise spend minutes retrying every request in turn.
    """


def check_sec_reachable(timeout: float = 15.0, url: str = PREFLIGHT_URL) -> None:
    """Probe sec.gov once and fail fast with a diagnosis.

    A blocked egress proxy answers CONNECT with 403/407 and no amount of
    backoff changes that, so a harvest should stop here rather than work
    through twelve quarters and forty-three companies discovering it again.
    """
    import httpx

    identity = os.environ.get(IDENTITY_ENV) or "bdctracker"
    try:
        response = httpx.get(
            url, timeout=timeout, follow_redirects=True,
            headers={"User-Agent": identity, "Accept-Encoding": "gzip, deflate"},
        )
    except httpx.ProxyError as exc:
        raise SecUnreachable(
            f"Blocked before reaching the SEC: {exc}. An egress proxy is refusing the "
            "connection (a 403/407 on CONNECT is a policy denial, not a transient error). "
            "Run this from a network that allows sec.gov, or have the proxy allow it."
        ) from None
    except httpx.HTTPError as exc:
        raise SecUnreachable(
            f"Could not reach {url}: {type(exc).__name__}: {exc}. "
            "Check network access and any HTTPS_PROXY setting."
        ) from None

    if response.status_code == 403:
        raise SecUnreachable(
            "The SEC returned 403. It rejects requests without a descriptive "
            f"User-Agent, so check {IDENTITY_ENV} is set to a real name and contact "
            f"address (currently {identity!r})."
        )
    if response.status_code == 429:
        raise SecUnreachable(
            "The SEC returned 429 (rate limited). Wait a few minutes before retrying."
        )
    if response.status_code >= 400:
        raise SecUnreachable(f"{url} returned HTTP {response.status_code}.")


def configure_edgar(identity: str | None = None) -> str:
    """Point edgartools at the SEC with a compliant identity.

    The SEC rejects unidentified traffic, so this raises rather than letting a
    run fail hundreds of requests later.
    """
    import edgar

    ident = identity or SETTINGS.identity
    if not ident:
        raise RuntimeError(
            f"Set {IDENTITY_ENV} (e.g. 'Jane Doe jane@example.com') before hitting EDGAR; "
            "the SEC blocks requests without a contact address."
        )
    edgar.set_identity(ident)
    return ident
