"""Healthcareboard email-code and extension-session integration tests."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sourcing import config, healthboard_auth


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_healthboard_client_requests_and_verifies_email_code(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/request-code"):
            return _Response({"challenge": "challenge-1", "expires_in": 600})
        return _Response({
            "extension_token": "opaque-token",
            "user": {"user_id": "42", "email": "recruiter@example.test"},
        })

    monkeypatch.setattr(config, "HEALTHBOARD_BASE_URL", "https://board.example.test")
    monkeypatch.setattr(healthboard_auth.httpx, "post", fake_post)

    requested = healthboard_auth.request_code("recruiter@example.test")
    verified = healthboard_auth.verify_code(
        "recruiter@example.test", "123456", requested["challenge"],
    )

    assert verified["extension_token"] == "opaque-token"
    assert calls[0][1]["json"] == {"email": "recruiter@example.test"}
    assert calls[1][1]["json"]["code"] == "123456"
