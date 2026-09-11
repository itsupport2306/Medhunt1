"""SendGrid watcher alerts are baseline-safe, retryable, and deduplicated."""
from __future__ import annotations

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sourcing import config, store, watcher_notifications


def _configure(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "watcher-email.db"))
    monkeypatch.setattr(config, "WATCHER_EMAIL_NOTIFICATIONS_ENABLED", True)
    monkeypatch.setattr(config, "SENDGRID_API_KEY", "test-sendgrid-key")
    monkeypatch.setattr(config, "EMAIL_FROM", "alerts@example.test")
    monkeypatch.setattr(config, "EMAIL_FROM_NAME", "Medhunt")
    monkeypatch.setattr(
        config,
        "WATCHER_NOTIFICATION_EMAILS",
        ("first@example.test", "second@example.test"),
    )


def _event():
    return {
        "event_id": "indeed.watch.change-1",
        "query": "registered nurse",
        "location": "Ohio",
        "detected_at": "2026-09-08T12:30:00Z",
        "profiles": [{
            "name": "Alice <Admin>",
            "location": "Columbus, OH",
            "headline": "Registered Nurse",
            "source_url": "https://employers.indeed.com/smartsourcing?candidateId=alice",
            "source_id": "alice",
            "resume_marker": "Resume updated just now",
            "change": "new",
        }],
    }


def test_watcher_alert_sends_once_per_recipient(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    sends = []

    def fake_send(recipient, subject, html):
        sends.append((recipient, subject, html))
        return True, ""

    monkeypatch.setattr(watcher_notifications, "_send_sendgrid", fake_send)
    first = watcher_notifications.send_new_resume_alert(**_event())
    second = watcher_notifications.send_new_resume_alert(**_event())

    assert first == {
        "status": "sent", "sent": 2, "deduplicated": 0,
        "failed": 0, "in_progress": 0, "configured": True,
    }
    assert second == {
        "status": "deduplicated", "sent": 0, "deduplicated": 2,
        "failed": 0, "in_progress": 0, "configured": True,
    }
    assert [recipient for recipient, _subject, _html in sends] == [
        "first@example.test", "second@example.test",
    ]
    assert "Alice &lt;Admin&gt;" in sends[0][2]
    assert "Alice <Admin>" not in sends[0][2]
    deliveries = store.list_watcher_email_deliveries("indeed.watch.change-1")
    assert len(deliveries) == 2
    assert {delivery["status"] for delivery in deliveries} == {"sent"}
    assert {delivery["attempts"] for delivery in deliveries} == {1}


def test_failed_watcher_alert_retries_only_failed_recipient(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    attempts = {}

    def flaky_send(recipient, _subject, _html):
        attempts[recipient] = attempts.get(recipient, 0) + 1
        if recipient == "second@example.test" and attempts[recipient] == 1:
            return False, "SendGrid returned HTTP 503."
        return True, ""

    monkeypatch.setattr(watcher_notifications, "_send_sendgrid", flaky_send)
    first = watcher_notifications.send_new_resume_alert(**_event())
    second = watcher_notifications.send_new_resume_alert(**_event())

    assert first["status"] == "partial"
    assert first["sent"] == 1 and first["failed"] == 1
    assert second["status"] == "sent"
    assert second["sent"] == 1 and second["deduplicated"] == 1
    assert attempts == {"first@example.test": 1, "second@example.test": 2}
    deliveries = store.list_watcher_email_deliveries("indeed.watch.change-1")
    assert {delivery["status"] for delivery in deliveries} == {"sent"}
    assert sorted(delivery["attempts"] for delivery in deliveries) == [1, 2]


def test_watcher_alert_is_inert_when_disabled(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "WATCHER_EMAIL_NOTIFICATIONS_ENABLED", False)
    monkeypatch.setattr(
        watcher_notifications,
        "_send_sendgrid",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not send")),
    )

    result = watcher_notifications.send_new_resume_alert(**_event())

    assert result["status"] == "disabled"
    assert store.list_watcher_email_deliveries() == []
