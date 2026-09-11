"""
Enformion / Endato Person Search client.

Takes a candidate name (+ optional location) and returns phone/email/address.
Includes a deterministic DEMO mode so the whole product runs before you wire the
real API key. Never scrapes anything — this is a licensed HTTPS data call.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from . import config, person_name, phone_policy, verification


_IDENTITY_SCORE_MIN = 0.72
_IDENTITY_WINNER_MARGIN = 0.08
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
_CONTEXT_STOP_WORDS = {
    "a", "an", "and", "at", "company", "corp", "corporation", "for",
    "health", "healthcare", "inc", "llc", "of", "services", "the",
}

# Kept as module-level callables so rate-limit tests can replace time without
# performing a real wait or depending on the wall clock.
_sleep = time.sleep
# One credential can be shared by several browser/backend requests in the same
# process.  Serialize the complete retry window so two lookup runs cannot
# create a new burst while either one is already honoring a provider throttle.
_HTTP_REQUEST_LOCK = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clean_phone(raw: str) -> str:
    d = re.sub(r"\D", "", str(raw or ""))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return f"({d[0:3]}) {d[3:6]}-{d[6:10]}" if len(d) == 10 else str(raw or "").strip()


def _dedupe(seq):
    seen, out = set(), []
    for s in seq:
        if not s:
            continue
        k = re.sub(r"\D", "", s) if any(c.isdigit() for c in s) else s.lower()
        if k and k not in seen:
            seen.add(k); out.append(s)
    return out


def _opaque_id(value) -> str:
    """Return an opaque provider identifier with outer whitespace removed.

    Tahoe/Person IDs are not human text: their case and internal characters
    can be significant.  Do not case-fold or whitespace-normalize them.
    """
    if not isinstance(value, (str, int, float)):
        return ""
    return str(value).strip()


def _dedupe_text(seq):
    """Deduplicate opaque provider identifiers using exact equality."""
    seen, output = set(), []
    for value in seq:
        text = _opaque_id(value)
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output


def _get(value: dict, *names):
    """Return a mapping value while tolerating provider casing variants."""
    if not isinstance(value, dict):
        return None
    for name in names:
        if name in value:
            return value[name]
    wanted = {re.sub(r"[^a-z0-9]", "", str(name).casefold()) for name in names}
    for key, item in value.items():
        normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
        if normalized in wanted:
            return item
    return None


def _values(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _text(value) -> str:
    if isinstance(value, (str, int, float)):
        return re.sub(r"\s+", " ", str(value)).strip()
    return ""


def _split_name(full: str):
    parts = (full or "").strip().split()
    # Suffixes are identity evidence, not surnames.  Keep them out of the
    # discovery fields and validate an explicit suffix on the returned record.
    if len(parts) >= 3 and person_name.is_name_suffix(parts[-1]):
        parts.pop()
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        return parts[0], "", ""
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return parts[0], " ".join(parts[1:-1]), parts[-1]


def _request_name(value: str) -> dict | None:
    normalized = person_name.normalize_person_name(value)
    first, middle, last = _split_name(normalized)
    if not first or not last:
        return None
    return {
        "FirstName": first,
        "MiddleName": middle,
        "LastName": last,
    }


def _request_names(values, *, exclude="") -> list[dict]:
    excluded = _normal_name(exclude)
    output, seen = [], set()
    for value in values or []:
        item = _request_name(str(value or ""))
        if not item:
            continue
        key = _normal_name(" ".join(filter(None, item.values())))
        if not key or key == excluded or key in seen:
            continue
        seen.add(key)
        output.append(item)
        if len(output) >= 8:
            break
    return output


def _demo_enrich(name: str, location: str) -> dict:
    """Deterministic fake data keyed off the name so demos are stable."""
    h = int(hashlib.sha256((name + location).encode()).hexdigest(), 16)
    area = 200 + (h % 700)
    mid = 200 + (h // 7 % 700)
    last = h % 10000
    handle = re.sub(r"[^a-z]", ".", name.lower()).strip(".")
    mobile = _clean_phone(f"{area}{mid:03d}{last:04d}"[:10])
    return {
        "status": "success",
        "matched_name": name,
        "phones": [mobile],
        "emails": [f"{handle}@example.com"],
        "addresses": [f"{100 + h % 9900} Main St, {location or 'Atlanta, GA'}"],
        "confidence": round(0.6 + (h % 40) / 100, 2),
        "source": "enformion (demo)",
        "phone_policy": phone_policy.MOBILE_PHONE_POLICY,
        "phone_evidence": [{
            "value": mobile, "type": "Wireless", "is_connected": True,
            "mobile_or_wireless": True, "accepted": True,
        }],
    }


def configured() -> bool:
    return bool(config.ENFORMION_AP_NAME and config.ENFORMION_AP_PASSWORD)


def enabled() -> bool:
    return bool(config.ENFORMION_ENABLED and configured() and not config.DEMO_MODE)


def _retry_after_seconds(response) -> float | None:
    """Parse an HTTP Retry-After header as seconds from now.

    The HTTP field permits either delay-seconds or an HTTP date. Malformed and
    negative values are ignored so they cannot create an unbounded wait.
    """
    headers = getattr(response, "headers", None) or {}
    raw = ""
    try:
        raw = str(headers.get("Retry-After") or headers.get("retry-after") or "").strip()
    except Exception:  # noqa: BLE001 - provider/test header objects vary
        return None
    if not raw:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(raw)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            seconds = (target.astimezone(timezone.utc) - _utcnow()).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds)


def _post_with_rate_limit(client, *, body: dict, headers: dict):
    """POST Person Search with bounded, Retry-After-aware 429 retries.

    Returned metadata contains only counts and timing information. It never
    includes request headers, provider credentials, URL, or request payload.
    """
    max_retries = config.ENFORMION_RATE_LIMIT_RETRIES
    base_backoff = config.ENFORMION_RATE_LIMIT_BACKOFF_SECONDS
    max_wait = config.ENFORMION_RATE_LIMIT_MAX_WAIT_SECONDS
    metadata = {
        "rate_limited": False,
        "rate_limit_retries": 0,
        "rate_limit_wait_seconds": 0.0,
        "request_attempts": 0,
    }
    response = None
    for retry_index in range(max_retries + 1):
        response = client.post(config.ENFORMION_URL, json=body, headers=headers)
        metadata["request_attempts"] += 1
        if response.status_code != 429:
            break

        metadata["rate_limited"] = True
        provider_delay = _retry_after_seconds(response)
        delay = (
            provider_delay
            if provider_delay is not None
            else base_backoff * (2 ** retry_index)
        )
        delay = max(0.0, float(delay))
        if provider_delay is not None:
            metadata["retry_after_seconds"] = round(provider_delay, 3)

        # Do not violate a Retry-After value that is beyond our bounded wait:
        # stop and let the caller retry the operation later instead.
        if retry_index >= max_retries:
            break
        if delay > max_wait:
            metadata["retry_deferred"] = True
            metadata["next_retry_delay_seconds"] = round(delay, 3)
            break

        _sleep(delay)
        metadata["rate_limit_wait_seconds"] += delay
        metadata["rate_limit_retries"] += 1

    metadata["rate_limit_wait_seconds"] = round(
        metadata["rate_limit_wait_seconds"], 3
    )
    return response, metadata


def enrich(
    name: str, location: str = "", *, phone: str = "", email: str = "",
    aliases=None, companies=None, schools=None, roles=None, relatives=None,
    birth_date: str = "", age=None,
) -> dict:
    """Return {status, matched_name, phones[], emails[], addresses[], confidence, source, error?}."""
    name = person_name.normalize_person_name(name)
    location = verification.us_city_state(location) or str(location or "").strip()
    if not name:
        return {"status": "error", "error": "Missing name", "phones": [], "emails": [], "addresses": []}

    if config.DEMO_MODE:
        return _demo_enrich(name, location)

    first, _middle, last = _split_name(name)
    # This account is licensed for Person Search. The reference implementation
    # confirms that location must be in Addresses[].AddressLine2.
    body = {
        # Middle names are inconsistently present across recruiting sites and
        # provider records.  They must never narrow the discovery request;
        # returned middles remain available as ranking evidence.
        "FirstName": first, "MiddleName": "", "LastName": last,
        "Page": 1, "ResultsPerPage": 10,
    }
    clean_phone = str(phone or "").strip()
    clean_email = str(email or "").strip()
    if clean_phone:
        body["Phone"] = clean_phone
    if clean_email:
        body["Email"] = clean_email
    if location:
        body["Addresses"] = [{"AddressLine1": "", "AddressLine2": location}]
    request_aliases = _request_names(aliases, exclude=name)
    request_relatives = _request_names(relatives)
    if request_aliases:
        body["Akas"] = request_aliases
    if request_relatives:
        body["Relatives"] = request_relatives
    request_fields = ["name"]
    if location:
        request_fields.append("location")
    if clean_phone:
        request_fields.append("phone")
    if clean_email:
        request_fields.append("email")
    if len(request_fields) < 2:
        return {
            "status": "skipped",
            "error": "Enformion Person Search requires a full name and location.",
            "phones": [], "emails": [], "addresses": [],
            "request_fields": request_fields, "source": "enformion",
        }
    headers = {
        "galaxy-ap-name": config.ENFORMION_AP_NAME,
        "galaxy-ap-password": config.ENFORMION_AP_PASSWORD,
        "galaxy-search-type": config.ENFORMION_SEARCH_TYPE,
        "galaxy-client-type": "Python",
        "Content-Type": "application/json", "Accept": "application/json",
    }
    completed_http_200 = False
    retry_metadata = {}
    try:
        with _HTTP_REQUEST_LOCK:
            with httpx.Client(timeout=config.HTTP_TIMEOUT) as c:
                r, retry_metadata = _post_with_rate_limit(
                    c, body=body, headers=headers,
                )
        if r.status_code in (401, 403):
            return {"status": "error", "error": f"Enformion auth failed ({r.status_code})",
                    "phones": [], "emails": [], "addresses": [],
                    "http_status": r.status_code, "credits_spent": 0,
                    **retry_metadata}
        if r.status_code >= 400:
            error_code = ""
            error_message = ""
            try:
                error_payload = r.json()
                provider_error = error_payload.get("error") or {}
                if isinstance(provider_error, dict):
                    error_code = str(provider_error.get("code") or "").strip()
                    error_message = str(
                        provider_error.get("message")
                        or provider_error.get("description")
                        or provider_error.get("details")
                        or ""
                    ).strip()
            except Exception:
                error_message = r.text.strip()
            detail = ": ".join(value for value in (error_code, error_message) if value)
            return {
                "status": "error",
                "error": f"Enformion HTTP {r.status_code}{': ' + detail[:600] if detail else ''}",
                "provider_error_code": error_code[:120],
                "phones": [], "emails": [], "addresses": [],
                "http_status": r.status_code,
                "retryable": r.status_code == 429,
                "credits_spent": 0,
                **retry_metadata,
            }
        completed_http_200 = r.status_code == 200
        result = _map_search(
            r.json(), name, location, request_fields=request_fields,
            expected_aliases=aliases, expected_companies=companies,
            expected_schools=schools, expected_roles=roles,
            expected_relatives=relatives, expected_birth_date=birth_date,
            expected_age=age,
        )
        # The Automater reference's Person Search allowance counts each
        # completed HTTP-200 search, including searches with no exact person.
        result["credits_spent"] = 1
        result.update(retry_metadata)
        return result
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"{type(e).__name__}: {e}",
                "phones": [], "emails": [], "addresses": [],
                "credits_spent": 1 if completed_http_200 else 0,
                **retry_metadata}


def resolve_tahoe_id(
    tahoe_id: str, name: str, location: str = "", *, aliases=None,
    companies=None, schools=None, roles=None, relatives=None,
    birth_date: str = "", age=None,
) -> dict:
    """Resolve a relative pivot to its own record through Person Search.

    The household result is intentionally not accepted as contact evidence.
    This second request uses only the stable Tahoe ID for discovery and then
    independently validates the returned target's own name and location.
    """
    target_id = _opaque_id(tahoe_id)
    name = person_name.normalize_person_name(name)
    location = verification.us_city_state(location) or str(location or "").strip()
    if not target_id or not name or not location:
        return {
            "status": "skipped", "source": "enformion",
            "error": "A Tahoe ID, full name, and city/state are required.",
            "phones": [], "emails": [], "addresses": [],
            "relative_bridge_used": False, "household_contact_used": False,
        }
    if config.DEMO_MODE:
        return {
            "status": "skipped", "source": "enformion (demo)",
            "error": "Relative Tahoe resolution is unavailable in demo mode.",
            "phones": [], "emails": [], "addresses": [],
            "relative_bridge_used": False, "household_contact_used": False,
        }
    body = {"TahoeIds": [target_id], "Page": 1, "ResultsPerPage": 10}
    headers = {
        "galaxy-ap-name": config.ENFORMION_AP_NAME,
        "galaxy-ap-password": config.ENFORMION_AP_PASSWORD,
        "galaxy-search-type": config.ENFORMION_SEARCH_TYPE,
        "galaxy-client-type": "Python",
        "Content-Type": "application/json", "Accept": "application/json",
    }
    completed_http_200 = False
    retry_metadata = {}
    try:
        with _HTTP_REQUEST_LOCK:
            with httpx.Client(timeout=config.HTTP_TIMEOUT) as client:
                response, retry_metadata = _post_with_rate_limit(
                    client, body=body, headers=headers,
                )
        if response.status_code in (401, 403):
            return {
                "status": "error", "source": "enformion",
                "error": f"Enformion auth failed ({response.status_code})",
                "phones": [], "emails": [], "addresses": [],
                "relative_bridge_used": False, "household_contact_used": False,
                "http_status": response.status_code, "credits_spent": 0,
                **retry_metadata,
            }
        if response.status_code >= 400:
            detail = ""
            try:
                payload = response.json()
                provider_error = payload.get("error") or payload.get("Error") or {}
                if isinstance(provider_error, dict):
                    detail = _text(_get(provider_error, "message", "description", "details"))
                else:
                    detail = _text(provider_error)
            except Exception:
                detail = response.text.strip()
            return {
                "status": "error", "source": "enformion",
                "error": f"Enformion HTTP {response.status_code}{': ' + detail[:600] if detail else ''}",
                "phones": [], "emails": [], "addresses": [],
                "relative_bridge_used": False, "household_contact_used": False,
                "http_status": response.status_code,
                "retryable": response.status_code == 429,
                "credits_spent": 0,
                **retry_metadata,
            }
        completed_http_200 = response.status_code == 200
        result = _map_tahoe_search(
            response.json(), target_id, name, location,
            request_fields=["tahoe_id"], expected_aliases=aliases,
            expected_companies=companies, expected_schools=schools,
            expected_roles=roles, expected_relatives=relatives,
            expected_birth_date=birth_date, expected_age=age,
        )
        result["credits_spent"] = 1
        result.update(retry_metadata)
        return result
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error", "source": "enformion",
            "error": f"{type(exc).__name__}: {exc}",
            "phones": [], "emails": [], "addresses": [],
            "relative_bridge_used": False, "household_contact_used": False,
            "credits_spent": 1 if completed_http_200 else 0,
            **retry_metadata,
        }


def _person(data: dict) -> dict:
    person = data.get("person") or data.get("Person")
    if isinstance(person, dict):
        return person
    people = data.get("people") or data.get("People") or data.get("results") or []
    if isinstance(people, list) and people and isinstance(people[0], dict):
        return people[0].get("person") or people[0].get("Person") or people[0]
    return data if isinstance(data, dict) else {}


def _people(data: dict) -> list[dict]:
    values = _get(data, "persons", "people", "results") or []
    if not isinstance(values, list):
        return []
    output = []
    for item in values:
        if not isinstance(item, dict):
            continue
        nested = _get(item, "person")
        output.append(nested if isinstance(nested, dict) else item)
    return _consolidate_people(output)


def _name_value(value) -> str:
    if isinstance(value, (str, int, float)):
        return re.sub(r"\s+", " ", str(value)).strip()
    if not isinstance(value, dict):
        return ""
    direct = _get(value, "fullName", "displayName", "formattedName")
    if direct is not None and direct is not value:
        resolved = _name_value(direct)
        if resolved:
            return resolved
    nested = _get(value, "name")
    if nested is not None and nested is not value:
        resolved = _name_value(nested)
        if resolved:
            return resolved
    parts = [
        _text(_get(value, "firstName", "givenName", "first")),
        _text(_get(value, "middleName", "middle")),
        _text(_get(value, "lastName", "familyName", "surname", "last")),
    ]
    return re.sub(r"\s+", " ", " ".join(part for part in parts if part)).strip()


def _matched_name(person: dict) -> str:
    # Never substitute the requested name when the provider omitted its
    # returned identity. That would manufacture an apparent exact-name match.
    return _name_value(person)


def _person_aliases(person: dict) -> list[str]:
    output = []
    sources = [person]
    nested_name = _get(person, "name")
    if isinstance(nested_name, dict):
        sources.append(nested_name)
    for source in sources:
        for key in (
            "akas", "aliases", "mergedNames", "otherNames", "previousNames",
            "alternateNames", "maidenNames",
        ):
            for value in _values(_get(source, key)):
                name = _name_value(value)
                if name:
                    output.append(name)
    primary_key = _normal_name(_matched_name(person))
    return [
        value for value in _dedupe(output)
        if _normal_name(value) and _normal_name(value) != primary_key
    ][:30]


def _normal_name(value: str) -> str:
    return " ".join(person_name.identity_tokens(value))


def _name_tokens(value: str) -> list[str]:
    parts = list(person_name.identity_tokens(value))
    while parts and parts[-1] in _NAME_SUFFIXES:
        parts.pop()
    return parts


def _name_suffix(value: str) -> str:
    return str(person_name.identity_signature(value).get("suffix") or "")


def _one_name_evidence(expected: str, returned: str) -> dict:
    """Match exact first and terminal surname while allowing arbitrary middles.

    A surname token merely appearing in the middle is deliberately not enough:
    ``Jane Lee`` must not automatically become ``Jane Lee Smith``.
    """
    left_signature = person_name.identity_signature(expected)
    right_signature = person_name.identity_signature(returned)
    left, right = list(left_signature["tokens"]), list(right_signature["tokens"])
    left_surname = tuple(left_signature["surname"])
    right_surname = tuple(right_signature["surname"])
    if not left_signature["first"] or not right_signature["first"] \
            or not left_surname or not right_surname:
        return {"matched": False, "score": 0.0}
    expected_suffix = str(left_signature.get("suffix") or "")
    returned_suffix = str(right_signature.get("suffix") or "")
    suffix_conflict = bool(
        expected_suffix and returned_suffix and expected_suffix != returned_suffix
    )
    first_exact = left_signature["first"] == right_signature["first"]
    surname_present = any(
        tuple(right[index:index + len(left_surname)]) == left_surname
        for index in range(1, max(1, len(right) - len(left_surname) + 1))
    )
    # Some providers flatten a hyphenated surname into separate space-delimited
    # tokens and therefore classify its leading token as a middle. Preserve
    # the complete captured surname as one terminal tuple in either format.
    surname_final = bool(
        left_surname == right_surname
        or (
            len(left_surname) > 1 and len(right) > len(left_surname)
            and tuple(right[-len(left_surname):]) == left_surname
        )
    )
    expected_middle = tuple(left_signature["middle"])
    returned_middle = tuple(right_signature["middle"])
    middle_exact = bool(expected_middle and expected_middle == returned_middle)
    matched = bool(first_exact and surname_final and not suffix_conflict)
    return {
        "matched": matched,
        "score": 1.0 if matched else 0.0,
        "first_exact": first_exact,
        "surname_present": surname_present,
        "surname_final": surname_final,
        "middle_exact": middle_exact,
        "expected_suffix": expected_suffix,
        "returned_suffix": returned_suffix,
        "suffix_conflict": suffix_conflict,
    }


def _name_evidence(person: dict, expected_name: str, expected_aliases=None) -> dict:
    primary = _matched_name(person)
    returned_names = [(primary, "canonical")]
    returned_names.extend((value, "provider_alias") for value in _person_aliases(person))
    expected_names = [(expected_name, "source_name")]
    expected_names.extend((value, "source_alias") for value in (expected_aliases or []))
    best = {
        "matched": False, "score": 0.0, "matched_name": "",
        "matched_via": "", "expected_name": "", "middle_exact": False,
    }
    for expected, expected_type in expected_names:
        if not _normal_name(expected):
            continue
        for returned, returned_type in returned_names:
            if not returned:
                continue
            evidence = _one_name_evidence(expected, returned)
            score = float(evidence.get("score") or 0)
            if returned_type == "provider_alias":
                score = max(0.0, score - 0.04)
            if expected_type == "source_alias":
                score = max(0.0, score - 0.02)
            if evidence.get("matched") and score > float(best.get("score") or 0):
                best = {
                    **evidence, "score": round(score, 3),
                    "matched_name": returned, "matched_via": returned_type,
                    "expected_name": expected, "expected_type": expected_type,
                }
    return best


def _year(value) -> int:
    match = re.search(r"\b(19|20)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else 0


def _address_records(person: dict) -> list[dict]:
    output, seen = [], set()
    values = []
    for key in ("addresses", "locations", "addressHistory", "previousAddresses"):
        values.extend(_values(_get(person, key)))
    for index, value in enumerate(values):
        line = _address_line(value) if isinstance(value, dict) else _text(value)
        key = re.sub(r"[^a-z0-9]", "", line.casefold())
        if not line or not key or key in seen:
            continue
        seen.add(key)
        current_raw = _get(value, "isCurrent", "current", "currentAddress") if isinstance(value, dict) else None
        current = _provider_true(current_raw) if current_raw is not None else None
        first_reported = _text(_get(value, "firstReportedDate", "firstReported", "firstSeen")) \
            if isinstance(value, dict) else ""
        last_reported = _text(_get(value, "lastReportedDate", "lastReported", "lastSeen")) \
            if isinstance(value, dict) else ""
        output.append({
            "value": line, "is_current": current,
            "first_reported": first_reported[:40], "last_reported": last_reported[:40],
            "last_reported_year": _year(last_reported), "provider_order": index,
        })
    return output


def _address_recency(record: dict) -> tuple[float, str]:
    if record.get("is_current") is True:
        return 1.0, "current"
    year = int(record.get("last_reported_year") or 0)
    age = max(0, date.today().year - year) if year else None
    if age is not None and age <= 2:
        return 0.88, "recent"
    if age is not None and age <= 5:
        return 0.78, "recent"
    if age is not None:
        return 0.62, "historical"
    return (0.80, "undated_primary") if record.get("provider_order") == 0 else (0.68, "undated")


def _location_evidence(person: dict, expected_location: str) -> dict:
    best = {
        "matched": False, "exact": False, "state_match": False,
        "score": 0.0, "address": "", "address_kind": "",
    }
    for record in _address_records(person):
        evidence = verification.location_evidence(expected_location, [record["value"]])
        recency, kind = _address_recency(record)
        exact = bool(evidence.get("exact"))
        state_match = bool(evidence.get("state_match"))
        score = recency if exact else (recency * 0.62 if state_match else 0.0)
        rank = (1 if exact else 0, score)
        best_rank = (1 if best.get("exact") else 0, float(best.get("score") or 0))
        if state_match and rank > best_rank:
            best = {
                "matched": True, "exact": exact, "state_match": state_match,
                "score": round(score, 3), "address": record["value"],
                "address_kind": kind, "is_current": record.get("is_current"),
                "last_reported": record.get("last_reported") or "",
            }
    return best


def _container_values(person: dict, container_names, value_names) -> list[str]:
    output = []

    def visit(value, depth=0):
        if depth > 4:
            return
        if isinstance(value, (str, int, float)):
            text = _text(value)
            if text:
                output.append(text)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                visit(item, depth + 1)
            return
        if not isinstance(value, dict):
            return
        found = False
        for name in value_names:
            item = _get(value, name)
            if isinstance(item, (str, int, float)) and _text(item):
                output.append(_text(item)); found = True
        if not found:
            for item in value.values():
                if isinstance(item, (dict, list, tuple)):
                    visit(item, depth + 1)

    for name in container_names:
        container = _get(person, name)
        if container is not None:
            visit(container)
    return _dedupe(output)[:50]


def _person_workplaces(person: dict) -> list[str]:
    return _container_values(
        person,
        ("workplaces", "workplaceSummary", "employment", "employments", "jobs", "workHistory"),
        ("companyName", "employerName", "organizationName", "businessName", "company", "employer", "name"),
    )


def _person_roles(person: dict) -> list[str]:
    return _container_values(
        person,
        ("workplaces", "workplaceSummary", "employment", "employments", "jobs", "workHistory"),
        ("jobTitle", "title", "occupation", "position"),
    )


def _person_schools(person: dict) -> list[str]:
    return _container_values(
        person,
        ("education", "educations", "schools", "educationSummary"),
        ("schoolName", "institutionName", "organizationName", "school", "institution", "name"),
    )


def _id_values(value: dict, names) -> list[str]:
    """Read explicit identifier fields without interpreting IDs as names."""
    output = []
    if not isinstance(value, dict):
        return output
    for field in names:
        for item in _values(_get(value, field)):
            if isinstance(item, dict):
                item = _get(item, "id", "value", "tahoeId", "personId")
            text = _opaque_id(item)
            if text:
                output.append(text[:160])
    return _dedupe_text(output)[:20]


def _provider_score(value: dict) -> float | None:
    for field in ("matchScore", "relativeScore", "confidence", "score"):
        raw = _get(value, field)
        try:
            score = float(raw)
        except (TypeError, ValueError):
            continue
        if score > 1:
            score /= 100
        return round(max(0.0, min(1.0, score)), 3)
    return None


def _relative_record(value) -> dict | None:
    if isinstance(value, str):
        name = person_name.normalize_person_name(value)
        return {"name": name} if len(_name_tokens(name)) >= 2 else None
    if not isinstance(value, dict):
        return None

    nested = _get(value, "person", "relativePerson", "relative")
    identity = nested if isinstance(nested, dict) else value
    name = _name_value(identity) or _name_value(value)
    if len(_name_tokens(name)) < 2:
        name = ""
    tahoe_ids = _dedupe_text([
        *_id_values(value, ("tahoeId", "tahoeIds", "relativeTahoeId", "relativeTahoeIds")),
        *_id_values(identity, ("tahoeId", "tahoeIds")),
    ])[:20]
    person_ids = _dedupe_text([
        *_id_values(value, ("personId", "personIds", "relativePersonId", "relativePersonIds")),
        *_id_values(identity, ("personId", "personIds", "id")),
    ])[:20]
    household_ids = _dedupe_text([
        *_id_values(value, (
            "sharedHouseholdId", "sharedHouseholdIds", "householdId", "householdIds",
        )),
        *_id_values(identity, (
            "sharedHouseholdId", "sharedHouseholdIds", "householdId", "householdIds",
        )),
    ])[:20]
    birth_date = _person_birth_date(identity) or _person_birth_date(value)
    relationship = _text(_get(
        value, "relationship", "relationshipType", "relativeType", "relation",
    ))[:80]
    score = _provider_score(value)
    # A wrapper node is not a relative merely because it contains child lists.
    if not (name or tahoe_ids or person_ids):
        return None
    return {
        "name": name,
        "tahoe_ids": tahoe_ids,
        "person_ids": person_ids,
        "birth_date": birth_date,
        "age": _person_age(identity),
        "relationship": relationship,
        "shared_household_ids": household_ids,
        "provider_score": score,
    }


def _person_relative_records(person: dict) -> list[dict]:
    """Return provider relatives as typed records, never loose recursive text.

    Recursive string walking used to risk treating a UUID/Tahoe identifier as
    a two-token human name.  Only explicit relative containers and explicit
    identity fields are now parsed.
    """
    output, seen = [], set()

    def visit(value, depth=0):
        if depth > 3 or value is None:
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                visit(item, depth + 1)
            return
        if isinstance(value, str):
            record = _relative_record(value)
            if record:
                add(record)
            return
        if not isinstance(value, dict):
            return
        record = _relative_record(value)
        if record:
            add(record)
            return
        # Tolerate response wrappers such as {"items": [...]} without
        # descending into the fields of an already recognized person record.
        for item in value.values():
            if isinstance(item, (dict, list, tuple)):
                visit(item, depth + 1)

    def add(record):
        key = (
            _normal_name(record.get("name") or ""),
            tuple(record.get("tahoe_ids") or ()),
            tuple(record.get("person_ids") or ()),
        )
        if key != ("", (), ()) and key not in seen:
            seen.add(key)
            output.append(record)

    for field in ("relatives", "relativesSummary", "familyMembers", "family"):
        container = _get(person, field)
        if container is not None:
            visit(container)
    return output[:50]


def _person_relatives(person: dict) -> list[str]:
    return _dedupe([
        record.get("name") or "" for record in _person_relative_records(person)
    ])[:50]


def _context_tokens(value: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", str(value or "").casefold())
        if token not in _CONTEXT_STOP_WORDS
    }


def _value_similarity(left: str, right: str) -> float:
    left_text = " ".join(sorted(_context_tokens(left)))
    right_text = " ".join(sorted(_context_tokens(right)))
    if not left_text or not right_text:
        return 0.0
    if left_text == right_text:
        return 1.0
    left_tokens, right_tokens = set(left_text.split()), set(right_text.split())
    overlap = len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))
    return round(overlap, 3) if overlap >= 0.5 else 0.0


def _best_similarity(expected, returned) -> tuple[float, str, str]:
    best = (0.0, "", "")
    for left in expected or []:
        for right in returned or []:
            score = _value_similarity(left, right)
            if score > best[0]:
                best = (score, str(left), str(right))
    return best


def _relative_similarity(expected, returned) -> tuple[float, str, str]:
    """Relatives corroborate only when the source independently supplied one."""
    best = (0.0, "", "")
    for left in expected or []:
        for right in returned or []:
            evidence = _one_name_evidence(left, right)
            score = float(evidence.get("score") or 0) if evidence.get("matched") else 0.0
            if score > best[0]:
                best = (score, str(left), str(right))
    return best


def _person_birth_date(person: dict) -> str:
    value = _get(person, "dob", "dateOfBirth", "birthDate")
    if isinstance(value, dict):
        value = _get(value, "date", "value", "formattedDate")
    return _text(value)[:40]


def _person_age(person: dict):
    value = _get(person, "age")
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _birth_evidence(person: dict, expected_birth_date="", expected_age=None) -> float:
    returned_dob = _person_birth_date(person)
    if expected_birth_date and returned_dob:
        left, right = re.sub(r"\D", "", str(expected_birth_date)), re.sub(r"\D", "", returned_dob)
        if left and right and (left == right or (_year(left) and _year(left) == _year(right))):
            return 1.0
    returned_age = _person_age(person)
    try:
        if expected_age is not None and returned_age is not None and abs(int(expected_age) - returned_age) <= 1:
            return 0.85
    except (TypeError, ValueError):
        pass
    return 0.0


def _identity_evidence(
    person: dict, expected_name: str, expected_location: str, *,
    expected_aliases=None, expected_companies=None, expected_schools=None,
    expected_roles=None, expected_relatives=None, expected_birth_date="", expected_age=None,
) -> dict:
    name = _name_evidence(person, expected_name, expected_aliases)
    location = _location_evidence(person, expected_location)
    company = _best_similarity(expected_companies, _person_workplaces(person))
    school = _best_similarity(expected_schools, _person_schools(person))
    role = _best_similarity(expected_roles, _person_roles(person))
    relative = _relative_similarity(expected_relatives, _person_relatives(person))
    birth = _birth_evidence(person, expected_birth_date, expected_age)
    context_score = max(company[0], school[0], role[0])
    # A generic title such as "Registered Nurse" may rank otherwise eligible
    # people, but it must not by itself overcome a city mismatch.
    independent_corroborator = max(
        company[0], school[0], relative[0], birth,
        1.0 if name.get("middle_exact") else 0.0,
    )
    alias_match = bool(
        name.get("matched_via") == "provider_alias"
        or name.get("expected_type") == "source_alias"
    )
    # Same-state alone is not sufficient to loosen a city mismatch. It must be
    # accompanied by independently supplied work/school/middle/DOB/relative
    # evidence. Provider-returned relatives never score without an expected
    # source relative.
    admissible = bool(
        name.get("matched") and location.get("state_match")
        and (location.get("exact") or independent_corroborator >= 0.6)
        # An explicit source alias or provider AKA is sufficient when the
        # returned person also has the exact captured city/state.  A weaker
        # state-only or historical-location match still needs an independent
        # employer, school, DOB, source relative, or exact middle corroborator.
        and (
            not alias_match or location.get("exact")
            or independent_corroborator >= 0.6
        )
    )
    score = round(
        (float(name.get("score") or 0) * 0.50)
        + (float(location.get("score") or 0) * 0.32)
        + (context_score * 0.10)
        + ((1.0 if name.get("middle_exact") else 0.0) * 0.03)
        + (birth * 0.03)
        + (relative[0] * 0.02),
        3,
    )
    minimum_score = 0.70 if relative[0] >= 0.9 and location.get("state_match") else _IDENTITY_SCORE_MIN
    return {
        "admissible": admissible and score >= minimum_score,
        "score": score, "name": name, "location": location,
        "alias_match": alias_match,
        "independent_corroborator": independent_corroborator,
        "company": {"score": company[0], "expected": company[1], "returned": company[2]},
        "school": {"score": school[0], "expected": school[1], "returned": school[2]},
        "role": {"score": role[0], "expected": role[1], "returned": role[2]},
        "relative": {"score": relative[0], "expected": relative[1], "returned": relative[2]},
        "birth_score": birth,
    }


def _identity_rank(person: dict, expected_name: str, expected_location: str) -> tuple:
    """Backward-compatible compact rank; contact volume never affects it."""
    evidence = _identity_evidence(person, expected_name, expected_location)
    return (
        1 if evidence["admissible"] else 0,
        float(evidence["score"]),
        float((evidence.get("name") or {}).get("score") or 0),
        float((evidence.get("location") or {}).get("score") or 0),
    )


def _select_person(
    data: dict, expected_name: str, expected_location: str, *,
    expected_aliases=None, expected_companies=None, expected_schools=None,
    expected_roles=None, expected_relatives=None, expected_birth_date="", expected_age=None,
) -> tuple[dict | None, dict]:
    people = _people(data)
    ranked = []
    for person in people:
        evidence = _identity_evidence(
            person, expected_name, expected_location,
            expected_aliases=expected_aliases, expected_companies=expected_companies,
            expected_schools=expected_schools, expected_roles=expected_roles,
            expected_relatives=expected_relatives, expected_birth_date=expected_birth_date,
            expected_age=expected_age,
        )
        ranked.append((person, evidence))
    ranked.sort(key=lambda item: item[1]["score"], reverse=True)
    admissible = [item for item in ranked if item[1]["admissible"]]
    if not admissible:
        return None, {
            "candidates_reviewed": len(people), "selection": "no_exact_name_location",
            "top_score": ranked[0][1]["score"] if ranked else 0,
        }

    top_person, top = admissible[0]
    # A near-threshold identity is still a competing identity. Requiring the
    # margin against all same-name/same-state contenders avoids accepting a
    # winner merely because the runner missed the hard threshold by 0.001.
    contenders = [
        item for item in ranked
        if (item[1].get("name") or {}).get("matched")
        and (item[1].get("location") or {}).get("state_match")
    ]
    runner_score = max(
        (float(item[1]["score"]) for item in contenders if item[0] is not top_person),
        default=0.0,
    )
    margin = round(float(top["score"]) - float(runner_score), 3)
    if runner_score and margin < _IDENTITY_WINNER_MARGIN:
        return None, {
            "candidates_reviewed": len(people), "selection": "ambiguous_exact_matches",
            "exact_candidates": len(admissible), "top_score": top["score"],
            "runner_up_score": runner_score, "winner_margin": margin,
        }

    exact_location = bool((top.get("location") or {}).get("exact"))
    selection_name = (
        "unique_exact_name_location"
        if len(ranked) == 1 and len(admissible) == 1 and exact_location
        else "ranked_identity_match"
    )
    return top_person, {
        "candidates_reviewed": len(people), "selection": selection_name,
        "exact_candidates": sum(
            bool((item[1].get("location") or {}).get("exact")) for item in admissible
        ),
        "selection_score": top["score"], "runner_up_score": runner_score,
        "winner_margin": margin, "matched_name": (top.get("name") or {}).get("matched_name") or "",
        "matched_name_type": (top.get("name") or {}).get("matched_via") or "",
        "identity_evidence": top,
    }


def _address_line(value: dict) -> str:
    explicit = value.get("fullAddress") or value.get("FullAddress")
    line = explicit or ", ".join(filter(None, [
        " ".join(filter(None, [
            str(value.get("street") or value.get("addressLine1") or value.get("AddressLine1") or "").strip(),
            str(value.get("unit") or "").strip(),
        ])),
        str(value.get("city") or value.get("City") or "").strip(),
        " ".join(filter(None, [
            str(value.get("state") or value.get("State") or "").strip(),
            str(value.get("zip") or value.get("zipCode") or value.get("Zip") or "").strip(),
        ])),
        str(value.get("addressLine2") or value.get("AddressLine2") or "").strip(),
    ]))
    return re.sub(r"\s+", " ", str(line or "")).strip(" ,")


def _person_locations(person: dict) -> list[str]:
    return [record["value"] for record in _address_records(person)]


def _score(person: dict, data: dict, has_contacts: bool) -> float:
    candidates = [
        person.get("identityScore"), person.get("IdentityScore"),
        person.get("matchScore"), person.get("MatchScore"),
        data.get("identityScore"), data.get("matchScore"),
    ]
    for value in candidates:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if numeric > 1:
            numeric /= 100
        return max(0.0, min(1.0, numeric))
    # A returned contact is evidence, but it is not a perfect identity score.
    return 0.8 if has_contacts else 0.0


def _ordinal(value) -> int:
    try:
        return int(value or 999)
    except (TypeError, ValueError):
        return 999


def _ordered_phone_values(person: dict) -> list:
    values = _values(_get(person, "phones", "phoneNumbers"))
    return sorted(
        values,
        key=lambda item: (
            0 if isinstance(item, dict) and _provider_true(_get(item, "isConnected")) else 1,
            _ordinal(_get(item, "phoneOrder")) if isinstance(item, dict) else 999,
        ),
    )


def _provider_true(value) -> bool:
    return value is True or str(value or "").strip().casefold() in {"1", "true", "yes"}


def _ordered_email_values(person: dict) -> list:
    values = _values(_get(person, "emails", "emailAddresses"))
    return sorted(
        values,
        key=lambda item: _ordinal(_get(item, "emailOrdinal")) if isinstance(item, dict) else 999,
    )


def _person_ids(person: dict) -> list[str]:
    output = []
    for name in ("tahoeId", "tahoeIds", "personId", "personIds", "id"):
        for value in _values(_get(person, name)):
            if isinstance(value, dict):
                value = _get(value, "id", "value", "tahoeId", "personId")
            text = _opaque_id(value)
            if text:
                output.append(text[:160])
    return _dedupe_text(output)[:20]


def _merge_provider_value(left, right):
    """Merge fragments already proven to share one exact provider owner ID."""
    if left in (None, "", [], {}):
        return right
    if right in (None, "", [], {}):
        return left
    if isinstance(left, list) and isinstance(right, list):
        output = list(left)
        seen = {repr(value) for value in output}
        for value in right:
            marker = repr(value)
            if marker not in seen:
                seen.add(marker)
                output.append(value)
        return output
    if isinstance(left, dict) and isinstance(right, dict):
        output = dict(left)
        for key, value in right.items():
            output[key] = (
                _merge_provider_value(output[key], value)
                if key in output else value
            )
        return output
    return left


def _consolidate_people(people: list[dict]) -> list[dict]:
    """Union only duplicate response fragments with an exact shared owner ID.

    Two same-name people are never merged.  This prevents provider pagination
    or bundle fragments for one Tahoe/person ID from becoming a fake runner-up
    ambiguity, while preserving contacts and context spread across fragments.
    """
    groups: list[dict] = []
    for person in people or []:
        if not isinstance(person, dict):
            continue
        ids = set(_person_ids(person))
        matches = [
            index for index, group in enumerate(groups)
            if ids and ids & group["ids"]
        ]
        if not matches:
            groups.append({"ids": ids, "person": dict(person), "count": 1})
            continue
        first = matches[0]
        groups[first]["person"] = _merge_provider_value(
            groups[first]["person"], person,
        )
        groups[first]["ids"].update(ids)
        groups[first]["count"] += 1
        for index in reversed(matches[1:]):
            groups[first]["person"] = _merge_provider_value(
                groups[first]["person"], groups[index]["person"],
            )
            groups[first]["ids"].update(groups[index]["ids"])
            groups[first]["count"] += groups[index]["count"]
            groups.pop(index)
    output = []
    for group in groups:
        person = dict(group["person"])
        if group["count"] > 1:
            person["_merged_provider_fragments"] = group["count"]
        output.append(person)
    return output


def _tahoe_ids(person: dict) -> list[str]:
    output = []
    for name in ("tahoeId", "tahoeIds"):
        for value in _values(_get(person, name)):
            if isinstance(value, dict):
                value = _get(value, "id", "value", "tahoeId")
            text = _opaque_id(value)
            if text:
                output.append(text[:160])
    return _dedupe_text(output)[:20]


def _map(data: dict, name: str, *, request_fields=None) -> dict:
    person = _person(data)
    phones, emails, addrs = [], [], []
    phone_evidence, email_evidence, address_evidence = [], [], []
    for p in _ordered_phone_values(person):
        num = _get(p, "number", "phoneNumber") if isinstance(p, dict) else p
        if num:
            cleaned = _clean_phone(num)
            phone_type = str(
                _get(p, "type", "phoneType") or ""
            )[:80] if isinstance(p, dict) else ""
            phone_status = str(
                _get(p, "status", "lineStatus", "phoneStatus", "connectionStatus") or ""
            )[:80] if isinstance(p, dict) else ""
            connected = _provider_true(_get(p, "isConnected")) if isinstance(p, dict) else False
            raw_current = (
                _get(p, "isCurrent", "current") if isinstance(p, dict) else None
            )
            current = None if raw_current is None else _provider_true(raw_current)
            mobile_or_wireless = phone_policy.is_explicit_mobile_type(phone_type)
            accepted = bool(mobile_or_wireless and connected)
            rejection_reasons = []
            if not mobile_or_wireless:
                rejection_reasons.append("not_explicitly_mobile_or_wireless")
            if not connected:
                rejection_reasons.append("not_confirmed_connected")
            phone_evidence.append({
                "value": cleaned,
                "type": phone_type,
                "normalized_type": phone_policy.normalized_phone_type(phone_type),
                "status": phone_status,
                "is_current": current,
                "is_connected": connected,
                "mobile_or_wireless": mobile_or_wireless,
                "accepted": accepted,
                "rejection_reason": ",".join(rejection_reasons),
                "first_reported": str(_get(p, "firstReportedDate") or "")[:40]
                    if isinstance(p, dict) else "",
                "last_reported": str(_get(p, "lastReportedDate") or "")[:40]
                    if isinstance(p, dict) else "",
            })
            if accepted:
                phones.append(cleaned)
    for e in _ordered_email_values(person):
        v = _get(e, "email", "emailAddress") if isinstance(e, dict) else e
        if v:
            cleaned = str(v).strip()
            if isinstance(e, dict):
                raw_current = _get(e, "isCurrent", "current")
                current = None if raw_current is None else _provider_true(raw_current)
                email_evidence.append({
                    "value": cleaned,
                    "type": str(_get(e, "type", "emailType") or "")[:80],
                    "is_current": current,
                    "first_reported": str(_get(e, "firstReportedDate") or "")[:40],
                    "last_reported": str(_get(e, "lastReportedDate") or "")[:40],
                })
                if current is False:
                    continue
            emails.append(cleaned)
    for record in _address_records(person):
        addrs.append(record["value"])
        address_evidence.append({
            "value": record["value"], "is_current": record.get("is_current"),
            "first_reported": record.get("first_reported") or "",
            "last_reported": record.get("last_reported") or "",
            "recency": _address_recency(record)[1],
        })
    phone_candidates = _dedupe(
        item.get("value") for item in phone_evidence if item.get("value")
    )
    phones = phone_policy.preferred_phone_values({
        "phones": phone_candidates,
        "phone_evidence": phone_evidence,
    })
    phones, emails, addrs = _dedupe(phones), _dedupe(emails), _dedupe(addrs)
    has_contacts = bool(phones or emails)
    workplaces = _person_workplaces(person)
    roles = _person_roles(person)
    schools = _person_schools(person)
    aliases = _person_aliases(person)
    relative_records = _person_relative_records(person)
    relatives = _dedupe([record.get("name") or "" for record in relative_records])
    person_ids = _person_ids(person)
    return {
        "status": "success" if has_contacts else "no_match",
        "matched_name": _matched_name(person),
        "provider_primary_name": _matched_name(person),
        "provider_aliases": aliases,
        "provider_birth_date": _person_birth_date(person),
        "provider_age": _person_age(person),
        "provider_person_ids": person_ids,
        "provider_tahoe_ids": _tahoe_ids(person),
        "provider_company": workplaces[0] if workplaces else "",
        "provider_organizations": _dedupe([*workplaces, *schools]),
        "provider_roles": roles,
        "provider_job_title": roles[0] if roles else "",
        "provider_workplaces": workplaces,
        "provider_schools": schools,
        "provider_relatives": relatives,
        "provider_relative_records": relative_records,
        "phones": phones, "emails": emails, "addresses": addrs,
        "provider_location": addrs[0] if addrs else "",
        "confidence": _score(person, data, has_contacts),
        "request_fields": list(request_fields or []),
        "phone_evidence": phone_evidence[:20],
        "email_evidence": email_evidence[:20],
        "address_evidence": address_evidence[:20],
        "source": "enformion",
        "phone_policy": phone_policy.selected_phone_policy({
            "phones": phones, "phone_evidence": phone_evidence,
        }),
    }


def _map_tahoe_search(
    data: dict, tahoe_id: str, name: str, location: str, *, request_fields=None,
    expected_aliases=None, expected_companies=None, expected_schools=None,
    expected_roles=None, expected_relatives=None, expected_birth_date="", expected_age=None,
) -> dict:
    """Map one Tahoe-ID response after validating the target's own identity."""
    if data.get("isError") or data.get("IsError"):
        provider_error = data.get("error") or data.get("Error") or {}
        message = _text(_get(provider_error, "message", "description", "details")) \
            if isinstance(provider_error, dict) else _text(provider_error)
        return {
            "status": "error", "source": "enformion",
            "error": f"Enformion Person Search failed{': ' + message if message else ''}",
            "phones": [], "emails": [], "addresses": [],
            "request_fields": list(request_fields or ["tahoe_id"]),
            "bridge_target_tahoe_id": _opaque_id(tahoe_id),
            "relative_bridge_used": False, "household_contact_used": False,
        }
    target_key = _opaque_id(tahoe_id)
    target_people = [
        person for person in _people(data)
        if target_key and any(_opaque_id(value) == target_key for value in _tahoe_ids(person))
    ]
    base = {
        "source": "enformion", "phones": [], "emails": [], "addresses": [],
        "request_fields": list(request_fields or ["tahoe_id"]),
        "bridge_target_tahoe_id": _opaque_id(tahoe_id),
        "relative_bridge_used": False, "household_contact_used": False,
    }
    if not target_people:
        return {
            **base, "status": "no_match", "confidence": 0,
            "selection": {"selection": "tahoe_id_not_returned", "candidates_reviewed": 0},
            "message": "The requested relative ID was not returned by the provider.",
        }
    person, selection = _select_person(
        {"persons": target_people}, name, location,
        expected_aliases=expected_aliases, expected_companies=expected_companies,
        expected_schools=expected_schools, expected_roles=expected_roles,
        expected_relatives=expected_relatives, expected_birth_date=expected_birth_date,
        expected_age=expected_age,
    )
    if person is None:
        return {
            **base, "status": "no_match", "confidence": 0, "selection": selection,
            "message": "The resolved relative record did not pass its own name/location validation.",
        }
    normalized = _map({"person": person}, name, request_fields=request_fields or ["tahoe_id"])
    normalized.update({
        "selection": selection,
        "source": "enformion",
        "bridge_target_tahoe_id": _opaque_id(tahoe_id),
        "relative_bridge_used": True,
        "household_contact_used": False,
    })
    if selection.get("matched_name"):
        normalized["matched_name"] = selection["matched_name"]
        normalized["matched_name_type"] = selection.get("matched_name_type") or ""
    return normalized


def _relative_pivot(data: dict, name: str, location: str, *, expected_aliases=None) -> dict | None:
    """Find one resolvable candidate relative without borrowing contacts.

    Person Search can return a household owner whose relative list points to
    the requested candidate.  That list is discovery evidence only.  The
    caller must resolve the returned Tahoe ID in a second request before any
    contact may be used.
    """
    expected_names = [(name, "source_name")]
    expected_names.extend(
        (alias, "source_alias") for alias in (expected_aliases or [])
        if len(_name_tokens(alias)) >= 2
    )
    matches: dict[str, list[dict]] = {}
    for household in _people(data):
        household_location = _location_evidence(household, location)
        if not household_location.get("exact"):
            continue
        for relative in _person_relative_records(household):
            returned_name = relative.get("name") or ""
            if not returned_name:
                continue
            matched_expected = None
            for expected, expected_type in expected_names:
                evidence = _one_name_evidence(expected, returned_name)
                if evidence.get("matched"):
                    matched_expected = (expected, expected_type, evidence)
                    break
            if matched_expected is None:
                continue
            # TahoeIds is the licensed endpoint's supported stable pivot.  A
            # generic PersonId is retained for audit but cannot be queried as
            # TahoeIds, and multiple Tahoe IDs are deliberately ambiguous.
            tahoe_ids = _dedupe_text(relative.get("tahoe_ids") or [])
            if len(tahoe_ids) != 1:
                continue
            expected, expected_type, evidence = matched_expected
            matches.setdefault(tahoe_ids[0], []).append({
                "relative": relative,
                "expected_name": expected,
                "expected_type": expected_type,
                "name_evidence": evidence,
                "household_primary_name": _matched_name(household),
                "household_primary_ids": _person_ids(household),
                "household_location_kind": household_location.get("address_kind") or "",
            })
    if len(matches) != 1:
        return None
    tahoe_id, evidence_rows = next(iter(matches.items()))
    selected = evidence_rows[0]
    relative = selected["relative"]
    return {
        "status": "relative_pivot",
        "source": "enformion",
        "matched_name": relative.get("name") or "",
        "matched_name_type": "provider_relative",
        "phones": [], "emails": [], "addresses": [],
        "phone_evidence": [], "email_evidence": [], "address_evidence": [],
        "confidence": 0,
        "relative_tahoe_id": tahoe_id,
        "relative_person_ids": list(relative.get("person_ids") or []),
        "relative_record": relative,
        "relative_bridge_used": False,
        "household_contact_used": False,
        "selection": {
            "selection": "unique_relative_pivot",
            "relative_tahoe_id": tahoe_id,
            "matched_name": relative.get("name") or "",
            "matched_name_type": "provider_relative",
            "expected_name": selected["expected_name"],
            "expected_name_type": selected["expected_type"],
            "candidate_occurrences": len(evidence_rows),
            "household_primary_name": selected["household_primary_name"],
            "household_primary_ids": selected["household_primary_ids"],
            "household_location_exact": True,
            "household_location_kind": selected["household_location_kind"],
        },
    }


def _map_search(
    data: dict, name: str, location: str, *, request_fields=None,
    expected_aliases=None, expected_companies=None, expected_schools=None,
    expected_roles=None, expected_relatives=None, expected_birth_date="", expected_age=None,
) -> dict:
    if data.get("isError") or data.get("IsError"):
        provider_error = data.get("error") or data.get("Error") or {}
        if not isinstance(provider_error, dict):
            provider_error = {"message": str(provider_error)}
        code = str(provider_error.get("code") or provider_error.get("Code") or "").strip()
        message = str(provider_error.get("message") or provider_error.get("Message") or "").strip()
        detail = ": ".join(value for value in (code, message) if value)
        return {
            "status": "error", "source": "enformion",
            "error": f"Enformion Person Search failed{': ' + detail if detail else ''}",
            "provider_error_code": code, "phones": [], "emails": [], "addresses": [],
            "request_fields": list(request_fields or []),
        }
    person, selection = _select_person(
        data, name, location,
        expected_aliases=expected_aliases, expected_companies=expected_companies,
        expected_schools=expected_schools, expected_roles=expected_roles,
        expected_relatives=expected_relatives, expected_birth_date=expected_birth_date,
        expected_age=expected_age,
    )
    if person is None:
        # An ambiguous set of otherwise admissible direct owners must remain
        # ambiguous.  Relative discovery is allowed only when no direct owner
        # met the identity policy at all.
        pivot = None
        if selection.get("selection") == "no_exact_name_location":
            pivot = _relative_pivot(
                data, name, location, expected_aliases=expected_aliases,
            )
        if pivot is not None:
            pivot["request_fields"] = list(request_fields or [])
            return pivot
        return {
            "status": "no_match", "source": "enformion",
            "matched_name": "", "phones": [], "emails": [], "addresses": [],
            "confidence": 0, "selection": selection,
            "request_fields": list(request_fields or []),
            "message": (
                "Enformion returned multiple exact name/location people."
                if selection.get("selection") == "ambiguous_exact_matches"
                else "Enformion returned no unique exact name/location person."
            ),
        }
    normalized = _map({"person": person}, name, request_fields=request_fields)
    normalized["selection"] = selection
    # When the independently supplied source name matched a documented AKA,
    # verification must compare against that provider-returned alias rather
    # than the different primary name. The primary remains available for audit.
    if selection.get("matched_name"):
        normalized["matched_name"] = selection["matched_name"]
        normalized["matched_name_type"] = selection.get("matched_name_type") or ""
    normalized["source"] = "enformion"
    normalized["provider_location"] = (
        normalized.get("provider_location")
        or (_person_locations(person) or [""])[0]
    )
    return normalized
