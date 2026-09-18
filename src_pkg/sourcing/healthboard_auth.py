"""HealthBoard-owned authentication and Medhunt activity reporting."""
from __future__ import annotations

import hashlib
import threading
import time

import httpx

from . import config

_CACHE_LOCK = threading.Lock()
_USER_CACHE: dict[str, tuple[float, dict]] = {}


def enabled() -> bool:
    return bool(config.HEALTHBOARD_BASE_URL)


def _url(path: str) -> str:
    return f"{config.HEALTHBOARD_BASE_URL.rstrip('/')}{path}"


def request_code(email: str, *, client_ip: str = "") -> dict:
    headers = {"X-Forwarded-For": client_ip} if client_ip else None
    response = httpx.post(
        _url("/api/extension/auth/request-code"),
        json={"email": email},
        headers=headers,
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def verify_code(email: str, code: str, challenge: str, *, client_ip: str = "") -> dict:
    headers = {"X-Forwarded-For": client_ip} if client_ip else None
    response = httpx.post(
        _url("/api/extension/auth/verify-code"),
        json={"email": email, "code": code, "challenge": challenge},
        headers=headers,
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def verify_extension_token(token: str) -> dict:
    """Resolve an opaque extension token through HealthBoard with a short cache."""
    supplied = str(token or "").strip()
    if not enabled() or not supplied:
        raise ValueError("HealthBoard extension authentication is unavailable")
    key = hashlib.sha256(supplied.encode()).hexdigest()
    now = time.time()
    with _CACHE_LOCK:
        cached = _USER_CACHE.get(key)
        if cached and cached[0] > now:
            return dict(cached[1])
    response = httpx.get(
        _url("/api/extension/auth/me"),
        headers={"X-Capture-Token": supplied},
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    response.raise_for_status()
    user = response.json()
    if not user.get("user_id"):
        raise ValueError("HealthBoard did not return a user identity")
    with _CACHE_LOCK:
        _USER_CACHE[key] = (now + config.HEALTHBOARD_AUTH_CACHE_SECONDS, dict(user))
    return user


def report_enrichment(token: str, *, event_id: str, candidate_id: int,
                      status: str, source: str = "", run_id: str = "") -> bool:
    response = httpx.post(
        _url("/api/extension/activity/enrichment"),
        headers={"X-Capture-Token": str(token or "").strip()},
        json={
            "event_id": event_id,
            "candidate_id": str(candidate_id),
            "status": status,
            "source": source,
            "run_id": run_id,
        },
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    response.raise_for_status()
    return bool(response.json().get("recorded", True))


def list_recruiters(token: str) -> list[dict]:
    response = httpx.get(
        _url("/api/extension/team/recruiters"),
        headers={"X-Capture-Token": str(token or "").strip()},
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    return list(payload.get("items") or [])


def assign_conversation(token: str, *, conversation: dict, recruiter_user_id: str) -> dict:
    response = httpx.post(
        _url("/api/extension/medhunt/conversations/assign"),
        headers={"X-Capture-Token": str(token or "").strip()},
        json={
            "conversation_id": str(conversation.get("id") or ""),
            "candidate_id": str(conversation.get("candidate_id") or ""),
            "nexus_candidate_id": str(conversation.get("nexus_candidate_id") or ""),
            "candidate_name": str(conversation.get("candidate_name") or ""),
            "recruiter_user_id": str(recruiter_user_id or ""),
        },
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def report_message_event(*, event_id: str, conversation: dict, event_type: str,
                         message_preview: str = "") -> bool:
    """Report asynchronous Zoom activity without exposing a user's session."""
    token = config.MEDHUNT_HEALTHBOARD_SERVICE_TOKEN
    if not enabled() or not token:
        return False
    response = httpx.post(
        _url("/api/extension/medhunt/events"),
        headers={"X-Medhunt-Service-Token": token},
        json={
            "event_id": str(event_id),
            "conversation_id": str(conversation.get("id") or ""),
            "candidate_id": str(conversation.get("candidate_id") or ""),
            "nexus_candidate_id": str(conversation.get("nexus_candidate_id") or ""),
            "candidate_name": str(conversation.get("candidate_name") or ""),
            "initiated_by_user_id": str(conversation.get("initiated_by") or ""),
            "assigned_recruiter_user_id": str(conversation.get("assigned_recruiter_id") or ""),
            "event_type": str(event_type),
            "message_preview": str(message_preview or "")[:240],
        },
        timeout=config.HEALTHBOARD_AUTH_TIMEOUT,
    )
    response.raise_for_status()
    return bool(response.json().get("recorded", True))
