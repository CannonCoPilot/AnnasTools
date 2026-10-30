"""_fast_download_url maps the member API's answers to named errors, and keeps the account block.

The quota case is the LIVE answer measured 2026-10-30 (W12, after 25 successes):
HTTP 429 {"error": "No downloads left"}. It names neither "quota" nor "limit", so it used to fall
through to API_ERROR and memberDownload's quota fallback never ran.
"""
import pytest

from annas_tools import mcp_server as ms

ACCT = {"downloads_left": 0, "downloads_per_day": 25}


def _answer(monkeypatch, status, data, cause=""):
    monkeypatch.setattr(ms, "_api_json", lambda path, params: (status, data, cause))


def test_live_429_no_downloads_left_is_quota_exhausted(monkeypatch):
    _answer(monkeypatch, 429, {"error": "No downloads left"})
    res = ms._fast_download_url("a" * 32, "k")
    assert res["error"] == "QUOTA_EXHAUSTED"
    assert res["status"] == 429


def test_429_with_any_wording_is_quota_exhausted(monkeypatch):
    _answer(monkeypatch, 429, {"error": "slow down"})
    assert ms._fast_download_url("a" * 32, "k")["error"] == "QUOTA_EXHAUSTED"


@pytest.mark.parametrize("msg", ["Quota exceeded", "daily limit reached", "No downloads left"])
def test_quota_wordings_without_429(monkeypatch, msg):
    _answer(monkeypatch, 403, {"error": msg})
    assert ms._fast_download_url("a" * 32, "k")["error"] == "QUOTA_EXHAUSTED"


@pytest.mark.parametrize("msg,code", [("Record not found", "NOT_FOUND"),
                                      ("Invalid secret key", "AUTH_INVALID"),
                                      ("something else", "API_ERROR")])
def test_other_errors_unchanged(monkeypatch, msg, code):
    _answer(monkeypatch, 400, {"error": msg})
    assert ms._fast_download_url("a" * 32, "k")["error"] == code


def test_success_passes_account_block_through(monkeypatch):
    _answer(monkeypatch, 200, {"download_url": "https://x/y.pdf", "account_fast_download_info": ACCT})
    res = ms._fast_download_url("a" * 32, "k")
    assert res["download_url"] == "https://x/y.pdf"
    assert res["account_fast_download_info"] == ACCT


def test_quota_answer_keeps_account_block(monkeypatch):
    _answer(monkeypatch, 429, {"error": "No downloads left", "account_fast_download_info": ACCT})
    assert ms._fast_download_url("a" * 32, "k")["account_fast_download_info"] == ACCT


def test_no_account_block_adds_no_key(monkeypatch):
    _answer(monkeypatch, 200, {"download_url": "https://x/y.pdf"})
    assert "account_fast_download_info" not in ms._fast_download_url("a" * 32, "k")
