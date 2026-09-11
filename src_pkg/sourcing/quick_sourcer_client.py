"""Quick Sourcer external contact lookup.

Quick Sourcer answers a name (plus an optional location) with a person's
contact details and the full public record behind them.  Behind its API a real
browser visits the underlying people-search site, so one uncached search takes
30-90 seconds; found records are cached locally so the panel can reopen them
instantly.

The API key is read only by this backend.  The browser extension calls the
local ``/quick-sourcer/*`` endpoints and never receives or stores the key.

A Quick Sourcer record is returned to the panel as its own clearly attributed
result.  It is deliberately never written into the candidate contact columns:
those stay reserved for the PDL/Enformion pipeline, whose trust policy decides
what may be reused for outreach.
"""
from __future__ import annotations

import re
import time

import httpx

from . import config, person_name, store

_PROVIDER = "quick_sourcer"
_MASKED_EMAIL_RE = re.compile(r"\*")
# Quick Sourcer scrapes live pages, so a failed page can surface as a
# "found" record built out of page furniture ("404 - Not Found", nav labels).
_SCRAPE_ARTIFACT_RE = re.compile(r"^\s*\d{3}\s*-\s*\w", re.IGNORECASE)


def _text(value) -> str:
    return " ".join(str(value or "").split())


def _identity_key(value) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _request_key(name: str, location: str) -> str:
    return f"find:{_identity_key(name)}|{_identity_key(location)}"


def _external_key(external_id) -> str:
    return f"id:{int(external_id)}"


def configured() -> bool:
    return bool(config.QUICK_SOURCER_ENABLED)


def status() -> dict:
    """Return internal readiness; public API code applies a stricter projection."""
    return {
        "enabled": bool(config.QUICK_SOURCER_ENABLED),
        "configured": bool(config.QUICK_SOURCER_API_KEY),
        "base_url": config.QUICK_SOURCER_BASE_URL,
        "typical_seconds": [30, 90],
    }


def _cached(request_key: str) -> dict | None:
    if not config.QUICK_SOURCER_CACHE_TTL_SECONDS:
        return None
    row = store.get_provider_lookup(_PROVIDER, request_key)
    if not row:
        return None
    age = time.time() - float(row.get("created") or 0)
    if age > config.QUICK_SOURCER_CACHE_TTL_SECONDS:
        return None
    result = row.get("result") or {}
    if result.get("status") != "found":
        return None
    return {**result, "cached": True}


def _remember(request_key: str, candidate_id: int, result: dict) -> None:
    """Cache a found record; a miss or an error must stay retryable."""
    if result.get("status") != "found":
        return
    store.save_provider_lookup(
        _PROVIDER, "external", int(candidate_id or 0), request_key, "found",
        {**result, "cached": False},
    )
    external_id = result.get("external_id")
    if external_id and request_key != _external_key(external_id):
        store.save_provider_lookup(
            _PROVIDER, "external", int(candidate_id or 0),
            _external_key(external_id), "found", {**result, "cached": False},
        )


def _empty(status_value: str, error: str = "") -> dict:
    return {
        "status": status_value,
        "error": error,
        "cached": False,
        "external_id": None,
        "name": "",
        "source": "",
        "emails": [],
        "masked_emails": [],
        "phones": [],
        "addresses": [],
        "current_address": {},
        "age": None,
        "born": "",
        "also_known_as": [],
        "employment": [],
        "education": [],
        "relatives": [],
        "associates": [],
        "businesses": [],
    }


def _phones(payload: dict, summary: dict) -> list[dict]:
    output, seen = [], set()
    rows = summary.get("phones") or (payload.get("profile") or {}).get("phones") or []
    for row in rows if isinstance(rows, list) else []:
        value = _text(row.get("number") if isinstance(row, dict) else row)
        if not value or value in seen:
            continue
        seen.add(value)
        details = row if isinstance(row, dict) else {}
        output.append({
            "value": value,
            "type": _text(details.get("type")),
            "carrier": _text(details.get("carrier")),
            "last_reported": _text(details.get("lastReported")),
            "primary": bool(details.get("isPrimary")),
        })
    fallback = _text(payload.get("phone"))
    if fallback and fallback not in seen:
        output.insert(0, {
            "value": fallback, "type": "", "carrier": "",
            "last_reported": "", "primary": True,
        })
    output.sort(key=lambda item: not item["primary"])
    return output[:40]


def _people(rows) -> list[dict]:
    output = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            entry = {
                "name": _text(row.get("name")),
                "age": row.get("age"),
                "relationship": _text(row.get("relationship")),
                "deceased": bool(row.get("deceased")),
            }
        else:
            entry = {"name": _text(row), "age": None, "relationship": "", "deceased": False}
        if entry["name"]:
            output.append(entry)
    return output[:60]


def _current_address(payload: dict) -> dict:
    profile = payload.get("profile") or {}
    current = profile.get("currentAddress")
    if isinstance(current, dict):
        return {
            "address": _text(current.get("address")),
            "county": _text(current.get("county")),
            "date_range": _text(current.get("dateRange")),
            "property_details": _text(current.get("propertyDetails")),
        }
    line = _text(current) or _text(payload.get("address"))
    if not line:
        return {}
    return {"address": line, "county": "", "date_range": "", "property_details": ""}


def normalize(payload: dict | None) -> dict:
    """Reshape one Quick Sourcer response into a single stable contract.

    ``summary`` is already source-independent, so it leads; ``profile`` fills
    in the richer per-site detail the panel shows underneath the contacts.
    """
    source_payload = payload if isinstance(payload, dict) else {}
    if not source_payload.get("found"):
        return _empty("not_found")

    summary = source_payload.get("summary") or {}
    profile = source_payload.get("profile") or {}
    emails, masked, seen = [], [], set()
    raw_emails = list(summary.get("emails") or [])
    if source_payload.get("email"):
        raw_emails.append(source_payload["email"])
    for value in raw_emails:
        address = _text(value)
        key = address.casefold()
        if not address or key in seen:
            continue
        seen.add(key)
        # searchpeoplefree publishes pre-masked addresses to non-paying
        # visitors. They are shown as evidence, never as a usable contact.
        (masked if _MASKED_EMAIL_RE.search(address) else emails).append(address)

    addresses, address_seen = [], set()
    for value in summary.get("addresses") or []:
        line = _text(value)
        if not line or _SCRAPE_ARTIFACT_RE.match(line):
            continue
        if line.casefold() not in address_seen:
            address_seen.add(line.casefold())
            addresses.append(line)

    result = _empty("found")
    result.update({
        "external_id": source_payload.get("candidate_id"),
        "name": _text(source_payload.get("name") or profile.get("fullName")),
        "source": _text(source_payload.get("source")),
        "emails": emails[:20],
        "masked_emails": masked[:20],
        "phones": _phones(source_payload, summary),
        "addresses": addresses[:40],
        "current_address": _current_address(source_payload),
        "age": profile.get("age"),
        "born": _text(profile.get("born") or profile.get("birthYear")),
        "also_known_as": [
            _text(value) for value in (summary.get("names") or [])[:20] if _text(value)
        ],
        "employment": [
            {
                "employer": _text(row.get("employer")),
                "title": _text(row.get("title")),
                "industry": _text(row.get("industry")),
                "from": _text(row.get("from")),
                "to": _text(row.get("to")),
                "location": _text(row.get("location")),
            }
            for row in (summary.get("experience") or []) if isinstance(row, dict)
        ][:20],
        "education": [
            {
                "institution": _text(row.get("institution")),
                "degree": _text(row.get("degree")),
            }
            for row in (summary.get("education") or []) if isinstance(row, dict)
        ][:20],
        "relatives": _people(profile.get("relatives")),
        "associates": _people(profile.get("associates")),
        "businesses": [
            {"name": _text(row.get("name")), "address": _text(row.get("address"))}
            for row in (profile.get("businesses") or []) if isinstance(row, dict)
        ][:20],
    })
    return result


def _call(method: str, path: str, payload: dict | None = None) -> dict:
    # Keep the configured timeout as one total budget. A gateway can return a
    # 502/503/504 early while the live-browser worker is being recycled; one
    # retry inside the remaining budget fixes that transient case without
    # allowing a single candidate to outlive the extension's request timeout.
    deadline = time.monotonic() + config.QUICK_SOURCER_TIMEOUT
    response = None
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            raise httpx.TimeoutException("Quick Sourcer request budget expired.")
        try:
            response = httpx.request(
                method,
                f"{config.QUICK_SOURCER_BASE_URL}{path}",
                json=payload,
                headers={"X-API-Key": config.QUICK_SOURCER_API_KEY},
                timeout=remaining,
            )
        except httpx.TransportError:
            if attempt or deadline - time.monotonic() <= 2:
                raise
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic() - 1)))
            continue
        if response.status_code not in {502, 503, 504} or attempt:
            break
        if deadline - time.monotonic() <= 2:
            break
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic() - 1)))

    if response is None:  # defensive; the loop either returns a response or raises
        raise httpx.TransportError("Quick Sourcer returned no response.")
    if response.status_code in (401, 403):
        raise PermissionError("Quick Sourcer rejected the configured API key.")
    if response.status_code == 404:
        return {"found": False}
    response.raise_for_status()
    body = response.json()
    return body if isinstance(body, dict) else {}


def _failure(exc: Exception) -> dict:
    if isinstance(exc, PermissionError):
        return _empty("error", str(exc))
    if isinstance(exc, httpx.TimeoutException):
        return _empty(
            "error",
            "Quick Sourcer did not answer in time. A live search can take 30-90 seconds.",
        )
    if isinstance(exc, httpx.HTTPStatusError):
        # The status is the only actionable detail here: a 5xx is the Hub or a
        # source site failing this one search, and it is worth retrying.
        return _empty(
            "error",
            f"Quick Sourcer returned HTTP {exc.response.status_code}. Try this candidate again.",
        )
    return _empty("error", f"Quick Sourcer request failed ({type(exc).__name__}).")


def _is_person(result: dict, searched_name: str = "") -> bool:
    """Reject a "found" record that is page furniture rather than a person.

    The upstream search answers with its single best guess and has no way to
    say "nobody like that". A blocked or missing source page can therefore come
    back as a record whose name is a nav label and whose only address is the
    site's own error line. Require something contactable, and — when the caller
    searched by name — require the answer to actually be about that name.
    """
    if result.get("status") != "found":
        return False
    if not (result.get("phones") or result.get("emails") or result.get("addresses")):
        return False
    if not searched_name:
        return True
    returned = set(person_name.identity_tokens(result.get("name") or ""))
    if not returned:
        return False
    return bool(returned & set(person_name.identity_tokens(searched_name)))


def find(name: str, location: str = "", candidate_id: int = 0, refresh: bool = False) -> dict:
    """Search Quick Sourcer for one person, reusing a cached record by default."""
    person = _text(name)
    if not person:
        return _empty("error", "A candidate name is required.")
    if not configured():
        return _empty("disabled", "Quick Sourcer is not configured on this backend.")

    request_key = _request_key(person, location)
    if not refresh:
        hit = _cached(request_key)
        if hit:
            return hit
    try:
        payload = _call("POST", "/find", {"name": person, "location": _text(location)})
    except Exception as exc:
        return _failure(exc)
    result = normalize(payload)
    if not _is_person(result, person):
        return _empty("not_found")
    _remember(request_key, candidate_id, result)
    return result


def fetch(external_id: int, refresh: bool = False) -> dict:
    """Re-read a person Quick Sourcer has already found. No new search runs."""
    try:
        identifier = int(external_id)
    except (TypeError, ValueError):
        return _empty("error", "A Quick Sourcer record id is required.")
    if not configured():
        return _empty("disabled", "Quick Sourcer is not configured on this backend.")

    request_key = _external_key(identifier)
    if not refresh:
        hit = _cached(request_key)
        if hit:
            return hit
    try:
        payload = _call("GET", f"/candidates/{identifier}")
    except Exception as exc:
        return _failure(exc)
    result = normalize(payload)
    if not _is_person(result):
        return _empty("not_found")
    _remember(request_key, 0, result)
    return result


# ---- candidate contact lookup ----
# When ``CONTACT_LOOKUP_PROVIDER`` selects Quick Sourcer, these functions answer
# the extension's lookups. Whatever the API delivers is stored and shown as-is:
# no identity threshold, no likelihood floor, no provider-trust gate. The one
# filter that still applies is the do-not-contact list, which is a compliance
# rule rather than a confidence judgement.
CONTACT_SOURCE = "quick_sourcer"


def phone_kind(phone: dict) -> str:
    """Map a reported line type onto the two kinds the panel renders."""
    return "mobile" if "wireless" in _text(phone.get("type")).casefold() else "other"


def public_lookup_result(result: dict | None) -> dict:
    """Return the browser's lookup contract, carrying the API's own values."""
    source = dict(result or {})
    status = str(source.get("status") or "error")
    if status == "found":
        emails = [str(value).strip() for value in source.get("emails") or [] if str(value).strip()]
        phone_rows = [
            {"value": _text(phone.get("value")), "kind": phone_kind(phone)}
            for phone in source.get("phones") or [] if _text(phone.get("value"))
        ]
        allowed = store.filter_dnc_groups({
            "emails": emails,
            "phones": [row["value"] for row in phone_rows],
        })
        allowed_phones = {store.contact_key(value) for value in allowed.get("phones") or []}
        phone_contacts = [
            row for row in phone_rows if store.contact_key(row["value"]) in allowed_phones
        ]
        emails = list(allowed.get("emails") or [])
        phones = [row["value"] for row in phone_contacts]
        found = bool(emails or phones)
    else:
        emails, phones, phone_contacts, found = [], [], [], False
    return {
        "status": "found" if found else ("failed" if status in ("error", "disabled") else "not_found"),
        "emails": emails[:20],
        "phones": phones[:40],
        "phone_contacts": phone_contacts[:40],
        # A usable phone or email is sufficient for Indeed resume capture.
        # Masked provider email labels remain display-only/non-contact data.
        "resume_required": bool(found and (emails or phones)),
        "location_match": None,
    }


def _stored_record(result: dict) -> dict:
    """Keep the reviewable detail beside the contacts, without the bulk."""
    return {
        "external_id": result.get("external_id"),
        "name": result.get("name"),
        "source_site": result.get("source"),
        "age": result.get("age"),
        "born": result.get("born"),
        "current_address": result.get("current_address") or {},
        "phones": result.get("phones") or [],
        "masked_emails": result.get("masked_emails") or [],
        "also_known_as": (result.get("also_known_as") or [])[:10],
        "employment": result.get("employment") or [],
        "education": result.get("education") or [],
    }


def apply_to_candidate(candidate_id: int, result: dict) -> dict:
    """Persist one Quick Sourcer record against a stored candidate."""
    public = public_lookup_result(result)
    found = public["status"] == "found"
    now = time.time()
    expires = now + max(3600, config.QUICK_SOURCER_CACHE_TTL_SECONDS or 0)
    verification = {
        "source": CONTACT_SOURCE,
        "identity_status": "public_record",
        "checked_at": now,
        "record": _stored_record(result) if result.get("status") == "found" else {},
        "evidence": {
            "contact_source": CONTACT_SOURCE,
            "source_site": result.get("source") or "",
            "external_id": result.get("external_id"),
            "lookup_status": result.get("status"),
            "error": result.get("error") or "",
        },
    }
    store.update_candidate(
        candidate_id,
        emails=public["emails"],
        phones=public["phones"],
        addresses=list(result.get("addresses") or [])[:40] if found else [],
        enrich_status="success" if found else (
            "error" if public["status"] == "failed" else "no_match"
        ),
        identity_provider=CONTACT_SOURCE,
        verification=verification,
        contact_verified_at=now if found else 0,
        contact_expires_at=expires if found else 0,
    )
    return public


def lookup_candidate(candidate_id: int, refresh: bool = False) -> dict:
    """Look one stored candidate up through Quick Sourcer and save the answer.

    An uncached search takes 30-90 seconds because the API drives a real
    browser, so callers must run these one at a time rather than in parallel.
    """
    candidate = store.get_candidate(candidate_id)
    if not candidate:
        return {"status": "failed", "emails": [], "phones": [], "phone_contacts": [],
                "resume_required": False, "location_match": None}
    name = person_name.normalize_person_name(candidate.get("name") or "") or str(
        candidate.get("name") or ""
    )
    result = find(
        name,
        str(candidate.get("location") or ""),
        candidate_id=candidate_id,
        refresh=refresh,
    )
    return apply_to_candidate(candidate_id, result)
