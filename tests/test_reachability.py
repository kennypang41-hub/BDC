"""A blocked network must fail fast and loudly, not retry its way through the run."""
from __future__ import annotations

import httpx
import pytest

from bdctracker import config
from bdctracker.sources import dera


class _Response:
    def __init__(self, status_code: int, content: bytes = b""):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)


def test_preflight_passes_on_a_healthy_response(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Response(200))
    config.check_sec_reachable()


def test_preflight_names_a_proxy_denial_rather_than_calling_it_transient(monkeypatch):
    def blocked(*args, **kwargs):
        raise httpx.ProxyError("403 Forbidden")

    monkeypatch.setattr(httpx, "get", blocked)
    with pytest.raises(config.SecUnreachable) as caught:
        config.check_sec_reachable()
    assert "policy denial" in str(caught.value)
    assert "not a transient error" in str(caught.value)


def test_preflight_reads_a_403_from_the_sec_as_a_user_agent_problem(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Response(403))
    with pytest.raises(config.SecUnreachable, match="EDGAR_IDENTITY"):
        config.check_sec_reachable()


def test_preflight_reports_rate_limiting_separately(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Response(429))
    with pytest.raises(config.SecUnreachable, match="429"):
        config.check_sec_reachable()


def _settings_in(tmp_path, monkeypatch):
    """Point the cache at a temp dir; Settings is frozen, so swap the instance."""
    settings = config.Settings(root=tmp_path, identity="test")
    monkeypatch.setattr(dera, "SETTINGS", settings)
    return settings


def _patch_quarter_fetch(monkeypatch, tmp_path, handler):
    settings = _settings_in(tmp_path, monkeypatch)
    monkeypatch.setattr(dera, "configure_edgar", lambda *a, **k: "test")
    monkeypatch.setattr("edgar.httprequests.get_with_retry", handler)
    return settings


def test_an_unpublished_quarter_is_skipped_not_fatal(monkeypatch, tmp_path):
    """The newest quarter routinely 404s; that is data, not an outage."""
    _patch_quarter_fetch(monkeypatch, tmp_path, lambda url, *a, **k: _Response(404))
    assert dera.download_quarter(dera.Quarter(2099, 1)) is None


def test_a_blocked_quarter_download_raises_rather_than_reporting_a_gap(monkeypatch, tmp_path):
    """Treating a blocked network as 'missing quarters' hides the real problem."""
    def blocked(url, *args, **kwargs):
        raise httpx.ProxyError("403 Forbidden")

    _patch_quarter_fetch(monkeypatch, tmp_path, blocked)
    with pytest.raises(config.SecUnreachable):
        dera.download_quarter(dera.Quarter(2025, 1))


def test_a_server_error_on_a_quarter_raises(monkeypatch, tmp_path):
    _patch_quarter_fetch(monkeypatch, tmp_path, lambda url, *a, **k: _Response(500))
    with pytest.raises(config.SecUnreachable, match="500"):
        dera.download_quarter(dera.Quarter(2025, 1))


def test_a_cached_quarter_never_touches_the_network(monkeypatch, tmp_path):
    settings = _settings_in(tmp_path, monkeypatch)
    settings.ensure_dirs()
    quarter = dera.Quarter(2025, 1)
    cached = settings.dera_dir / quarter.filename
    cached.write_bytes(b"zip")

    def explode(*args, **kwargs):
        raise AssertionError("should not have hit the network")

    monkeypatch.setattr("edgar.httprequests.get_with_retry", explode)
    assert dera.download_quarter(quarter) == cached
