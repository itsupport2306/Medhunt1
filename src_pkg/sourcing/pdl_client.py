"""Server-side People Data Labs enrichment with provider evidence and credit guards.

The browser never sees the API key. Results are cached by normalized identity,
and a lookup run observes ``PDL_RUN_CREDIT_LIMIT`` when it is greater than zero.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, quote, urlparse

import httpx

from . import (
    config, enrich, store, identity_resolution, person_name, phone_policy, trust_policy,
    source_context,
)

_PROVIDER = "people_data_labs"
_REQUEST_POLICY_VERSION = "pdl-person-enrich-v22-combined-inputs"
# PDL's ``required`` parameter accepts Boolean field expressions.  Require at
# least one contact that this application is prepared to use, while no longer
# hiding an otherwise useful email-bearing identity merely because PDL lacks
# its dedicated mobile field.  The broad historical ``emails`` and
# ``phone_numbers`` fields are intentionally absent from this expression.
_REQUIRED_PROFILE_FIELDS = (
    "(mobile_phone OR phone_numbers OR recommended_personal_email OR personal_emails OR work_email)"
)
_LOOKUP_LOCK = threading.Lock()
_HTTP_CLIENT_LOCK = threading.Lock()
_HTTP_CLIENT: httpx.Client | None = None
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
# Omitting ``data_include`` asks PDL for the full profile available to this API
# key. That mirrors PDL's reviewed sample and retains Person Base corroboration
# fields such as current job title/company, while our normalized result stores
# only the recruitment-relevant subset below.
_RESPONSE_FIELD_POLICY = "full-profile-including-person-base"

_STATE_NAMES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
    "ca": "california", "co": "colorado", "ct": "connecticut", "de": "delaware",
    "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
    "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas",
    "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota",
    "ms": "mississippi", "mo": "missouri", "mt": "montana", "ne": "nebraska",
    "nv": "nevada", "nh": "new hampshire", "nj": "new jersey",
    "nm": "new mexico", "ny": "new york", "nc": "north carolina",
    "nd": "north dakota", "oh": "ohio", "ok": "oklahoma", "or": "oregon",
    "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
    "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah",
    "vt": "vermont", "va": "virginia", "wa": "washington",
    "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
}
_STATE_ALIASES = {**_STATE_NAMES, **{name: name for name in _STATE_NAMES.values()}}
_FACEBOOK_RESERVED_ROUTES = {
    "watch", "marketplace", "gaming", "groups", "events", "pages", "ads",
    "business", "help", "settings", "notifications", "messages", "friends",
    "bookmarks", "memories", "search", "stories", "reels", "reel", "live",
    "fundraisers", "offers", "jobs", "login", "recover", "policies", "privacy",
    "terms", "cookies", "photo.php", "video.php", "share.php", "sharer.php",
    "dialog", "home.php", "places", "photo",
}


def configured() -> bool:
    return bool(config.PDL_ENABLED and config.PDL_API_KEY)


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(
        timeout=min(config.PDL_TIMEOUT, 15.0),
        connect=min(config.PDL_TIMEOUT, 5.0),
        read=min(config.PDL_TIMEOUT, 15.0),
        write=min(config.PDL_TIMEOUT, 5.0),
        pool=min(config.PDL_TIMEOUT, 5.0),
    )


def _bulk_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        timeout=config.PDL_BULK_TIMEOUT,
        connect=min(config.PDL_TIMEOUT, 5.0),
        read=config.PDL_BULK_TIMEOUT,
        write=min(config.PDL_TIMEOUT, 10.0),
        pool=min(config.PDL_TIMEOUT, 5.0),
    )


def _client() -> httpx.Client:
    global _HTTP_CLIENT
    with _HTTP_CLIENT_LOCK:
        if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
            _HTTP_CLIENT = httpx.Client(
                timeout=_timeout(),
                limits=httpx.Limits(
                    max_connections=5,
                    max_keepalive_connections=2,
                    keepalive_expiry=300.0,
                ),
            )
        return _HTTP_CLIENT


def warm() -> bool:
    """Open a reusable PDL connection with a zero-credit invalid HEAD call."""
    if not configured():
        return False
    try:
        response = _client().head(
            config.PDL_BASE_URL,
            headers={"X-Api-Key": config.PDL_API_KEY, "Accept": "application/json"},
        )
        return int(response.headers.get("X-Call-Credits-Spent", "0")) == 0
    except (TypeError, ValueError, httpx.HTTPError):
        return False


def close() -> None:
    global _HTTP_CLIENT
    with _HTTP_CLIENT_LOCK:
        client, _HTTP_CLIENT = _HTTP_CLIENT, None
        if client is not None:
            client.close()


@contextmanager
def _lookup_slot():
    acquired = _LOOKUP_LOCK.acquire(timeout=1.0)
    try:
        yield acquired
    finally:
        if acquired:
            _LOOKUP_LOCK.release()


def _normal(value: str) -> str:
    return " ".join(person_name.identity_tokens(value))


def _unique(values, limit=20):
    output, seen = [], set()
    for value in values or []:
        # Free/basic PDL field bundles may return boolean availability flags
        # instead of contact values. Never turn True into a fake email/phone.
        if isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
            continue
        cleaned = str(value or "").strip()
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        output.append(cleaned)
        if len(output) >= limit:
            break
    return output


def _items(value):
    if value is None:
        return []
    if isinstance(value, bool):
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _person_name_matches(expected: str, returned: str) -> bool:
    expected_parts = _normal(person_name.normalize_person_name(expected)).split()
    returned_parts = _normal(person_name.normalize_person_name(returned)).split()
    if len(expected_parts) < 2 or len(returned_parts) < 2:
        return False
    # Middle names/initials and suffixes may differ; first and last must agree.
    suffixes = {"jr", "sr", "ii", "iii", "iv"}
    while expected_parts and expected_parts[-1] in suffixes:
        expected_parts.pop()
    while returned_parts and returned_parts[-1] in suffixes:
        returned_parts.pop()
    return bool(expected_parts and returned_parts) and (
        expected_parts[0] == returned_parts[0]
        and expected_parts[-1] == returned_parts[-1]
    )


def _location_parts(value: str) -> tuple[str, str]:
    parts = [_normal(part) for part in str(value or "").split(",") if _normal(part)]
    if not parts:
        return "", ""
    city = parts[0]
    state = parts[1] if len(parts) > 1 else ""
    state = re.sub(r"\b\d{5}(?: \d{4})?\b", "", state).strip()
    return city, _STATE_ALIASES.get(state, state)


def _location_matches(expected: str, returned: str) -> bool:
    expected_city, expected_state = _location_parts(expected)
    returned_city, returned_state = _location_parts(returned)
    if not expected_city or not expected_state:
        return False
    return expected_city == returned_city and expected_state == returned_state


def _profile_match_inputs(candidate: dict) -> tuple[list[str], list[str]]:
    """Return shared, bounded employer/school alternatives for PDL."""
    context = source_context.extract(candidate)
    return context["companies"], context["schools"]


def _social_profile(candidate: dict) -> str:
    """Return a normalized, supported person-profile URL from a captured card.

    PDL accepts a social profile as a minimum identity input. Only the two
    explicit single-profile adapters are allowed here; arbitrary source URLs,
    Facebook groups/search routes, and LinkedIn search pages are ignored.
    """
    source = str(candidate.get("source") or "").strip().casefold()
    raw = str(candidate.get("source_url") or "").strip()
    if source not in {"facebook", "linkedin"} or not raw:
        return ""
    try:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold()
    path = parsed.path.rstrip("/") or "/"
    if source == "linkedin":
        if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
            return ""
        match = re.fullmatch(r"/in/([^/?#]+)", path, re.IGNORECASE)
        return f"https://www.linkedin.com/in/{quote(match.group(1))}" if match else ""
    if not (host == "facebook.com" or host.endswith(".facebook.com")):
        return ""
    if path.casefold() == "/profile.php":
        identity = (parse_qs(parsed.query).get("id") or [""])[0].strip()
        if re.fullmatch(r"[A-Za-z0-9._-]+", identity):
            return f"https://www.facebook.com/profile.php?id={quote(identity)}"
        return ""
    match = re.fullmatch(r"/([A-Za-z0-9._-]+)", path)
    if not match:
        return ""
    username = match.group(1)
    if username.casefold() in _FACEBOOK_RESERVED_ROUTES:
        return ""
    return f"https://www.facebook.com/{quote(username)}"


def _request_key(candidate: dict) -> str:
    companies, schools = _profile_match_inputs(candidate)
    source_aliases = [
        item.get("value", "") for item in _source_name_alias_evidence(candidate)
    ]
    material = "|".join((
        _REQUEST_POLICY_VERSION,
        str(config.PDL_MIN_LIKELIHOOD),
        _REQUIRED_PROFILE_FIELDS,
        _RESPONSE_FIELD_POLICY,
        "deterministic-identity-validation-required",
        _normal(_lookup_name(candidate)),
        _normal(candidate.get("location", "")),
        _normal(_social_profile(candidate)),
        ",".join(_normal(value) for value in companies),
        ",".join(_normal(value) for value in schools),
        ",".join(_normal(value) for value in source_aliases),
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _lookup_name(candidate: dict) -> str:
    if candidate.get("identity_status") in ("verified", "recruiter_confirmed"):
        value = candidate.get("canonical_name") or candidate.get("name") or ""
    else:
        value = candidate.get("name") or ""
    return person_name.normalize_person_name(value)


def _lookup_names(candidate: dict) -> list[str]:
    """Return the primary name plus explicit full source aliases.

    PDL supports alternative values for a person input. Sending the captured
    Facebook/LinkedIn alternate name in the same request improves married-name
    recall without multiplying credits. One-word nicknames and inferred name
    variants remain excluded because they are too weak to be query anchors.
    """
    primary = _lookup_name(candidate)
    signature = person_name.identity_signature(primary)
    # PDL accepts several values for one input in a single request.  Keep the
    # full captured name and, when it contains ordinary middle names, also send
    # a first+terminal-surname discovery value.  Final identity verification
    # still compares the returned owner and does not trust query broadening.
    middle_flexible = (
        " ".join((signature["first"], *signature["surname"]))
        if signature.get("middle")
        else ""
    )
    return _unique([
        primary,
        middle_flexible,
        *(item.get("value") for item in _source_name_alias_evidence(candidate)),
    ], limit=5)


def _lookup_name_parameter(candidate: dict) -> str | list[str]:
    names = _lookup_names(candidate)
    return names if len(names) > 1 else (names[0] if names else "")


def _query_attempts(candidate: dict) -> list[dict]:
    """Build one complete PDL enrichment entry for one source identity.

    PDL's matcher is designed to evaluate all available identity evidence in a
    single request.  Splitting location, employers, schools, or a social URL
    into follow-up enrichment calls both removes useful context and can buy
    several responses for the same candidate.  Alternate full names remain
    values of the same ``name`` input; they are not separate requests.

    Local identity, likelihood, ambiguity, DNC, and contact-quality gates still
    decide whether the one returned person can be exposed.
    """
    name = _lookup_name_parameter(candidate)
    location = str(candidate.get("location") or "").strip()
    companies, schools = _profile_match_inputs(candidate)
    profile = _social_profile(candidate)
    if not name or not any((location, companies, schools, profile)):
        return []
    identity = (name, location, companies, schools, profile)
    call_identity = identity if profile else identity[:4]
    return [{"strategy": "combined_context", "identity": call_identity}]


def _lookup_candidate(candidate: dict) -> dict:
    name = _lookup_name(candidate)
    return {**candidate, "name": name} if name else dict(candidate)


def _provider_value(item, *keys) -> str:
    """Return one provider scalar without stringifying flags/containers."""
    if isinstance(item, bool) or item is None:
        return ""
    if isinstance(item, dict):
        for key in keys:
            value = item.get(key)
            if value is not None and not isinstance(value, (bool, dict, list, tuple, set)):
                cleaned = str(value).strip()
                if cleaned:
                    return cleaned
        return ""
    if isinstance(item, (list, tuple, set)):
        return ""
    return str(item).strip()


def _provider_metadata(item: dict) -> dict:
    """Keep only bounded, non-secret provenance fields from a PDL item."""
    if not isinstance(item, dict):
        return {}
    evidence = {}
    for key in ("first_seen", "last_seen"):
        value = _provider_value(item, key)
        if value:
            evidence[key] = value[:50]
    for output_key, provider_keys in {
        "is_connected": ("is_connected", "connected"),
        "is_current": ("is_current", "current"),
    }.items():
        for provider_key in provider_keys:
            if provider_key in item and isinstance(item.get(provider_key), bool):
                evidence[output_key] = item[provider_key]
                break
    status = _provider_value(
        item, "status", "line_status", "phone_status", "connection_status",
    )
    if status:
        evidence["status"] = status[:80]
    try:
        if item.get("num_sources") is not None and not isinstance(item.get("num_sources"), bool):
            evidence["num_sources"] = max(0, int(item.get("num_sources")))
    except (TypeError, ValueError):
        pass
    return evidence


def _email_evidence(person: dict) -> list[dict]:
    """Separate outreach-safe PDL email fields from broad historical evidence.

    PDL documents ``emails`` as an associated/historical collection.  Those
    values are useful for identity corroboration but are not automatically
    saved for outreach.  Only the dedicated current/recommended fields satisfy
    the application's usable-email policy.
    """
    evidence_by_value = {}

    def add(item, source_field: str, accepted: bool):
        value = _provider_value(item, "address", "email", "value")
        if not value:
            return
        key = value.casefold()
        existing = evidence_by_value.get(key)
        metadata = _provider_metadata(item) if isinstance(item, dict) else {}
        if existing is None:
            existing = {
                "value": value,
                "source_field": source_field,
                "source_fields": [source_field],
                "accepted_for_outreach": bool(accepted),
                **metadata,
            }
            evidence_by_value[key] = existing
        else:
            if source_field not in existing["source_fields"]:
                existing["source_fields"].append(source_field)
            existing["accepted_for_outreach"] = bool(
                existing["accepted_for_outreach"] or accepted
            )
            # Dedicated fields remain the primary provenance when a historical
            # detail record contains metadata for the same address.
            if accepted and existing.get("source_field") == "emails":
                existing["source_field"] = source_field
            existing.update({key: value for key, value in metadata.items() if value not in (None, "")})

    add(person.get("recommended_personal_email"), "recommended_personal_email", True)
    for item in _items(person.get("personal_emails")):
        add(item, "personal_emails", True)
    add(person.get("work_email"), "work_email", True)
    for item in _items(person.get("emails")):
        add(item, "emails", False)
    return list(evidence_by_value.values())[:50]


def _email_values(person: dict) -> list[str]:
    return _unique(
        item.get("value") for item in _email_evidence(person)
        if item.get("accepted_for_outreach") is True
    )


def _phone_evidence(person: dict) -> list[dict]:
    """Retain dedicated mobile and separately labeled associated phones."""
    evidence_by_value = {}

    def add(item, source_field: str, accepted: bool):
        value = _provider_value(item, "number", "phone_number", "phone", "value")
        if not value:
            return
        key = _normal(value)
        if not key:
            return
        metadata = _provider_metadata(item) if isinstance(item, dict) else {}
        provider_type = (
            _provider_value(item, "type", "phone_type", "line_type").casefold()
            if isinstance(item, dict) else ""
        )
        existing = evidence_by_value.get(key)
        if existing is None:
            existing = {
                "value": value,
                "type": "mobile" if accepted else provider_type,
                "source_field": source_field,
                "source_fields": [source_field],
                "mobile_or_wireless": bool(
                    accepted or provider_type in {"mobile", "wireless", "cell", "cellular"}
                ),
                "is_connected": None,
                "accepted": bool(accepted),
                **metadata,
            }
            evidence_by_value[key] = existing
        else:
            if source_field not in existing["source_fields"]:
                existing["source_fields"].append(source_field)
            existing["accepted"] = bool(existing["accepted"] or accepted)
            existing["mobile_or_wireless"] = bool(
                existing["mobile_or_wireless"] or accepted
                or provider_type in {"mobile", "wireless", "cell", "cellular"}
            )
            if accepted:
                existing["source_field"] = "mobile_phone"
                existing["type"] = "mobile"
            elif provider_type and not existing.get("type"):
                existing["type"] = provider_type
            existing.update({key: value for key, value in metadata.items() if value not in (None, "")})

    add(person.get("mobile_phone"), "mobile_phone", True)
    for item in _items(person.get("phone_numbers")):
        add(item, "phone_numbers", False)
    for item in _items(person.get("phones")):
        add(item, "phones", False)
    return list(evidence_by_value.values())[:50]


def _phone_values(person: dict) -> list[str]:
    # Even if a broad phone record says "mobile", only ``mobile_phone`` is an
    # automatically usable phone.  The rest remains backend match evidence.
    return _unique(
        item.get("value") for item in _phone_evidence(person)
        if item.get("accepted") is True and item.get("source_field") == "mobile_phone"
    )


def _address_text(item) -> str:
    if not isinstance(item, dict):
        return _provider_value(item)
    street = _provider_value(item, "street_address", "address_line_1", "address")
    # ``street_addresses[].name`` is only its city/region label. Prefer the
    # actual address components when present so historical street evidence is
    # not collapsed into the corresponding location-name row.
    if not street:
        explicit = _provider_value(
            item, "name", "location_name", "formatted_address", "display"
        )
        if explicit:
            return explicit
    parts = [
        street,
        _provider_value(item, "address_line_2"),
        _provider_value(item, "locality", "city"),
        _provider_value(item, "region", "state"),
        _provider_value(item, "postal_code", "zip"),
        _provider_value(item, "country"),
    ]
    return ", ".join(value for value in parts if value)


def _address_evidence(person: dict) -> list[dict]:
    """Normalize current and associated PDL locations with recency metadata."""
    evidence = []
    seen = set()

    def add(item, source_field: str, is_current=False):
        value = _address_text(item)
        key = _normal(value)
        if not key:
            return
        metadata = _provider_metadata(item) if isinstance(item, dict) else {}
        explicit_current = item.get("is_current") if isinstance(item, dict) else None
        current = bool(explicit_current) if isinstance(explicit_current, bool) else bool(is_current)
        if key in seen:
            for row in evidence:
                if _normal(row.get("value", "")) == key:
                    row["is_current"] = bool(row.get("is_current") or current)
                    if source_field not in row["source_fields"]:
                        row["source_fields"].append(source_field)
                    row.update({key: value for key, value in metadata.items() if value not in (None, "")})
                    break
            return
        seen.add(key)
        evidence.append({
            "value": value,
            "source_field": source_field,
            "source_fields": [source_field],
            "is_current": current,
            **metadata,
        })

    add(person.get("location_name"), "location_name", True)
    current_parts = {
        "street_address": person.get("location_street_address") or person.get("street_address"),
        "locality": person.get("location_locality") or person.get("locality"),
        "region": person.get("location_region") or person.get("region"),
        "postal_code": person.get("location_postal_code") or person.get("postal_code"),
        "country": person.get("location_country") or person.get("country"),
    }
    if any(value for value in current_parts.values() if not isinstance(value, bool)):
        add(current_parts, "current_location_fields", True)
    for index, item in enumerate(_items(person.get("location_names"))):
        # PDL documents the first associated location as current.
        add(item, "location_names", index == 0)
    for index, item in enumerate(_items(person.get("street_addresses"))):
        # PDL documents current first, followed by most-recently-seen history.
        add(item, "street_addresses", index == 0)
    # Compatibility with older cached/provider variants; this field is not
    # relied upon as the documented PDL source of address history.
    for item in _items(person.get("locations")):
        add(item, "locations", False)
    return evidence[:50]


def _address_values(person: dict) -> list[str]:
    return _unique(
        (item.get("value") for item in _address_evidence(person)), limit=50,
    )


def _name_alias_evidence(person: dict) -> list[dict]:
    aliases = []
    seen = set()
    primary = _normal(person.get("full_name") or "")
    for item in _items(person.get("name_aliases")):
        value = _provider_value(item, "full_name", "name", "value", "display")
        key = _normal(value)
        if not key or key == primary or key in seen:
            continue
        seen.add(key)
        aliases.append({
            "value": value,
            "source_field": "name_aliases",
            **(_provider_metadata(item) if isinstance(item, dict) else {}),
        })
    return aliases[:30]


def _source_name_alias_evidence(candidate: dict) -> list[dict]:
    """Retain only explicit, plausible full alternate names from source text."""
    primary = _normal(_lookup_name(candidate))
    aliases = []
    seen = set()
    for normalized in person_name.source_alternate_names(candidate.get("notes") or ""):
        key = _normal(normalized)
        parts = key.split()
        # A one-word nickname is not an independent full identity. It can be a
        # display hint, but must not become a provider match/query anchor.
        if len(parts) < 2 or len(parts[0]) < 2 or len(parts[-1]) < 2:
            continue
        if key == primary or key in seen or not _person_name_matches(normalized, normalized):
            continue
        seen.add(key)
        aliases.append({
            "value": normalized,
            "source_field": "captured_alternate_name",
            "independent_source": True,
        })
    return aliases[:10]


def _canonical_social_key(raw: str) -> str:
    try:
        parsed = urlparse(raw if "://" in str(raw or "") else f"https://{raw}")
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold()
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/") or "/"
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        match = re.fullmatch(r"/in/([^/?#]+)", path, re.IGNORECASE)
        return f"linkedin:{match.group(1).casefold()}" if match else ""
    if host == "facebook.com" or host.endswith(".facebook.com"):
        if path.casefold() == "/profile.php":
            identity = (parse_qs(parsed.query).get("id") or [""])[0].strip()
            return f"facebook:id:{identity.casefold()}" if identity else ""
        match = re.fullmatch(r"/([A-Za-z0-9._-]+)", path)
        if match and match.group(1).casefold() not in _FACEBOOK_RESERVED_ROUTES:
            return f"facebook:user:{match.group(1).casefold()}"
    return ""


def _profile_evidence(person: dict) -> list[dict]:
    evidence = []
    seen = set()

    def add(item, source_field: str, default_network=""):
        if isinstance(item, dict):
            url = _provider_value(item, "url", "profile_url", "link")
            network = _provider_value(item, "network", "platform", "service").casefold()
            username = _provider_value(item, "username", "handle")
            profile_id = _provider_value(item, "id", "profile_id")
            if not url and username and (network or default_network) in {"linkedin", "facebook"}:
                selected = network or default_network
                url = (
                    f"https://www.linkedin.com/in/{quote(username)}"
                    if selected == "linkedin" else f"https://www.facebook.com/{quote(username)}"
                )
        else:
            url = _provider_value(item)
            network, username, profile_id = default_network, "", ""
        if not url:
            return
        key = _canonical_social_key(url) or url.casefold().rstrip("/")
        if key in seen:
            return
        seen.add(key)
        evidence.append({
            "url": url,
            "network": network or (key.split(":", 1)[0] if ":" in key else ""),
            "username": username,
            "profile_id": profile_id,
            "source_field": source_field,
            **(_provider_metadata(item) if isinstance(item, dict) else {}),
        })

    add(person.get("linkedin_url"), "linkedin_url", "linkedin")
    add(person.get("facebook_url"), "facebook_url", "facebook")
    for item in _items(person.get("profiles")):
        add(item, "profiles")
    return evidence[:50]


def _matching_profile_url(candidate: dict, person: dict) -> str:
    profiles = _profile_evidence(person)
    expected = _social_profile(candidate)
    expected_key = _canonical_social_key(expected)
    if expected_key:
        for item in profiles:
            if _canonical_social_key(item.get("url", "")) == expected_key:
                # Returning the captured canonical URL makes exact confirmation
                # insensitive to harmless provider URL formatting differences.
                return expected
    source = str(candidate.get("source") or "").strip().casefold()
    for item in profiles:
        if item.get("network") == source:
            return item.get("url", "")
    return profiles[0].get("url", "") if profiles else ""


def _credits(response: httpx.Response, default: int) -> int:
    try:
        return max(0, int(response.headers.get("X-Call-Credits-Spent", default)))
    except (TypeError, ValueError):
        return default


def _call(
    name: str | list[str], location: str, companies=None, schools=None, profile: str = "",
) -> tuple[httpx.Response, dict]:
    headers = {"X-Api-Key": config.PDL_API_KEY, "Accept": "application/json"}
    params = {
        "name": name,
        "min_likelihood": config.PDL_MIN_LIKELIHOOD,
        "include_if_matched": "true",
        "required": _REQUIRED_PROFILE_FIELDS,
        "pretty": "false",
    }
    if location:
        params["location"] = location
    if profile:
        params["profile"] = profile
    if companies:
        params["company"] = list(companies)
    if schools:
        params["school"] = list(schools)
    response = _client().get(config.PDL_BASE_URL, params=params, headers=headers)
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    return response, payload if isinstance(payload, dict) else {}


def _bulk_url() -> str:
    """The bulk sibling of the configured person-enrich endpoint."""
    base = config.PDL_BASE_URL.rstrip("/")
    if base.endswith("/enrich"):
        return f"{base[: -len('/enrich')]}/bulk"
    return f"{base}/bulk"


def _bulk_call(
    identities: list[tuple],
) -> tuple[httpx.Response, list]:
    """Enrich up to ``PDL_BULK_MAX`` identities in one request.

    PDL documents response filtering and formatting parameters as valid global
    bulk fields, so one compact field selection applies to the whole request.
    """
    headers = {
        "X-Api-Key": config.PDL_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    requests = []
    for identity in identities:
        name, location, companies, schools = identity[:4]
        profile = identity[4] if len(identity) > 4 else ""
        params = {
            # PDL accepts multiple values for a person input as alternatives.
            # Keep captured full alternate names in the same billable request;
            # do not buy one speculative request per nickname/alias.
            "name": list(name) if isinstance(name, (list, tuple)) else [name],
            "min_likelihood": config.PDL_MIN_LIKELIHOOD,
            "include_if_matched": True,
            "required": _REQUIRED_PROFILE_FIELDS,
        }
        if location:
            params["location"] = [location]
        if profile:
            params["profile"] = [profile]
        if companies:
            params["company"] = list(companies)
        if schools:
            params["school"] = list(schools)
        requests.append({"params": params})
    # PDL currently returns per-item ``matched`` evidence only when this flag
    # is set globally on a bulk request. Keep the billable response filters in
    # each item, where PDL enforces them per person.
    body = {"requests": requests, "include_if_matched": True}
    response = _client().post(
        _bulk_url(), json=body, headers=headers, timeout=_bulk_timeout(),
    )
    try:
        payload = response.json()
    except ValueError:
        payload = []
    return response, payload if isinstance(payload, list) else []


def _discovery_person(person: dict, provider: str, score=0, matched=None) -> dict:
    """Normalize PDL Identify/Search records for the identity resolver."""
    if not isinstance(person, dict):
        return {}
    full_name = person.get("full_name") or " ".join(
        str(person.get(key) or "").strip() for key in ("first_name", "last_name")
        if person.get(key)
    )
    def nested_name(item, key, fallback):
        value = item.get(key)
        if isinstance(value, dict):
            return value.get("name")
        return value or item.get(fallback)

    organizations = _unique([
        person.get("job_company_name"),
        *(
            nested_name(item, "company", "company_name")
            for item in _items(person.get("experience")) if isinstance(item, dict)
        ),
        *(
            nested_name(item, "school", "school_name")
            for item in _items(person.get("education")) if isinstance(item, dict)
        ),
    ], limit=15)
    roles = _unique([
        person.get("job_title"),
        *(
            nested_name(item, "title", "title_name")
            for item in _items(person.get("experience")) if isinstance(item, dict)
        ),
    ], limit=10)
    return {
        "full_name": str(full_name or "").strip(),
        "location_name": person.get("location_name") or ", ".join(
            str(person.get(key) or "").strip()
            for key in ("location_locality", "location_region") if person.get(key)
        ),
        "job_title": person.get("job_title") or "",
        "job_company_name": person.get("job_company_name") or "",
        "roles": roles, "organizations": organizations,
        "provider_person_id": str(person.get("id") or ""),
        "provider": provider, "confidence": max(0.0, min(1.0, float(score or 0))),
        "matched_inputs": matched or [],
    }


def _discover_identity(candidate: dict) -> dict:
    """Use PDL Search for masked surnames and Identify for broad full names."""
    headers = {
        "X-Api-Key": config.PDL_API_KEY, "Accept": "application/json",
        "Content-Type": "application/json",
    }
    lookup_name = _lookup_name(candidate)
    tokens = _normal(lookup_name).split()
    if len(tokens) < 2:
        return {
            "status": "skipped", "provider": "people_data_labs_discovery",
            "people": [], "credits_spent": 0,
            "message": "A full two-part person name is required for identity discovery.",
        }
    companies, schools = _profile_match_inputs(candidate)
    partial = len(tokens) >= 2 and len(tokens[-1]) == 1
    try:
        if partial:
            must = [{"match": {"first_name": tokens[0]}}]
            must.append({"prefix": {"last_name": tokens[-1]}})
            _city_value, state = _location_parts(candidate.get("location", ""))
            filters = [{"term": {"location_region": state}}] if state else []
            body = {
                "query": {"bool": {"must": must, "filter": filters}},
                "size": 5, "dataset": "resume,mobile_phone",
            }
            response = _client().post(config.PDL_SEARCH_URL, json=body, headers=headers)
            payload = response.json() if response.content else {}
            people = payload.get("data") if isinstance(payload, dict) else []
            provider = "people_data_labs_search"
            normalized = [
                _discovery_person(person, provider, 0.75, ["name", "region"])
                for person in people or []
            ]
        else:
            params = {
                "name": lookup_name,
                "location": candidate.get("location", ""), "size": 10,
            }
            if companies:
                params["company"] = companies
            if schools:
                params["school"] = schools
            response = _client().get(config.PDL_IDENTIFY_URL, params=params, headers=headers)
            payload = response.json() if response.content else {}
            matches = payload.get("matches") if isinstance(payload, dict) else []
            provider = "people_data_labs_identify"
            normalized = []
            for item in matches or []:
                person = item.get("data") if isinstance(item, dict) else {}
                raw_score = item.get("match_score") if isinstance(item, dict) else 0
                try:
                    numeric = float(raw_score or 0)
                    numeric = numeric / 100.0 if numeric > 1 else numeric
                except (TypeError, ValueError):
                    numeric = 0.0
                normalized.append(_discovery_person(
                    person, provider, numeric,
                    item.get("matched_on") if isinstance(item, dict) else [],
                ))
        credits = _credits(response, 1 if response.status_code in (200, 404) else 0)
        if response.status_code not in (200, 404):
            return {
                "status": "error", "provider": provider, "people": [],
                "credits_spent": credits, "error": f"PDL discovery returned HTTP {response.status_code}.",
            }
        return {
            "status": "success" if normalized else "no_match",
            "provider": provider, "people": [item for item in normalized if item.get("full_name")],
            "credits_spent": credits,
        }
    except httpx.TimeoutException:
        return {"status": "error", "provider": "people_data_labs_discovery", "people": [], "credits_spent": 0, "error": "PDL discovery timed out."}
    except Exception as exc:
        return {"status": "error", "provider": "people_data_labs_discovery", "people": [], "credits_spent": 0, "error": f"PDL discovery failed ({type(exc).__name__})."}


def resolve_identity(cid: int, run_id: str, *, allow_paid=True) -> dict:
    """Resolve one incomplete identity and persist auditable evidence."""
    if not config.IDENTITY_RESOLUTION_ENABLED:
        return {
            "status": "disabled", "provider": "identity_resolution",
            "credits_spent": 0, "message": "Identity resolution is disabled.",
        }
    candidate = store.get_candidate(cid)
    if not candidate:
        return {"status": "error", "error": "candidate not found", "credits_spent": 0}
    discover = None
    if allow_paid and configured():
        spent = store.provider_run_credits(_PROVIDER, run_id)
        if config.PDL_RUN_CREDIT_LIMIT <= 0 or spent < config.PDL_RUN_CREDIT_LIMIT:
            discover = _discover_identity
    resolution = identity_resolution.resolve(candidate, discover=discover)
    identity_resolution.persist(cid, resolution)
    credits = int(resolution.get("credits_spent") or 0)
    if credits:
        discovery_key = "identity:" + hashlib.sha256(
            f"{_REQUEST_POLICY_VERSION}|{_normal(candidate.get('name'))}|{_normal(candidate.get('location'))}".encode()
        ).hexdigest()
        store.save_provider_lookup(
            _PROVIDER, run_id, cid, discovery_key, resolution.get("status") or "no_match",
            resolution, credits,
        )
    return resolution


def _person_from_payload(payload) -> dict:
    person = payload.get("data") if isinstance(payload, dict) else None
    return person if isinstance(person, dict) else {}


def _matched_inputs(payload) -> list[str]:
    """Normalize PDL's top-level ``matched`` evidence list."""
    matched = payload.get("matched") if isinstance(payload, dict) else None
    if not isinstance(matched, list):
        return []
    return _unique(str(value).strip().casefold() for value in matched)


def _build_result(candidate: dict, person: dict, likelihood, matched_inputs=None) -> dict:
    """Normalize one PDL record; identity validation happens before storage."""
    matched_inputs = _unique(matched_inputs or [])
    matched_name = str(person.get("full_name") or "").strip()
    if not matched_name:
        matched_name = " ".join(
            str(value).strip() for value in (
                person.get("first_name"), person.get("last_name")
            ) if value
        )
    returned_location = str(person.get("location_name") or "").strip()
    if not returned_location:
        returned_location = ", ".join(
            str(value).strip() for value in (
                person.get("location_locality"), person.get("location_region")
            ) if value
        )
    provider_job_title = str(person.get("job_title") or "").strip()
    provider_company = str(person.get("job_company_name") or "").strip()
    def nested_name(item, key, fallback):
        value = item.get(key)
        if isinstance(value, dict):
            return value.get("name")
        return value or item.get(fallback)
    provider_roles = _unique(
        [provider_job_title] + [
            nested_name(item, "title", "title_name")
            for item in _items(person.get("experience")) if isinstance(item, dict)
        ], limit=10,
    )
    provider_organizations = _unique(
        [provider_company] + [
            nested_name(item, "company", "company_name")
            for item in _items(person.get("experience")) if isinstance(item, dict)
        ] + [
            nested_name(item, "school", "school_name")
            for item in _items(person.get("education")) if isinstance(item, dict)
        ], limit=15,
    )
    pdl_id = str(person.get("id") or "").strip()
    try:
        email_evidence = _email_evidence(person)
        phone_evidence = _phone_evidence(person)
        address_evidence = _address_evidence(person)
        alias_evidence = _name_alias_evidence(person)
        source_alias_evidence = _source_name_alias_evidence(candidate)
        profile_evidence = _profile_evidence(person)
        profile_url = _matching_profile_url(candidate, person)
    except Exception as exc:
        email_evidence, phone_evidence, address_evidence = [], [], []
        alias_evidence, source_alias_evidence = [], []
        profile_evidence, profile_url = [], ""
        normalization_error = type(exc).__name__
    else:
        normalization_error = ""
    profile_urls = _unique(
        (item.get("url") for item in profile_evidence), limit=50,
    )
    name_aliases = _unique(
        (item.get("value") for item in alias_evidence), limit=30,
    )
    try:
        likelihood_score = max(0, min(10, int(likelihood or 0)))
    except (TypeError, ValueError):
        likelihood_score = 0
    try:
        confidence = max(0.0, min(1.0, float(likelihood_score) / 10.0))
    except (TypeError, ValueError):
        confidence = 0.0

    def rejected(message):
        return {
            "source": _PROVIDER, "status": "no_match", "matched_name": matched_name,
            "emails": [], "phones": [], "addresses": [], "confidence": confidence,
            "matched_inputs": matched_inputs, "provider_location": returned_location,
            "provider_job_title": provider_job_title,
            "provider_company": provider_company, "pdl_id": pdl_id,
            "profile_url": profile_url, "likelihood": likelihood_score,
            "provider_roles": provider_roles,
            "provider_organizations": provider_organizations,
            "name_aliases": name_aliases,
            "name_alias_evidence": alias_evidence,
            "source_name_aliases": _unique(
                item.get("value") for item in source_alias_evidence
            ),
            "source_name_alias_evidence": source_alias_evidence,
            "profile_urls": profile_urls,
            "profile_evidence": profile_evidence,
            "address_evidence": address_evidence,
            "email_evidence": email_evidence,
            "phone_evidence": phone_evidence,
            "required_field": _REQUIRED_PROFILE_FIELDS,
            "message": message,
        }

    if normalization_error:
        return rejected(f"PDL response fields could not be normalized ({normalization_error}).")
    emails = _unique(
        item.get("value") for item in email_evidence
        if item.get("accepted_for_outreach") is True
    )
    phone_candidates = _unique(
        item.get("value") for item in phone_evidence if item.get("value")
    )
    phones = phone_policy.preferred_phone_values({
        "phones": phone_candidates,
        "associated_phone_evidence": phone_evidence,
    })
    addresses = _unique(
        (item.get("value") for item in address_evidence), limit=50,
    )
    if not (emails or phones):
        return rejected("PDL returned no usable email or phone value.")
    selected_phone_keys = {
        re.sub(r"\D", "", value)[-10:]
        for value in phones if re.sub(r"\D", "", value)
    }
    usable_phone_evidence = [
        item for item in phone_evidence
        if re.sub(r"\D", "", str(item.get("value") or ""))[-10:] in selected_phone_keys
    ]
    return {
        "source": _PROVIDER, "status": "success", "matched_name": matched_name,
        "emails": emails, "phones": phones, "addresses": addresses,
        "confidence": confidence, "matched_inputs": matched_inputs,
        "provider_location": returned_location,
        "provider_job_title": provider_job_title,
        "provider_company": provider_company, "pdl_id": pdl_id,
        "profile_url": profile_url, "likelihood": likelihood_score,
        "provider_roles": provider_roles,
        "provider_organizations": provider_organizations,
        "name_aliases": name_aliases,
        "name_alias_evidence": alias_evidence,
        "source_name_aliases": _unique(
            item.get("value") for item in source_alias_evidence
        ),
        "source_name_alias_evidence": source_alias_evidence,
        "profile_urls": profile_urls,
        "profile_evidence": profile_evidence,
        "address_evidence": address_evidence,
        "email_evidence": email_evidence,
        "phone_policy": phone_policy.selected_phone_policy({
            "phones": phones, "phone_evidence": usable_phone_evidence,
        }),
        # This compatibility field is intentionally restricted because the
        # trust layer treats every entry here as potential contact material.
        "phone_evidence": usable_phone_evidence,
        # Historical/broad phones stay backend-only identity evidence.
        "associated_phone_evidence": phone_evidence,
        "required_field": _REQUIRED_PROFILE_FIELDS,
    }


def _stored_response(candidate: dict, result: dict, stored: dict | None = None, **extra) -> dict:
    refreshed = stored or candidate
    return {
        **result,
        "provider": _PROVIDER,
        "emails": result.get("emails", []),
        "phones": result.get("phones", []),
        "addresses": result.get("addresses", []),
        "stored_emails": refreshed.get("emails", []),
        "stored_phones": refreshed.get("phones", []),
        "stored_addresses": refreshed.get("addresses", []),
        # A successful response reaches this helper only after the current
        # identity/likelihood gate and provider-originated contact filtering.
        "contacts_trusted": bool(
            result.get("status") == "success"
            and (result.get("emails") or result.get("phones"))
        ),
        **extra,
    }


def _trusted_contact_source(candidate: dict) -> dict | None:
    """Return a fresh, automatically-usable contact record without a paid call."""
    sources = [candidate]
    candidate_id = int(candidate.get("id") or 0)
    master_id = int(candidate.get("master_candidate_id") or 0)
    if master_id and master_id != candidate_id:
        master = store.get_candidate(master_id)
        if master:
            sources.append(master)
    provider_duplicate = store.get_candidate_by_provider_person_id(
        candidate.get("provider_person_id") or "", exclude_id=candidate_id,
    )
    if provider_duplicate and all(
        int(source.get("id") or 0) != int(provider_duplicate.get("id") or 0)
        for source in sources
    ):
        sources.append(provider_duplicate)

    for source in sources:
        verification_record = source.get("verification") or {}
        evidence = verification_record.get("evidence") or {}
        contact_evidence = evidence.get("contact_verification") or {}
        selected_policy = str(
            evidence.get("phone_policy") or contact_evidence.get("phone_policy") or ""
        )
        provider_contacts = trust_policy.trusted_provider_contacts(candidate, source)
        if provider_contacts:
            provider_contacts = store.filter_dnc_groups(provider_contacts)
        if (
            provider_contacts and (provider_contacts.get("emails") or provider_contacts.get("phones"))
            and float(source.get("contact_expires_at") or 0) > time.time()
            and (
                not provider_contacts.get("phones")
                or selected_policy in {
                    phone_policy.MOBILE_PHONE_POLICY,
                    phone_policy.OTHER_PHONE_POLICY,
                }
            )
        ):
            return {**source, "_trusted_provider_contacts": provider_contacts}
    return None


def _already_complete(candidate: dict) -> dict | None:
    """Reuse a fresh trusted contact set from the candidate/master record."""
    source = _trusted_contact_source(candidate)
    if not source:
        return None
    verification = source.get("verification") or {}
    provider_contacts = source.get("_trusted_provider_contacts") or {}
    evidence = verification.get("evidence") or {}
    reused_from_other_candidate = int(source.get("id") or 0) != int(candidate.get("id") or 0)
    if reused_from_other_candidate:
        reused_evidence = dict(evidence)
        reused_evidence["internal_contact_reuse"] = {
            "source_candidate_id": int(source["id"]),
            "reused_at": time.time(),
        }
        # Bind the copied verification to this captured source identity. Without
        # this rewrite, the next lookup would compare the alias candidate with
        # the master candidate's fingerprint and reject its own trusted cache.
        reused_evidence["contact_trust_policy"] = trust_policy.CONTACT_TRUST_POLICY
        reused_evidence["source_identity_fingerprint"] = (
            trust_policy.source_identity_fingerprint(candidate)
        )
        verification = {**verification, "evidence": reused_evidence}
    fields = {
        "emails": list(provider_contacts.get("emails") or []),
        "phones": list(provider_contacts.get("phones") or []),
        "addresses": list(provider_contacts.get("addresses") or []),
        "enrich_status": "success",
        "contact_verified_at": float(source.get("contact_verified_at") or time.time()),
        "contact_expires_at": float(source.get("contact_expires_at") or 0),
    }
    if reused_from_other_candidate:
        fields["verification"] = verification
    if candidate.get("stage") == "new":
        fields["stage"] = "enriched"
    store.update_candidate(int(candidate["id"]), **fields)
    candidate = {**candidate, **fields}
    contact_evidence = (verification.get("evidence") or {}).get("contact_verification") or {}
    return _stored_response(candidate, {
        "status": "success", "matched_name": candidate.get("name", ""),
        "confidence": candidate.get("confidence", 0), "credits_spent": 0,
        "cached": True, "trusted_cache": True,
        "message": "Reused a fresh verified contact from the internal candidate database.",
        "emails": provider_contacts.get("emails") or [],
        "phones": provider_contacts.get("phones") or [],
        "addresses": provider_contacts.get("addresses") or [],
        "matched_inputs": evidence.get("matched_inputs") or [],
        "provider_location": evidence.get("provider_location") or "",
        "provider_job_title": evidence.get("provider_job_title") or "",
        "provider_company": evidence.get("provider_company") or "",
        "pdl_id": evidence.get("pdl_id") or "",
        "likelihood": evidence.get("pdl_likelihood") or 0,
        "profile_url": verification.get("profile_url") or "",
        "verification": verification,
        "contact_verification": contact_evidence,
        "contact_source_candidate_id": int(source.get("id") or 0),
    })


def _skip_reason(candidate: dict) -> str:
    """Why this candidate must not reach PDL, or '' when it may."""
    name = _lookup_name(candidate)
    name_parts = _normal(name).split()
    if (
        not _person_name_matches(name, name)
        or len(name_parts[0]) < 2 or len(name_parts[-1]) < 2
    ):
        return "A full candidate name is required for PDL lookup."
    if _social_profile(candidate):
        return ""
    city, state = _location_parts(candidate.get("location", ""))
    if not city or not state:
        return "An exact city and state are required for PDL lookup."
    return ""


def _retry_cached_local_rejection(result: dict) -> bool:
    """Never replay results rejected by an older local validation policy."""
    if str(result.get("reason_code") or "") == "local_validation_rejected":
        return True
    if str(result.get("reason_code") or "") == "matched_input_evidence_rejected":
        return True
    if str(result.get("reason_code") or "") == "returned_location_evidence_rejected":
        return True
    message = str(result.get("message") or "").casefold()
    return (
        "identity/contact evidence" in message
        or "identity evidence was rejected" in message
        or "did not confirm both the supplied name and location" in message
    )


def _with_lookup_diagnostics(
    result: dict, candidate: dict, attempted: list[str], selected: str = "",
) -> dict:
    """Attach backend-only, non-secret strategy evidence to one result."""
    attempted = list(dict.fromkeys(str(value or "").strip() for value in attempted))
    output = {
        **dict(result or {}),
        "lookup_strategy": str(selected or ""),
        "lookup_attempt_count": len(attempted),
        "lookup_attempted_strategies": attempted,
    }
    output["lookup_evidence"] = _lookup_evidence(
        candidate, attempted=attempted, selected=selected,
    )
    return output


def _provider_person_id(result: dict) -> str:
    """Return the stable PDL owner key used to deduplicate staged responses."""
    return str((result or {}).get("pdl_id") or "").strip().casefold()


def _provider_evidence_fingerprint(result: dict) -> tuple:
    """Identify one materially identical PDL observation.

    A stable person ID identifies the owner, not the query evidence. PDL's
    ``matched`` list and likelihood are request-specific and can make the same
    owner verifiable on a later, less stale query. Only identical observations
    are skipped; repeated owners are never counted as independent proof.
    """
    contacts = tuple(sorted(
        store.contact_key(value)
        for value in [
            *(result.get("emails") or []), *(result.get("phones") or []),
        ]
        if store.contact_key(value)
    ))
    return (
        _provider_person_id(result),
        tuple(sorted(_normal(value) for value in result.get("matched_inputs") or [])),
        int(result.get("likelihood") or 0),
        _normal(result.get("matched_name") or ""),
        _normal(result.get("provider_location") or ""),
        contacts,
    )


def _retry_evidence_rank(result: dict) -> tuple:
    matched = {_normal(value) for value in result.get("matched_inputs") or []}
    anchor_weight = sum({
        "profile": 5, "location": 4, "locality": 4,
        "street address": 4, "region": 2, "company": 2, "school": 2,
        "name": 1,
    }.get(value, 0) for value in matched)
    return (
        1 if result.get("status") == "success" else 0,
        anchor_weight,
        int(result.get("likelihood") or 0),
        len(result.get("emails") or []) + len(result.get("phones") or []),
    )


def _prefer_retry_evidence(current: tuple | None, result: dict, strategy: str):
    """Keep the most useful rejected response for the final audit record."""
    candidate = (dict(result or {}), str(strategy or ""))
    if current is None:
        return candidate
    current_result = current[0]
    if _retry_evidence_rank(result) > _retry_evidence_rank(current_result):
        return candidate
    return current


def _local_result_accepted(candidate: dict, result: dict) -> tuple[bool, str]:
    """Run the same identity/contact gate as persistence, without writing.

    A provider HTTP 200 is discovery evidence, not automatic acceptance.  DNC
    filtering remains part of the final merge; an empty pre-resolved block set
    keeps this probe deterministic and avoids one database lookup per attempt.
    """
    if str((result or {}).get("status") or "") != "success":
        return False, ""
    probe_candidate = {
        "id": 0, "stage": "new", "emails": [], "phones": [],
        "addresses": [], "verification": {}, "identity_evidence": {},
        **dict(candidate or {}),
    }
    try:
        assessed = enrich.save_provider_result(
            int(probe_candidate.get("id") or 0), result,
            candidate=probe_candidate,
            persist=False, dnc_blocked=set(),
        )
    except Exception as exc:
        return False, type(exc).__name__
    return assessed.get("enrich_status") == "success", ""


def _run_single_query_plan(
    candidate: dict, *, available_credits: int | None = None,
) -> tuple[dict, int]:
    """Run one staged plan until a contact passes local deterministic gates."""
    attempted = []
    credits = 0
    seen_evidence = set()
    best_rejected = None
    for attempt in _query_attempts(candidate):
        if available_credits is not None and credits >= available_credits:
            if best_rejected is not None:
                result, selected = best_rejected
                return _with_lookup_diagnostics(
                    result, candidate, attempted, selected=selected,
                ), credits
            return _with_lookup_diagnostics({
                "status": "budget_exhausted", "provider": _PROVIDER,
                "credits_spent": credits,
                "message": "PDL lookup stopped at the configured run credit limit.",
            }, candidate, attempted), credits
        strategy = attempt["strategy"]
        attempted.append(strategy)
        try:
            response, payload = _call(*attempt["identity"])
        except httpx.TimeoutException:
            return _with_lookup_diagnostics({
                "status": "error", "provider": _PROVIDER,
                "error": "People Data Labs timed out.",
            }, candidate, attempted), credits
        except httpx.HTTPError as exc:
            return _with_lookup_diagnostics({
                "status": "error", "provider": _PROVIDER,
                "error": f"People Data Labs request failed: {type(exc).__name__}.",
            }, candidate, attempted), credits
        except Exception as exc:
            return _with_lookup_diagnostics({
                "status": "error", "provider": _PROVIDER,
                "error": f"People Data Labs request processing failed ({type(exc).__name__}).",
            }, candidate, attempted), credits

        spent = _credits(response, 1 if response.status_code == 200 else 0)
        credits += spent
        if response.status_code == 404:
            continue
        if response.status_code >= 400:
            return _with_lookup_diagnostics(
                _http_error(response, credits), candidate, attempted,
            ), credits
        result = _build_result(
            candidate, _person_from_payload(payload), payload.get("likelihood"),
            _matched_inputs(payload),
        )
        evidence_fingerprint = _provider_evidence_fingerprint(result)
        if evidence_fingerprint in seen_evidence:
            continue
        seen_evidence.add(evidence_fingerprint)
        best_rejected = _prefer_retry_evidence(best_rejected, result, strategy)
        accepted, validation_error = _local_result_accepted(candidate, result)
        if validation_error:
            return _with_lookup_diagnostics({
                "status": "error", "provider": _PROVIDER,
                "error": (
                    "People Data Labs local validation failed "
                    f"({validation_error})."
                ),
            }, candidate, attempted), credits
        if accepted:
            return _with_lookup_diagnostics(
                result, candidate, attempted, selected=strategy,
            ), credits

    if best_rejected is not None:
        result, selected = best_rejected
        return _with_lookup_diagnostics(
            result, candidate, attempted, selected=selected,
        ), credits

    return _with_lookup_diagnostics(_no_match_result(
        "PDL completed the staged lookup but returned no record meeting "
        "the usable-contact policy.", candidate,
    ), candidate, attempted), credits


def enrich_candidate(cid: int, run_id: str) -> dict:
    """Use PDL only when needed, cache the response, and merge safe contacts."""
    if not configured():
        return {
            "status": "disabled", "provider": _PROVIDER, "credits_spent": 0,
            "message": "People Data Labs enrichment is not configured.",
        }
    if not _RUN_ID_RE.fullmatch(str(run_id or "")):
        return {
            "status": "error", "provider": _PROVIDER, "credits_spent": 0,
            "error": "A valid lookup run ID is required.",
        }
    with _lookup_slot() as acquired:
        if not acquired:
            return {
                "status": "error", "provider": _PROVIDER, "credits_spent": 0,
                "error": "Another PDL request is still finishing. Wait a moment and retry once.",
            }
        # Candidate, cache, and budget checks stay inside the lock so simultaneous
        # requests cannot purchase the same enrichment or overrun the run budget.
        preliminary = store.get_candidate(cid)
        if not preliminary:
            return {"status": "error", "error": "candidate not found", "credits_spent": 0}
        identity_credits = 0
        if preliminary.get("identity_status") not in ("verified", "recruiter_confirmed"):
            internal = identity_resolution.resolve_internal(preliminary)
            if internal.get("status") == "verified":
                internal["attempts"] = [internal]
                identity_resolution.persist(cid, internal)
                preliminary = store.get_candidate(cid) or preliminary
        preliminary_reason = _skip_reason(_lookup_candidate(preliminary))
        if preliminary_reason:
            resolved = resolve_identity(cid, run_id)
            identity_credits = int(resolved.get("credits_spent") or 0)
            if resolved.get("status") != "verified":
                return {
                    "status": "review" if resolved.get("status") == "review" else "skipped",
                    "provider": _PROVIDER, "credits_spent": identity_credits,
                    "identity_resolution": resolved,
                    "message": (
                        resolved.get("message")
                        or "The captured identity could not be uniquely resolved before contact lookup."
                    ),
                }
        preflight = store.provider_lookup_preflight(
            _PROVIDER, run_id, cid, _request_key,
        )
        candidate = _lookup_candidate(preflight["candidate"] or {})
        if not candidate:
            return {"status": "error", "error": "candidate not found", "credits_spent": 0}
        complete = _already_complete(candidate)
        if complete:
            return complete
        reason = _skip_reason(candidate)
        if reason:
            return {
                "status": "skipped", "provider": _PROVIDER, "credits_spent": 0,
                "message": reason,
            }

        request_key = preflight["request_key"]
        cached = preflight["cached"]
        if cached and time.time() - float(cached.get("updated") or 0) <= config.PDL_CACHE_TTL_SECONDS:
            cached_result = _upgrade_legacy_mobile_no_match(
                dict(cached.get("result") or {}), candidate,
            )
            if _retry_cached_local_rejection(cached_result):
                cached = None
            elif cached_result.get("status") == "success":
                saved = enrich.save_provider_result(cid, cached_result, candidate=candidate)
                if saved.get("enrich_status") == "success":
                    stored = {
                        "emails": saved.get("stored_emails", candidate.get("emails", [])),
                        "phones": saved.get("stored_phones", candidate.get("phones", [])),
                        "addresses": saved.get("stored_addresses", candidate.get("addresses", [])),
                    }
                    return _stored_response(
                        candidate, cached_result, stored=stored,
                        cached=True, credits_spent=0,
                    )
                return {
                    "status": "no_match", "provider": _PROVIDER, "cached": True,
                    "credits_spent": 0, "emails": [], "phones": [], "addresses": [],
                    "message": saved.get("error") or "Cached PDL contact evidence was rejected.",
                }
            elif cached is not None:
                return {**cached_result, "provider": _PROVIDER, "cached": True, "credits_spent": 0}

        spent = preflight["run_credits"]
        if config.PDL_RUN_CREDIT_LIMIT > 0 and spent >= config.PDL_RUN_CREDIT_LIMIT:
            return {
                "status": "budget_exhausted", "provider": _PROVIDER,
                "credits_spent": 0, "run_credits_spent": spent,
                "message": f"PDL lookup stopped at the {config.PDL_RUN_CREDIT_LIMIT}-credit run limit.",
            }

        available_credits = (
            max(0, config.PDL_RUN_CREDIT_LIMIT - spent)
            if config.PDL_RUN_CREDIT_LIMIT > 0 else None
        )
        result, call_credits = _run_single_query_plan(
            candidate, available_credits=available_credits,
        )
        if result.get("status") in {"error", "budget_exhausted"}:
            return {
                **result,
                "credits_spent": call_credits + identity_credits,
                "identity_credits_spent": identity_credits,
                "run_credits_spent": spent + call_credits,
            }

        total_spent = spent + call_credits
        if result["status"] != "success":
            try:
                store.finalize_provider_lookup(
                    _PROVIDER, run_id, cid, request_key,
                    result["status"], result, call_credits,
                )
            except Exception as exc:
                return {
                    "status": "error", "provider": _PROVIDER,
                    "credits_spent": call_credits,
                    "run_credits_spent": total_spent,
                    "error": f"PDL cache write failed ({type(exc).__name__}).",
                }
            return {
                **result, "provider": _PROVIDER, "cached": False,
                "credits_spent": call_credits + identity_credits,
                "identity_credits_spent": identity_credits,
                "run_credits_spent": total_spent,
            }

        try:
            saved = enrich.save_provider_result(
                cid, result, candidate=candidate, persist=False,
            )
        except Exception as exc:
            return {
                "status": "error", "provider": _PROVIDER,
                "credits_spent": call_credits, "run_credits_spent": total_spent,
                "emails": [], "phones": [], "addresses": [],
                "error": f"PDL candidate merge failed ({type(exc).__name__}).",
            }
        if saved.get("enrich_status") != "success":
            rejected = {
                "status": "no_match", "provider": _PROVIDER, "cached": False,
                "credits_spent": call_credits, "run_credits_spent": total_spent,
                "confidence": saved.get("confidence", 0),
                "emails": [], "phones": [], "addresses": [],
                "reason_code": "local_validation_rejected",
                "lookup_strategy": result.get("lookup_strategy", ""),
                "lookup_attempt_count": result.get("lookup_attempt_count", 0),
                "lookup_attempted_strategies": result.get(
                    "lookup_attempted_strategies", []
                ),
                "lookup_evidence": result.get("lookup_evidence")
                    or _lookup_evidence(candidate),
                "verification": saved.get("verification", {}),
                "message": saved.get("error") or "PDL identity evidence was rejected.",
            }
            try:
                store.finalize_provider_lookup(
                    _PROVIDER, run_id, cid, request_key, "no_match",
                    {**rejected, "source": _PROVIDER}, call_credits,
                    candidate_updates=saved.get("candidate_updates") or {},
                )
            except Exception as exc:
                return {
                    "status": "error", "provider": _PROVIDER,
                    "credits_spent": call_credits, "run_credits_spent": total_spent,
                    "emails": [], "phones": [], "addresses": [],
                    "error": f"PDL cache write failed ({type(exc).__name__}).",
                }
            return rejected
        try:
            refreshed = store.finalize_provider_lookup(
                _PROVIDER, run_id, cid, request_key, "success", result, call_credits,
                candidate_updates=saved.get("candidate_updates") or {},
            )
        except Exception as exc:
            return {
                "status": "error", "provider": _PROVIDER,
                "credits_spent": call_credits, "run_credits_spent": total_spent,
                "emails": [], "phones": [], "addresses": [],
                "error": f"PDL result storage failed ({type(exc).__name__}).",
            }
        if not refreshed:
            return {
                "status": "error", "provider": _PROVIDER,
                "credits_spent": call_credits, "run_credits_spent": total_spent,
                "emails": [], "phones": [], "addresses": [],
                "error": "PDL result storage could not update the candidate.",
            }
        stored = {
            "emails": refreshed.get("emails", saved.get("stored_emails", [])),
            "phones": refreshed.get("phones", saved.get("stored_phones", [])),
            "addresses": refreshed.get("addresses", saved.get("stored_addresses", [])),
        }
        return _stored_response(candidate, {
            **result,
            "status": "success", "matched_name": result["matched_name"],
            "confidence": saved.get("confidence", result["confidence"]),
            "verification": saved.get("verification", {}),
            "emails": saved.get("emails", []),
            "phones": saved.get("phones", []),
            "addresses": saved.get("addresses", []),
            "cached": False, "credits_spent": call_credits,
            "run_credits_spent": total_spent,
        }, stored=stored)


# ---------------------------------------------------------------------------
# Batch lookup
#
# The side panel used to POST one request per selected candidate, and each of
# those served one PDL person-enrich call behind a process-wide lock. A 50-card
# selection therefore cost 50 browser round trips, 50 provider calls, and about
# 150 Neon queries. The batch path below answers the same selection with three
# preflight queries, at most one bulk provider call per 100 identities, and a
# single write.
# ---------------------------------------------------------------------------

def _batch_error(cids, payload: dict) -> dict:
    return {
        "results": {int(cid): dict(payload) for cid in cids},
        "run_credits_spent": 0,
        "credits_spent": 0,
    }


def _lookup_evidence(
    candidate: dict, *, attempted: list[str] | None = None, selected: str = "",
) -> dict:
    """Describe the non-secret identity inputs sent to PDL for audit/UI use."""
    companies, schools = _profile_match_inputs(candidate)
    profile = _social_profile(candidate)
    platform = str(candidate.get("source") or "").strip().casefold()
    source_aliases = _source_name_alias_evidence(candidate)
    return {
        "provider_queried": True,
        "required_field": _REQUIRED_PROFILE_FIELDS,
        "name_supplied": bool(_lookup_name(candidate)),
        "location_supplied": bool(str(candidate.get("location") or "").strip()),
        "profile_supplied": bool(profile),
        "profile_platform": platform if profile and platform in {"facebook", "linkedin"} else "",
        "company_count": len(companies),
        "school_count": len(schools),
        "explicit_alternate_name_count": len(source_aliases),
        "staged_retry_enabled": bool(config.PDL_STAGED_RETRY_ENABLED),
        "strategy_attempt_count": len(attempted or []),
        "attempted_strategies": list(attempted or []),
        "selected_strategy": str(selected or ""),
    }


def _no_match_result(message: str, candidate: dict | None = None) -> dict:
    result = {
        "source": _PROVIDER, "status": "no_match", "matched_name": "",
        "emails": [], "phones": [], "addresses": [], "confidence": 0,
        "message": message,
        "reason_code": "pdl_required_contact_no_match",
    }
    if candidate:
        result["lookup_evidence"] = _lookup_evidence(candidate)
    return result


def _upgrade_legacy_mobile_no_match(result: dict, candidate: dict) -> dict:
    """Add current diagnostics when replaying a pre-diagnostics 404 cache row."""
    upgraded = dict(result or {})
    message = str(upgraded.get("message") or "")
    if (
        upgraded.get("status") == "no_match"
        and (
            "profile with a mobile phone" in message.casefold()
            or "required=mobile_phone" in message.casefold()
        )
    ):
        upgraded.update(_no_match_result(
            "PDL completed the lookup but returned no record meeting "
            "the usable-contact policy. This can mean no matching identity "
            "or a matching record without a dedicated mobile or current email.",
            candidate,
        ))
    return upgraded


def _http_error(response: httpx.Response, credits: int) -> dict:
    if response.status_code in (401, 403):
        error = f"People Data Labs authentication failed ({response.status_code})."
    elif response.status_code == 429:
        error = "People Data Labs rate or account limit was reached."
    else:
        error = f"People Data Labs returned HTTP {response.status_code}."
    return {
        "status": "error", "provider": _PROVIDER,
        "credits_spent": credits, "error": error,
    }


def _fetch_single(keys, identities) -> tuple[dict, dict, int]:
    """Per-identity fallback used when the bulk endpoint is unavailable."""
    fetched, failures, credits = {}, {}, 0
    for key, identity in zip(keys, identities):
        name, location, companies, schools = identity[:4]
        profile = identity[4] if len(identity) > 4 else ""
        try:
            response, payload = (
                _call(name, location, companies, schools, profile)
                if profile else _call(name, location, companies, schools)
            )
        except httpx.TimeoutException:
            failures[key] = {
                "status": "error", "provider": _PROVIDER, "credits_spent": 0,
                "error": "People Data Labs timed out.",
            }
            continue
        except Exception as exc:
            failures[key] = {
                "status": "error", "provider": _PROVIDER, "credits_spent": 0,
                "error": f"People Data Labs request failed ({type(exc).__name__}).",
            }
            continue
        spent = _credits(response, 1 if response.status_code == 200 else 0)
        credits += spent
        if response.status_code == 200:
            fetched[key] = (
                _person_from_payload(payload), payload.get("likelihood"), spent,
                _matched_inputs(payload),
            )
        elif response.status_code == 404:
            fetched[key] = (None, 0, spent, [])
        else:
            failures[key] = _http_error(response, spent)
    return fetched, failures, credits


def _fetch_bulk(keys, identities) -> tuple[dict, dict, int, bool]:
    """Resolve one chunk of identities. Returns (fetched, failures, credits, bulk_ok)."""
    try:
        response, items = _bulk_call(identities)
    except httpx.TimeoutException:
        return {}, {key: {
            "status": "error", "provider": _PROVIDER, "credits_spent": 0,
            "error": "People Data Labs timed out.",
        } for key in keys}, 0, True
    except Exception as exc:
        return {}, {key: {
            "status": "error", "provider": _PROVIDER, "credits_spent": 0,
            "error": f"People Data Labs bulk request failed ({type(exc).__name__}).",
        } for key in keys}, 0, True

    reported = _credits(response, 0)
    if response.status_code != 200:
        # An account or plan without bulk access must still get its results, so
        # fall back to individual calls once and remember the answer.
        if response.status_code in (400, 404, 405, 501) and reported == 0:
            fetched, failures, credits = _fetch_single(keys, identities)
            return fetched, failures, credits, False
        return {}, {key: _http_error(response, 0) for key in keys}, reported, True

    fetched, failures = {}, {}
    for key, item in zip(keys, items):
        if not isinstance(item, dict):
            failures[key] = {
                "status": "error", "provider": _PROVIDER, "credits_spent": 0,
                "error": "People Data Labs returned an unreadable bulk entry.",
            }
            continue
        try:
            status = int(item.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        if status == 200:
            fetched[key] = (
                _person_from_payload(item), item.get("likelihood"), 1,
                _matched_inputs(item),
            )
        elif status == 404:
            fetched[key] = (None, 0, 0, [])
        else:
            failures[key] = {
                "status": "error", "provider": _PROVIDER, "credits_spent": 0,
                "error": f"People Data Labs returned HTTP {status} for this identity.",
            }
    for key in keys[len(items):]:
        failures[key] = {
            "status": "error", "provider": _PROVIDER, "credits_spent": 0,
            "error": "People Data Labs omitted this identity from the bulk response.",
        }
    return fetched, failures, reported, True


def _candidate_with_location_override(candidate: dict, location_overrides: dict) -> dict:
    """Return a transient lookup identity without changing stored location.

    ``_source_identity_location`` lets the trust layer bind accepted contacts
    to the captured profile's real/current location while provider matching is
    evaluated against a Facebook hometown during the controlled retry.
    """
    candidate_id = int(candidate.get("id") or 0)
    override = str((location_overrides or {}).get(candidate_id) or "").strip()
    if not override:
        return dict(candidate)
    return {
        **candidate,
        "_source_identity_location": candidate.get("location") or "",
        "location": override,
    }


def enrich_candidates(cids, run_id: str, *, location_overrides: dict | None = None) -> dict:
    """Enrich a whole selection with bulk PDL calls and one database write."""
    ordered = list(dict.fromkeys(int(value) for value in cids or []))
    overrides = {
        int(key): " ".join(str(value or "").split())[:500]
        for key, value in (location_overrides or {}).items()
        if str(value or "").strip()
    }
    if not ordered:
        return {"results": {}, "run_credits_spent": 0, "credits_spent": 0}
    if not configured():
        return _batch_error(ordered, {
            "status": "disabled", "provider": _PROVIDER, "credits_spent": 0,
            "message": "People Data Labs enrichment is not configured.",
        })
    if not _RUN_ID_RE.fullmatch(str(run_id or "")):
        return _batch_error(ordered, {
            "status": "error", "provider": _PROVIDER, "credits_spent": 0,
            "error": "A valid lookup run ID is required.",
        })
    with _lookup_slot() as acquired:
        if not acquired:
            return _batch_error(ordered, {
                "status": "error", "provider": _PROVIDER, "credits_spent": 0,
                "error": "Another PDL request is still finishing. Wait a moment and retry once.",
            })
        prepared_results = {}
        ready = []
        identity_credits = 0
        for cid in ordered:
            candidate = store.get_candidate(cid)
            if not candidate:
                prepared_results[cid] = {
                    "status": "error", "provider": _PROVIDER,
                    "credits_spent": 0, "error": "candidate not found",
                }
                continue
            if candidate.get("identity_status") not in ("verified", "recruiter_confirmed"):
                internal = identity_resolution.resolve_internal(candidate)
                if internal.get("status") == "verified":
                    internal["attempts"] = [internal]
                    identity_resolution.persist(cid, internal)
                    candidate = store.get_candidate(cid) or candidate
            candidate = _candidate_with_location_override(candidate, overrides)
            reason = _skip_reason(_lookup_candidate(candidate))
            if reason:
                resolved = resolve_identity(cid, run_id.strip())
                spent = int(resolved.get("credits_spent") or 0)
                identity_credits += spent
                if resolved.get("status") != "verified":
                    prepared_results[cid] = {
                        "status": "review" if resolved.get("status") == "review" else "skipped",
                        "provider": _PROVIDER, "credits_spent": spent,
                        "identity_resolution": resolved,
                        "message": resolved.get("message") or (
                            "The captured identity could not be uniquely resolved before contact lookup."
                        ),
                    }
                    continue
            ready.append(cid)
        if ready:
            batch = _run_batch(ready, run_id.strip(), location_overrides=overrides)
        else:
            run_credits = store.provider_run_credits(_PROVIDER, run_id.strip())
            batch = {
                "results": {}, "credits_spent": 0,
                "run_credits_spent": run_credits,
                "provider_reported_credits": identity_credits, "bulk": False,
            }
        batch["results"] = {**batch["results"], **prepared_results}
        batch["credits_spent"] = int(batch.get("credits_spent") or 0) + identity_credits
        batch["identity_credits_spent"] = identity_credits
        for result in batch["results"].values():
            result.setdefault("run_credits_spent", batch.get("run_credits_spent", 0))
        return batch


def _run_batch(cids, run_id: str, *, location_overrides: dict | None = None) -> dict:
    overrides = location_overrides or {}

    def lookup_candidate(candidate):
        return _lookup_candidate(_candidate_with_location_override(candidate, overrides))

    preflight = store.provider_lookup_preflight_batch(
        _PROVIDER, run_id, cids, lambda candidate: _request_key(lookup_candidate(candidate)),
    )
    candidates = {
        cid: lookup_candidate(candidate)
        for cid, candidate in preflight["candidates"].items()
    }
    request_keys = preflight["request_keys"]
    cached_lookups = preflight["cached"]
    run_credits = preflight["run_credits"]
    now = time.time()

    results = {}
    replays = []
    pending = {}
    plans = {}

    for cid in cids:
        candidate = candidates.get(cid)
        if not candidate:
            results[cid] = {
                "status": "error", "provider": _PROVIDER, "credits_spent": 0,
                "error": "candidate not found",
            }
            continue
        complete = _already_complete(candidate)
        if complete:
            results[cid] = complete
            continue
        reason = _skip_reason(candidate)
        if reason:
            results[cid] = {
                "status": "skipped", "provider": _PROVIDER, "credits_spent": 0,
                "message": reason,
            }
            continue
        key = request_keys.get(cid, "")
        cached = cached_lookups.get(key)
        if cached and now - float(cached.get("updated") or 0) <= config.PDL_CACHE_TTL_SECONDS:
            cached_result = _upgrade_legacy_mobile_no_match(
                dict(cached.get("result") or {}), candidate,
            )
            if _retry_cached_local_rejection(cached_result):
                cached = None
            elif cached_result.get("status") == "success":
                replays.append((cid, cached_result))
            else:
                results[cid] = {
                    **cached_result, "provider": _PROVIDER,
                    "cached": True, "credits_spent": 0,
                }
            if cached is not None:
                continue
        # Two selected cards can be the same person. Buy that identity once.
        if key not in pending:
            pending[key] = []
            plans[key] = _query_attempts(candidate)
        pending[key].append(cid)

    # Conservatively assume every unbought identity costs one credit, matching
    # the guard the sequential path applied before each call.
    remaining = (
        max(0, config.PDL_RUN_CREDIT_LIMIT - run_credits)
        if config.PDL_RUN_CREDIT_LIMIT > 0
        else len(pending)
    )
    billable = list(pending)[:remaining]
    for key in list(pending)[remaining:]:
        for cid in pending[key]:
            results[cid] = {
                "status": "budget_exhausted", "provider": _PROVIDER,
                "credits_spent": 0, "run_credits_spent": run_credits,
                "message": (
                    f"PDL lookup stopped at the {config.PDL_RUN_CREDIT_LIMIT}"
                    "-credit run limit."
                ),
            }

    fetched, failures = {}, {}
    rejected = {}
    attempted = {key: [] for key in billable}
    key_credits = {key: 0 for key in billable}
    seen_evidence = {key: set() for key in billable}
    provider_reported = 0
    bulk_ok = True
    unresolved = list(billable)
    max_rounds = max((len(plans.get(key) or []) for key in unresolved), default=0)
    for round_index in range(max_rounds):
        active = [
            key for key in unresolved if round_index < len(plans.get(key) or [])
        ]
        for start in range(0, len(active), config.PDL_BULK_MAX):
            chunk = active[start:start + config.PDL_BULK_MAX]
            if config.PDL_RUN_CREDIT_LIMIT > 0:
                available = max(
                    0,
                    config.PDL_RUN_CREDIT_LIMIT
                    - run_credits
                    - sum(key_credits.values()),
                )
                if available <= 0:
                    break
                # Every identity in a provider request can consume one credit.
                # Limit the request before it is sent, rather than discovering
                # an overrun from the response headers afterwards.
                chunk = chunk[:available]
            if not chunk:
                break
            chunk_attempts = [plans[key][round_index] for key in chunk]
            chunk_identities = [attempt["identity"] for attempt in chunk_attempts]
            for key, attempt in zip(chunk, chunk_attempts):
                attempted[key].append(attempt["strategy"])
            if bulk_ok and len(chunk) > 1:
                chunk_fetched, chunk_failures, reported, bulk_ok = _fetch_bulk(
                    chunk, chunk_identities,
                )
            else:
                chunk_fetched, chunk_failures, reported = _fetch_single(
                    chunk, chunk_identities,
                )
            provider_reported += reported
            for key in chunk:
                if key in chunk_failures:
                    failures[key] = _with_lookup_diagnostics(
                        chunk_failures[key], candidates[pending[key][0]],
                        attempted[key],
                    )
                    if key in unresolved:
                        unresolved.remove(key)
                    continue
                person, likelihood, spent, matched_inputs = chunk_fetched.get(
                    key, (None, 0, 0, []),
                )
                key_credits[key] += int(spent or 0)
                if person is not None:
                    candidate = candidates[pending[key][0]]
                    strategy = plans[key][round_index]["strategy"]
                    result = _build_result(
                        candidate, person, likelihood, matched_inputs,
                    )
                    evidence_fingerprint = _provider_evidence_fingerprint(result)
                    if evidence_fingerprint in seen_evidence[key]:
                        continue
                    seen_evidence[key].add(evidence_fingerprint)
                    rejected[key] = _prefer_retry_evidence(
                        rejected.get(key), result, strategy,
                    )
                    accepted, validation_error = _local_result_accepted(
                        candidate, result,
                    )
                    if validation_error:
                        failures[key] = _with_lookup_diagnostics({
                            "status": "error", "provider": _PROVIDER,
                            "error": (
                                "People Data Labs local validation failed "
                                f"({validation_error})."
                            ),
                        }, candidate, attempted[key])
                        if key in unresolved:
                            unresolved.remove(key)
                    elif accepted:
                        fetched[key] = (
                            result, key_credits[key], strategy,
                            list(attempted[key]),
                        )
                        if key in unresolved:
                            unresolved.remove(key)

    fresh = {}
    for key in billable:
        if key in failures:
            for cid in pending[key]:
                results[cid] = failures[key]
            continue
        accepted = fetched.get(key)
        rejected_result = rejected.get(key)
        if accepted:
            result, spent, strategy, strategies = accepted
        elif rejected_result:
            result, strategy = rejected_result
            spent = key_credits.get(key, 0)
            strategies = attempted.get(key, [])
        else:
            result = _no_match_result(
                "PDL completed the staged lookup but returned no record meeting "
                "the usable-contact policy.", candidates[pending[key][0]],
            )
            spent = key_credits.get(key, 0)
            strategy = ""
            strategies = attempted.get(key, [])
        for index, cid in enumerate(pending[key]):
            result = _with_lookup_diagnostics(
                result,
                candidates[cid], strategies, selected=strategy,
            )
            # A repeated identity was charged once and owns one cache row, so
            # only the first card carries the credit and writes that row.
            fresh[cid] = (result, spent if index == 0 else 0, key if index == 0 else "")

    spent_now = sum(credits for _, credits, _ in fresh.values())
    total_spent = run_credits + spent_now

    # One do-not-contact query covers every contact the run produced.
    contact_values = []
    for result, _, _ in fresh.values():
        contact_values.extend(result.get("emails", []))
        contact_values.extend(result.get("phones", []))
    for _, cached_result in replays:
        contact_values.extend(cached_result.get("emails", []))
        contact_values.extend(cached_result.get("phones", []))
    try:
        blocked = store.dnc_blocked(contact_values)
    except Exception as exc:
        message = f"Do-not-contact check failed ({type(exc).__name__})."
        for cid, (_, credits, _) in fresh.items():
            results[cid] = {
                "status": "error", "provider": _PROVIDER,
                "credits_spent": credits, "run_credits_spent": total_spent,
                "error": message,
            }
        for cid, _ in replays:
            results[cid] = {
                "status": "error", "provider": _PROVIDER, "credits_spent": 0,
                "run_credits_spent": total_spent, "error": message,
            }
        return {
            "results": results, "credits_spent": spent_now,
            "run_credits_spent": total_spent,
            "provider_reported_credits": provider_reported,
            "bulk": bulk_ok,
        }

    entries = []
    saved_success = {}

    def merge(cid, result, credits, *, cached, request_key):
        candidate = candidates[cid]
        try:
            saved = enrich.save_provider_result(
                cid, result, candidate=candidate, persist=False, dnc_blocked=blocked,
            )
        except Exception as exc:
            results[cid] = {
                "status": "error", "provider": _PROVIDER, "credits_spent": credits,
                "emails": [], "phones": [], "addresses": [],
                "error": f"PDL candidate merge failed ({type(exc).__name__}).",
            }
            return
        if saved.get("enrich_status") != "success":
            rejected = {
                "status": "no_match", "provider": _PROVIDER, "cached": cached,
                "credits_spent": credits, "confidence": saved.get("confidence", 0),
                "emails": [], "phones": [], "addresses": [],
                "reason_code": "local_validation_rejected",
                "lookup_strategy": result.get("lookup_strategy", ""),
                "lookup_attempt_count": result.get("lookup_attempt_count", 0),
                "lookup_attempted_strategies": result.get(
                    "lookup_attempted_strategies", []
                ),
                "lookup_evidence": result.get("lookup_evidence")
                    or _lookup_evidence(candidate),
                "verification": saved.get("verification", {}),
                "message": saved.get("error") or "PDL identity evidence was rejected.",
            }
            results[cid] = rejected
            if request_key:
                entries.append({
                    "candidate_id": cid, "request_key": request_key,
                    "status": "no_match", "result": {**rejected, "source": _PROVIDER},
                    "credits_spent": credits,
                    "candidate_updates": saved.get("candidate_updates") or {},
                })
            return
        entries.append({
            "candidate_id": cid,
            "request_key": request_key,
            "status": "success",
            "result": result,
            "credits_spent": credits,
            "candidate_updates": saved.get("candidate_updates") or {},
        })
        saved_success[cid] = (saved, result, cached, credits)

    for cid, (result, credits, request_key) in fresh.items():
        if result["status"] != "success":
            results[cid] = {
                **result, "provider": _PROVIDER, "cached": False,
                "credits_spent": credits,
            }
            if request_key:
                entries.append({
                    "candidate_id": cid, "request_key": request_key,
                    "status": result["status"], "result": result,
                    "credits_spent": credits,
                })
            continue
        merge(cid, result, credits, cached=False, request_key=request_key)
    for cid, cached_result in replays:
        # Replays must not rewrite the cache row; that would erase the credit
        # already recorded against the run that originally bought this identity.
        merge(cid, cached_result, 0, cached=True, request_key="")

    try:
        refreshed = store.finalize_provider_lookups(_PROVIDER, run_id, entries)
    except Exception as exc:
        for cid, (_, _, _, credits) in saved_success.items():
            results[cid] = {
                "status": "error", "provider": _PROVIDER, "credits_spent": credits,
                "run_credits_spent": total_spent,
                "emails": [], "phones": [], "addresses": [],
                "error": f"PDL result storage failed ({type(exc).__name__}).",
            }
        saved_success, refreshed = {}, {}

    for cid, (saved, result, cached, credits) in saved_success.items():
        stored_row = refreshed.get(cid) or {}
        stored = {
            "emails": stored_row.get("emails", saved.get("stored_emails", [])),
            "phones": stored_row.get("phones", saved.get("stored_phones", [])),
            "addresses": stored_row.get("addresses", saved.get("stored_addresses", [])),
        }
        results[cid] = _stored_response(candidates[cid], {
            **result,
            "status": "success",
            "matched_name": result.get("matched_name", ""),
            "confidence": saved.get("confidence", result.get("confidence", 0)),
            "verification": saved.get("verification", {}),
            "emails": saved.get("emails", []),
            "phones": saved.get("phones", []),
            "addresses": saved.get("addresses", []),
            "cached": cached, "credits_spent": credits,
            "run_credits_spent": total_spent,
        }, stored=stored)

    for result in results.values():
        result.setdefault("run_credits_spent", total_spent)

    return {
        "results": results,
        "credits_spent": spent_now,
        "run_credits_spent": total_spent,
        "provider_reported_credits": provider_reported,
        "bulk": bulk_ok,
    }
