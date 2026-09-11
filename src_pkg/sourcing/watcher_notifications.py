"""Deduplicated SendGrid alerts for resumes discovered by Medhunt Watcher."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import logging
import re
from urllib.parse import urlsplit

import httpx

from . import config, store


logger = logging.getLogger("medhunt.watcher_email")
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def configured() -> bool:
    return bool(
        config.WATCHER_EMAIL_NOTIFICATIONS_ENABLED
        and config.SENDGRID_API_KEY
        and _EMAIL.fullmatch(config.EMAIL_FROM)
        and any(_EMAIL.fullmatch(email) for email in config.WATCHER_NOTIFICATION_EMAILS)
    )


def _profile_link(value: str) -> str:
    try:
        parsed = urlsplit(str(value or "").strip())
        host = (parsed.hostname or "").casefold()
        if parsed.scheme == "https" and (host == "indeed.com" or host.endswith(".indeed.com")):
            return parsed.geturl()
    except ValueError:
        pass
    return ""


def _email_html(*, query: str, location: str, detected_at: str, profiles: list[dict]) -> str:
    new_count = sum(1 for profile in profiles if profile.get("change") == "new")
    updated_count = sum(1 for profile in profiles if profile.get("change") == "updated")
    search_parts = [part for part in (query.strip(), location.strip()) if part]
    search_label = " · ".join(search_parts) or "Indeed Smart Sourcing"
    try:
        detected = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
        detected_label = detected.astimezone(timezone.utc).strftime("%b %d, %Y at %H:%M UTC")
    except (TypeError, ValueError):
        detected_label = "just now"

    rows = []
    for profile in profiles:
        name = escape(str(profile.get("name") or "Candidate"))
        detail = " · ".join(
            escape(str(value).strip())
            for value in (profile.get("headline"), profile.get("location"))
            if str(value or "").strip()
        )
        marker = escape(str(profile.get("resume_marker") or "").strip())
        change = "Updated resume" if profile.get("change") == "updated" else "New resume"
        link = _profile_link(str(profile.get("source_url") or ""))
        name_html = (
            f'<a href="{escape(link, quote=True)}" style="color:#5531a7;text-decoration:none">{name}</a>'
            if link else name
        )
        rows.append(
            '<tr><td style="padding:12px;border-bottom:1px solid #ece8f7">'
            f'<strong>{name_html}</strong><br>'
            f'<span style="font-size:12px;color:#6e6685">{detail}</span>'
            '</td><td style="padding:12px;border-bottom:1px solid #ece8f7;white-space:nowrap">'
            f'<strong>{escape(change)}</strong><br>'
            f'<span style="font-size:12px;color:#6e6685">{marker}</span>'
            '</td></tr>'
        )

    count_label = f"{new_count} new"
    if updated_count:
        count_label += f", {updated_count} updated"
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif;'
        'max-width:680px;margin:0 auto;color:#241b35">'
        '<div style="font-size:20px;font-weight:750;color:#5531a7;padding:8px 0 18px">Medhunt</div>'
        '<h1 style="font-size:22px;margin:0 0 8px">New resume activity found</h1>'
        f'<p style="margin:0 0 18px;color:#625875">Medhunt Watcher detected {count_label} '
        f'resume profile(s) for <strong>{escape(search_label)}</strong> on {escape(detected_label)}.</p>'
        '<table style="border-collapse:collapse;width:100%;font-size:14px">'
        f'<tbody>{"".join(rows)}</tbody></table>'
        '<p style="font-size:12px;color:#817991;margin-top:20px">'
        'This alert was generated after the initial search baseline. Open Indeed or Medhunt to review the candidates.'
        '</p></div>'
    )


def _send_sendgrid(recipient: str, subject: str, html: str) -> tuple[bool, str]:
    payload = {
        "personalizations": [{"to": [{"email": recipient}]}],
        "from": {"email": config.EMAIL_FROM, "name": config.EMAIL_FROM_NAME},
        "subject": subject,
        "content": [{"type": "text/html", "value": html}],
    }
    try:
        response = httpx.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {config.SENDGRID_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=config.SENDGRID_TIMEOUT,
        )
        if 200 <= response.status_code < 300:
            return True, ""
        return False, f"SendGrid returned HTTP {response.status_code}."
    except Exception as exc:  # noqa: BLE001 - notification failures remain retryable
        logger.warning("Watcher email delivery failed (%s).", type(exc).__name__)
        return False, f"SendGrid request failed ({type(exc).__name__})."


def send_new_resume_alert(
    *, event_id: str, query: str, location: str, detected_at: str, profiles: list[dict]
) -> dict:
    """Send an alert once per event and recipient; failed recipients remain retryable."""
    recipients = [
        email for email in config.WATCHER_NOTIFICATION_EMAILS
        if _EMAIL.fullmatch(email)
    ]
    if not configured():
        return {
            "status": "disabled",
            "sent": 0,
            "deduplicated": 0,
            "failed": 0,
            "configured": False,
        }

    safe_profiles = list(profiles or [])[:100]
    subject = f"Medhunt: {len(safe_profiles)} new or updated Indeed resume"
    if len(safe_profiles) != 1:
        subject += "s"
    html = _email_html(
        query=query,
        location=location,
        detected_at=detected_at,
        profiles=safe_profiles,
    )
    sent = deduplicated = failed = in_progress = 0
    for recipient in recipients:
        claim = store.claim_watcher_email_delivery(event_id, recipient)
        if not claim.get("claimed"):
            if claim.get("status") == "sent":
                deduplicated += 1
            elif claim.get("status") == "sending":
                in_progress += 1
            else:
                failed += 1
            continue
        ok, error = _send_sendgrid(recipient, subject, html)
        store.finish_watcher_email_delivery(
            event_id, recipient, sent=ok, error=error,
        )
        if ok:
            sent += 1
        else:
            failed += 1

    if failed:
        status = "partial" if sent or deduplicated else "failed"
    elif in_progress:
        status = "in_progress"
    elif sent:
        status = "sent"
    else:
        status = "deduplicated"
    return {
        "status": status,
        "sent": sent,
        "deduplicated": deduplicated,
        "failed": failed,
        "in_progress": in_progress,
        "configured": True,
    }
