"""Offline Enformion HTTP 429 retry tests; no provider calls or credits."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sourcing import enformion_client as ef


class _Response:
    def __init__(self, status_code, payload=None, *, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


def _install_client(monkeypatch, responses):
    pending = list(responses)
    calls = []

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, *, json, headers):
            calls.append({"url": url, "body": json, "headers": headers})
            return pending.pop(0)

    monkeypatch.setattr(ef.httpx, "Client", _Client)
    return calls


def _configure_live_client(monkeypatch, *, retries=2, backoff=1.0, max_wait=30.0):
    monkeypatch.setattr(ef.config, "DEMO_MODE", False)
    monkeypatch.setattr(ef.config, "ENFORMION_AP_NAME", "private-test-name")
    monkeypatch.setattr(ef.config, "ENFORMION_AP_PASSWORD", "private-test-password")
    monkeypatch.setattr(ef.config, "ENFORMION_RATE_LIMIT_RETRIES", retries)
    monkeypatch.setattr(ef.config, "ENFORMION_RATE_LIMIT_BACKOFF_SECONDS", backoff)
    monkeypatch.setattr(ef.config, "ENFORMION_RATE_LIMIT_MAX_WAIT_SECONDS", max_wait)


def _target_person():
    return {
        "fullName": "Jane Doe",
        "tahoeId": "TH-JANE",
        "addresses": [{"city": "Atlanta", "state": "GA", "isCurrent": True}],
        "phoneNumbers": [{
            "phoneNumber": "4045550105",
            "phoneType": "Wireless",
            "isConnected": True,
        }],
    }


def test_enrich_honors_retry_after_then_returns_successful_response(monkeypatch):
    _configure_live_client(monkeypatch)
    calls = _install_client(monkeypatch, [
        _Response(429, {"error": {"code": "rate_limited"}}, headers={"Retry-After": "2"}),
        _Response(200, {"persons": []}),
    ])
    waits = []
    monkeypatch.setattr(ef, "_sleep", waits.append)

    result = ef.enrich("Jane Doe", "Atlanta, GA")

    assert result["status"] == "no_match"
    assert result["credits_spent"] == 1
    assert len(calls) == 2
    assert waits == [2.0]
    assert result["rate_limited"] is True
    assert result["rate_limit_retries"] == 1
    assert result["rate_limit_wait_seconds"] == 2.0
    assert result["request_attempts"] == 2
    assert result["retry_after_seconds"] == 2.0


def test_missing_retry_after_uses_bounded_exponential_backoff(monkeypatch):
    _configure_live_client(monkeypatch, retries=2, backoff=0.5, max_wait=5.0)
    calls = _install_client(monkeypatch, [
        _Response(429),
        _Response(429),
        _Response(200, {"persons": []}),
    ])
    waits = []
    monkeypatch.setattr(ef, "_sleep", waits.append)

    result = ef.enrich("Jane Doe", "Atlanta, GA")

    assert result["status"] == "no_match"
    assert len(calls) == 3
    assert waits == [0.5, 1.0]
    assert result["rate_limit_retries"] == 2
    assert result["rate_limit_wait_seconds"] == 1.5
    assert result["request_attempts"] == 3


def test_long_retry_after_is_deferred_without_an_early_retry(monkeypatch):
    _configure_live_client(monkeypatch, retries=2, backoff=0.1, max_wait=5.0)
    calls = _install_client(monkeypatch, [
        _Response(
            429,
            {"error": {"code": "Rate Limit Exceeded", "message": "Try later"}},
            headers={"Retry-After": "60"},
        ),
    ])
    waits = []
    monkeypatch.setattr(ef, "_sleep", waits.append)

    result = ef.enrich("Jane Doe", "Atlanta, GA")

    assert result["status"] == "error"
    assert result["http_status"] == 429
    assert result["retryable"] is True
    assert result["credits_spent"] == 0
    assert len(calls) == 1
    assert waits == []
    assert result["retry_deferred"] is True
    assert result["next_retry_delay_seconds"] == 60.0
    assert result["rate_limit_retries"] == 0
    assert "private-test-password" not in repr(result)
    assert "private-test-name" not in repr(result)


def test_tahoe_resolution_uses_the_same_rate_limit_policy(monkeypatch):
    _configure_live_client(monkeypatch, retries=1, backoff=0.25, max_wait=5.0)
    calls = _install_client(monkeypatch, [
        _Response(429),
        _Response(200, {"persons": [_target_person()]}),
    ])
    waits = []
    monkeypatch.setattr(ef, "_sleep", waits.append)

    result = ef.resolve_tahoe_id("TH-JANE", "Jane Doe", "Atlanta, GA")

    assert result["status"] == "success"
    assert result["relative_bridge_used"] is True
    assert result["credits_spent"] == 1
    assert len(calls) == 2
    assert calls[0]["body"] == {"TahoeIds": ["TH-JANE"], "Page": 1, "ResultsPerPage": 10}
    assert waits == [0.25]
    assert result["rate_limited"] is True
    assert result["rate_limit_retries"] == 1
    assert result["request_attempts"] == 2


def test_retry_after_http_date_is_converted_to_seconds(monkeypatch):
    monkeypatch.setattr(
        ef,
        "_utcnow",
        lambda: datetime(2026, 8, 15, 0, 0, 0, tzinfo=timezone.utc),
    )
    response = _Response(
        429,
        headers={"Retry-After": "Sat, 15 Aug 2026 00:00:07 GMT"},
    )

    assert ef._retry_after_seconds(response) == 7.0
