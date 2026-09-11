"""Immutable, contact-free lookup coverage accounting.

Coverage is measured from the original selection, not from whichever provider
responses happened to arrive.  Reviews, provider failures, and missing results
stay in the denominator; only the same public trust projection used by the
extension counts as found.
"""
from __future__ import annotations

import hashlib
from collections import Counter

from . import person_name, verification


METRIC_VERSION = "trusted-contact-coverage-v1"
SUPPORTED_SOURCES = {"indeed", "linkedin", "facebook", "ziprecruiter"}
TARGET_FOUND_RATE = 0.75


def _identity_material(candidate: dict | None, candidate_id: int) -> str:
    candidate = candidate or {}
    source = str(candidate.get("source") or "").strip().casefold()
    source_id = str(candidate.get("source_id") or "").strip().casefold()
    source_url = str(candidate.get("source_url") or "").strip().casefold().rstrip("/")
    if source and source_id:
        return f"source-id|{source}|{source_id}"
    if source_url:
        return f"source-url|{source}|{source_url}"
    return f"candidate-id|{int(candidate_id)}"


def source_identity(candidate: dict | None, candidate_id: int) -> str:
    return hashlib.sha256(
        _identity_material(candidate, candidate_id).encode("utf-8")
    ).hexdigest()


def eligibility(candidate: dict | None) -> tuple[bool, str]:
    if not candidate:
        return False, "candidate_not_found"
    source = str(candidate.get("source") or "").strip().casefold()
    if source and source not in SUPPORTED_SOURCES:
        return False, "unsupported_source"
    name = person_name.normalize_person_name(candidate.get("name") or "")
    if len(person_name.identity_tokens(name)) < 2:
        return False, "full_name_required"
    has_profile = bool(str(candidate.get("source_url") or "").strip())
    has_location = bool(verification.us_city_state(candidate.get("location") or ""))
    if not (has_profile or has_location):
        return False, "profile_or_us_location_required"
    return True, ""


def freeze_cohort(candidate_ids, get_candidate) -> dict:
    raw_ids = [int(value) for value in candidate_ids or []]
    cohort, seen = [], set()
    for candidate_id in raw_ids:
        candidate = get_candidate(candidate_id)
        identity = source_identity(candidate, candidate_id)
        if identity in seen:
            continue
        seen.add(identity)
        eligible, reason = eligibility(candidate)
        cohort.append({
            "source_identity": identity,
            "candidate_id": candidate_id,
            "eligible": eligible,
            "ineligibility_reason": reason,
        })
    return {
        "metric_version": METRIC_VERSION,
        "selected_count": len(raw_ids),
        "unique_count": len(cohort),
        "eligible_count": sum(bool(item["eligible"]) for item in cohort),
        "items": cohort,
    }


def _contact_verification(result: dict) -> dict:
    verification_record = result.get("verification") or {}
    evidence = verification_record.get("evidence") or {}
    return dict(result.get("contact_verification") or evidence.get("contact_verification") or {})


def classify(item: dict, result: dict | None, public_result: dict | None) -> dict:
    """Classify one frozen cohort member without storing contact values."""
    result = dict(result or {})
    public_result = dict(public_result or {})
    if not item.get("eligible"):
        outcome = "input_ineligible"
    elif not result:
        outcome = "backend_missing_result"
    elif public_result.get("status") == "found":
        contact = _contact_verification(result)
        cached = bool(
            result.get("cached") or result.get("trusted_cache")
            or result.get("cache_migrated") or contact.get("fallback_cached")
        )
        outcome = "trusted_found_cache" if cached else "trusted_found"
    else:
        cached = False
        contact = _contact_verification(result)
        internal_status = str(
            result.get("status") or result.get("enrich_status") or ""
        ).strip().casefold()
        identity_status = str(
            (result.get("verification") or {}).get("identity_status")
            or result.get("identity_status") or ""
        ).strip().casefold()
        contact_status = str(contact.get("status") or "").strip().casefold()
        error = " ".join(filter(None, [
            str(result.get("error") or ""), str(contact.get("enformion_error") or ""),
        ])).casefold()
        contact_bearing = bool(result.get("emails") or result.get("phones"))
        if "429" in error or "rate limit" in error:
            outcome = "provider_rate_limited"
        elif "timeout" in error or "timed out" in error:
            outcome = "provider_timeout"
        elif contact_status == "fallback_budget_exhausted" or internal_status == "budget_exhausted":
            outcome = "fallback_budget_skipped"
        elif contact_status == "fallback_not_authorized":
            outcome = "fallback_not_authorized"
        elif identity_status == "rejected" and contact_bearing:
            outcome = "contact_bearing_identity_rejected"
        elif contact_bearing:
            outcome = "contact_bearing_review"
        elif internal_status == "error" or error:
            outcome = "provider_error"
        elif identity_status in {"verified", "recruiter_confirmed"}:
            outcome = "identity_found_no_usable_contact"
        else:
            outcome = "no_contact_bearing_match"
    contact = _contact_verification(result)
    verification_record = result.get("verification") or {}
    evidence = verification_record.get("evidence") or {}
    quality = evidence.get("pdl_quality_gate") or {}
    return {
        **item,
        "outcome": outcome,
        "found": outcome in {"trusted_found", "trusted_found_cache"},
        "cached": outcome == "trusted_found_cache",
        "trace": {
            "internal_status": str(result.get("status") or result.get("enrich_status") or "")[:80],
            "reason_code": str(result.get("reason_code") or "")[:120],
            "identity_status": str(verification_record.get("identity_status") or result.get("identity_status") or "")[:80],
            "quality_rule": str(quality.get("rule") or "")[:120],
            "contact_status": str(contact.get("status") or "")[:120],
            "pdl_attempt_count": max(0, int(result.get("lookup_attempt_count") or 0)),
            "fallback_called": bool(contact.get("fallback_called")),
            "fallback_cached": bool(contact.get("fallback_cached")),
            "fallback_fresh_calls": max(0, int(contact.get("fallback_fresh_calls") or 0)),
            "provider_rate_limited": bool(contact.get("enformion_error_code") == "429"),
            "raw_contact_bearing": bool(result.get("emails") or result.get("phones")),
        },
    }


def summarize(items: list[dict]) -> dict:
    eligible = [item for item in items if item.get("eligible")]
    found = sum(bool(item.get("found")) for item in eligible)
    outcomes = Counter(str(item.get("outcome") or "unknown") for item in items)
    rate = (found / len(eligible)) if eligible else 0.0
    return {
        "metric_version": METRIC_VERSION,
        "target_found_rate": TARGET_FOUND_RATE,
        "unique_count": len(items),
        "eligible_count": len(eligible),
        "found_count": found,
        "found_rate": round(rate, 4),
        "target_met": bool(eligible and rate >= TARGET_FOUND_RATE),
        "outcomes": dict(sorted(outcomes.items())),
    }
