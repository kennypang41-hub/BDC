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
