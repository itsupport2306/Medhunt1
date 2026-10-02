"""Read-only Nexus ownership guard for enrichment and recruiter messaging."""
from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import replace
from typing import Any, Mapping

from . import config, store, verification
from .nexus_sync import NexusClient, NexusDeliveryError, NexusSettings


class NexusEligibilityUnavailable(RuntimeError):
    pass


_CLIENT: NexusClient | None = None
_CLIENT_LOCK = threading.Lock()
_NPI_RE = re.compile(r"\bNPI\s*[:#-]?\s*(\d{10})\b", re.IGNORECASE)


def enabled() -> bool:
    return bool(config.NEXUS_PRECHECK_ENABLED)


def _client() -> NexusClient:
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            settings = replace(NexusSettings.from_config(), enabled=True)
            _CLIENT = NexusClient(settings)
        return _CLIENT


def _values(values: Any) -> list[str]:
    output: list[str] = []
    for value in values if isinstance(values, list) else []:
        text = str(value.get("value") if isinstance(value, Mapping) else value or "").strip()
        if text and text not in output:
            output.append(text)
    return output


def _phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else ""


def candidate_identifiers(candidate: Mapping[str, Any]) -> dict[str, list[str]]:
    emails = list(dict.fromkeys(v.casefold() for v in _values(candidate.get("emails")) if "@" in v))
    phones = list(dict.fromkeys(filter(None, (_phone(v) for v in _values(candidate.get("phones"))))))
    npis: list[str] = []
    source = str(candidate.get("source") or "").casefold()
    source_id = str(candidate.get("source_id") or "").strip()
    if source in {"npino", "npiprofile"} and re.fullmatch(r"\d{10}", source_id):
        npis.append(source_id)
    for match in _NPI_RE.findall(str(candidate.get("notes") or "")):
        if match not in npis:
            npis.append(match)
    # Provider results can contain many historical contacts. Bound the remote
    # read load while still checking both current channels plus an NPI.
    return {"emails": emails[:2], "phones": phones[:2], "npis": npis[:1]}


def _identity_key(
    identifiers: Mapping[str, list[str]], candidate: Mapping[str, Any] | None = None,
) -> str:
    stable = "|".join(
        f"{kind}:{value}" for kind in ("emails", "phones", "npis")
        for value in sorted(identifiers.get(kind) or [])
    )
    candidate = candidate or {}
    stable += "|name:" + " ".join(re.findall(
        r"[a-z0-9]+", str(candidate.get("name") or candidate.get("canonical_name") or "").casefold(),
    ))
    stable += "|location:" + " ".join(re.findall(
        r"[a-z0-9]+", str(candidate.get("location") or "").casefold(),
    ))
    return hashlib.sha256(stable.encode()).hexdigest() if stable else "none"


def _truthy(value: Any) -> bool:
    return value is True or str(value or "").strip().casefold() in {"1", "true", "yes"}


def _record_summary(record: Mapping[str, Any], matched_by: list[str]) -> dict[str, Any]:
    recruiter = str(record.get("recruiter") or record.get("staffingSpecialist") or "").strip()
    return {
        "candidate_id": str(record.get("candidateId") or record.get("id") or ""),
        "status": str(record.get("status") or "").strip(),
        "status_code": str(record.get("statusCode") or "").strip(),
        "recruiter": recruiter,
        "matched_by": matched_by,
    }


def _identity_match(candidate: Mapping[str, Any], record: Mapping[str, Any]) -> bool | None:
    """Compare Nexus contact hits against all four requested identity fields.

    ``None`` means Nexus omitted data needed to verify the hit. A contact match
    by itself is not enough to tell a recruiter that this is their candidate.
    """
    expected_name = str(candidate.get("name") or candidate.get("canonical_name") or "")
    returned_name = str(
        record.get("name") or record.get("fullName") or record.get("candidateName") or ""
    ).strip()
    if not returned_name:
        returned_name = " ".join(filter(None, (
            str(record.get("firstName") or record.get("first_name") or "").strip(),
            str(record.get("lastName") or record.get("last_name") or "").strip(),
        )))
    expected_location = str(candidate.get("location") or "")
    returned_city = str(record.get("city") or record.get("cityName") or "").strip()
    returned_state = str(
        record.get("state") or record.get("stateName") or record.get("stateCode") or ""
    ).strip()
    returned_location = str(record.get("location") or "").strip()
    if returned_city and returned_state:
        returned_location = f"{returned_city}, {returned_state}"
    if not (expected_name and returned_name and verification.us_city_state(expected_location)
            and verification.us_city_state(returned_location)):
        return None
    name_match = verification.name_evidence(expected_name, returned_name)
    location_match = verification.location_evidence(expected_location, [returned_location])
    return bool(name_match.get("exact") and location_match.get("exact"))


def check_candidate(candidate: Mapping[str, Any], *, fresh: bool = False) -> dict[str, Any]:
    if not enabled():
        return {"state": "disabled", "blocked": False, "checked": False}
    identifiers = candidate_identifiers(candidate)
    identity_key = _identity_key(identifiers, candidate)
    candidate_id = int(candidate.get("id") or 0)
    cached = store.get_nexus_candidate_check(candidate_id) if candidate_id else None
    if (
        not fresh and cached and cached.get("identity_key") == identity_key
        and float(cached.get("checked") or 0) + config.NEXUS_PRECHECK_CACHE_SECONDS > time.time()
    ):
        return dict(cached.get("result") or {})
    if not any(identifiers.values()):
        # Current Nexus search accepts email, phone, or NPI only. Do not let a
        # contactless profile proceed to enrichment when the ownership guard
        # cannot perform a pre-enrichment lookup by the candidate's name and
        # city/state.
        result = {
            "state": "identity_search_unavailable", "blocked": True,
            "checked": False,
        }
        if candidate_id:
            store.save_nexus_candidate_check(candidate_id, identity_key, result)
        return result

    client = _client()
    matches: dict[str, dict[str, Any]] = {}
    matched_by: dict[str, list[str]] = {}
    try:
        for email in identifiers["emails"]:
            rows = client.search_candidates(email=email)
            for row in rows:
                key = str(row.get("candidateId") or row.get("id") or f"email:{email}")
                matches[key] = row
                matched_by.setdefault(key, []).append("email")
        for phone in identifiers["phones"]:
            rows = client.search_candidates(phone=phone)
            for row in rows:
                key = str(row.get("candidateId") or row.get("id") or f"phone:{phone}")
                matches[key] = row
                matched_by.setdefault(key, []).append("phone")
        for npi in identifiers["npis"]:
            rows = client.search_candidates(npi=npi)
            for row in rows:
                key = str(row.get("candidateId") or row.get("id") or f"npi:{npi}")
                matches[key] = row
                matched_by.setdefault(key, []).append("npi")
        statuses = client.get_master("candidatestatuses") if matches else []
    except NexusDeliveryError as exc:
        if candidate_id:
            store.save_nexus_candidate_check(candidate_id, identity_key, {
                "state": "unavailable", "blocked": True, "checked": False,
            })
        raise NexusEligibilityUnavailable("Nexus candidate verification is temporarily unavailable.") from exc

    active_ids = {
        str(row.get("id")) for row in statuses
        if row.get("id") is not None and _truthy(row.get("active"))
    }
    active_codes = {
        str(row.get("code") or "").strip().casefold() for row in statuses
        if _truthy(row.get("active"))
    }
    inactive_ids = {
        str(row.get("id")) for row in statuses
        if row.get("id") is not None and not _truthy(row.get("active"))
    }
    inactive_codes = {
        str(row.get("code") or "").strip().casefold() for row in statuses
        if not _truthy(row.get("active"))
    }
    blocked_records = []
    unverified_records = []
    for key, row in matches.items():
        status_id = str(row.get("statusId") or "")
        status_code = str(row.get("statusCode") or "").strip().casefold()
        owned = bool(row.get("recruiter") or row.get("recruiterEmail") or row.get("staffingSpecialist"))
        is_active = status_id in active_ids or (status_code and status_code in active_codes)
        explicitly_inactive = status_id in inactive_ids or (
            status_code and status_code in inactive_codes
        )
        if is_active or (owned and not explicitly_inactive):
            summary = _record_summary(row, sorted(set(matched_by.get(key) or [])))
            identity_match = _identity_match(candidate, row)
            if identity_match is True:
                blocked_records.append(summary)
            elif identity_match is None:
                unverified_records.append(summary)
    if blocked_records:
        primary = blocked_records[0]
        result = {
            "state": "active_in_nexus", "blocked": True, "checked": True,
            "status": primary["status"], "status_code": primary["status_code"],
            "recruiter": primary["recruiter"], "matched_by": primary["matched_by"],
            "match_count": len(blocked_records),
        }
    elif unverified_records:
        result = {
            "state": "identity_unverified", "blocked": True, "checked": True,
            "match_count": len(unverified_records),
        }
    else:
        result = {"state": "clear", "blocked": False, "checked": True}
    if candidate_id:
        store.save_nexus_candidate_check(candidate_id, identity_key, result)
    return result


def stored_result(candidate: Mapping[str, Any]) -> dict[str, Any]:
    candidate_id = int(candidate.get("id") or 0)
    row = store.get_nexus_candidate_check(candidate_id) if candidate_id else None
    if not row or row.get("identity_key") != _identity_key(candidate_identifiers(candidate), candidate):
        return {}
    return dict(row.get("result") or {})


__all__ = ["NexusEligibilityUnavailable", "check_candidate", "enabled", "stored_result"]
