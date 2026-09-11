"""Cost-aware contact-provider waterfall.

PDL remains the primary bulk provider. Enformion is called only when a PDL
result is absent, below the automatic quality gate, or missing one contact
type. Stable person/no-match evidence is cached; transport, rate-limit, and
provider errors are never replayed. Provider data is labelled as
high-confidence/corroborated rather than candidate-confirmed.
"""
from __future__ import annotations

import hashlib
import inspect
import re
import threading
import time

from . import (
    config, enformion_client as ef, store, verification, phone_policy, trust_policy,
    person_name, source_context,
)

_PROVIDER = "enformion"
_REQUEST_POLICY_VERSION = "enformion-person-search-v11-source-alias-relative"
_RELATIVE_REQUEST_POLICY_VERSION = "enformion-relative-tahoe-v1-owner-validated"
_RUN_BUDGET_LOCKS: dict[str, threading.Lock] = {}
_RUN_CALL_COUNTS: dict[str, int] = {}
_RUN_BUDGET_SEEN: dict[str, float] = {}
_RUN_BUDGET_LOCKS_GUARD = threading.Lock()
_RUN_BUDGET_STATE_TTL = 60 * 60
_RUN_BUDGET_STATE_MAX = 1000


def _prune_run_budget_state(now: float) -> None:
    stale = [
        key for key, seen in _RUN_BUDGET_SEEN.items()
        if now - seen > _RUN_BUDGET_STATE_TTL
    ]
    excess = max(0, len(_RUN_BUDGET_SEEN) - _RUN_BUDGET_STATE_MAX)
    if excess:
        stale.extend(
            key for key, _seen in sorted(_RUN_BUDGET_SEEN.items(), key=lambda item: item[1])[:excess]
        )
    for key in set(stale):
        lock = _RUN_BUDGET_LOCKS.get(key)
        if lock is not None and lock.locked():
            continue
        _RUN_BUDGET_LOCKS.pop(key, None)
        _RUN_CALL_COUNTS.pop(key, None)
        _RUN_BUDGET_SEEN.pop(key, None)


def _run_budget_lock(run_id: str) -> threading.Lock:
    """Serialize one run's paid budget check/calls in this backend process."""
    normalized = str(run_id or "").strip()
    with _RUN_BUDGET_LOCKS_GUARD:
        now = time.time()
        _prune_run_budget_state(now)
        _RUN_BUDGET_SEEN[normalized] = now
        return _RUN_BUDGET_LOCKS.setdefault(normalized, threading.Lock())


def enabled() -> bool:
    return bool(
        config.ENFORMION_VERIFY_PDL
        and config.ENFORMION_FALLBACK_ONLY
        and ef.enabled()
    )


def _specific_location(value: str) -> bool:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    # Enformion's people data is US-focused.  Requiring a parsed US state
    # prevents an international Facebook fact such as "Paris, France" from
    # consuming a paid fallback call that cannot be authoritative. A bare ZIP
    # is also accepted, but a five-digit foreign postal code after a comma is
    # not treated as proof of a US location.
    if verification.location_evidence(text, [text]).get("exact"):
        return True
    return bool("," not in text and re.search(r"\b\d{5}(?:-\d{4})?\b", text))


def _email_key(value: str) -> str:
    return str(value or "").strip().casefold()


def _phone_key(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else digits


def _unique(values, existing=None, *, key_fn=None) -> list[str]:
    output, seen = [], set()
    for value in [*(existing or []), *(values or [])]:
        cleaned = str(value or "").strip()
        key = key_fn(cleaned) if key_fn else cleaned.casefold()
        if cleaned and key and key not in seen:
            seen.add(key)
            output.append(cleaned)
    return output[:20]


def _quality_gate(result: dict) -> dict:
    return dict(
        (result.get("verification") or {}).get("evidence", {}).get("pdl_quality_gate")
        or {}
    )


def _pdl_accepted(result: dict) -> bool:
    verification_record = result.get("verification") or {}
    verification_evidence = verification_record.get("evidence") or {}
    contact_evidence = verification_evidence.get("contact_verification") or {}
    emails = [str(value).strip() for value in result.get("emails") or [] if str(value).strip()]
    phones = phone_policy.preferred_phone_values(result)
    return bool(
        result.get("status") == "success"
        and verification_record.get("identity_status") in ("verified", "recruiter_confirmed")
        and (_quality_gate(result).get("accepted") or result.get("trusted_cache"))
        and (emails or phones)
    )


def _complete(result: dict) -> bool:
    # A valid alternate phone is displayable, but still trigger the licensed
    # fallback once so a connected mobile/wireless number can replace it.
    return bool(result.get("emails") and phone_policy.accepted_mobile_phone_values(result))


def _recruiter_rejected(record: dict) -> bool:
    """Return True only for an explicit human identity rejection.

    Provider-side identity conflicts are evidence about one provider result,
    not a human decision to reject the captured candidate.  Keep the test
    deliberately narrow so a new/unknown provider label cannot accidentally
    suppress the independent fallback.
    """
    verification_record = record.get("verification") or {}
    identity_status = str(
        verification_record.get("identity_status")
        or record.get("identity_status")
        or ""
    ).strip().casefold()
    identity_provider = str(
        verification_record.get("identity_provider")
        or record.get("identity_provider")
        or ""
    ).strip().casefold()
    return identity_status == "rejected" and identity_provider == "recruiter_review"


def _fallback_reason(result: dict) -> str:
    """Return why Enformion should run, or an empty string to avoid the cost."""
    status = str(result.get("status") or "").strip().casefold()
    if _pdl_accepted(result):
        return "" if _complete(result) else "pdl_missing_contact_type"
    if status in ("budget_exhausted", "review_approved", "skipped"):
        return ""
    if status == "rejected" and _recruiter_rejected(result):
        return ""
    identity_status = str((result.get("verification") or {}).get("identity_status") or "")
    if identity_status == "rejected":
        # This describes the PDL record, not a recruiter rejection of the
        # source candidate. A conflicting provider must not prevent an
        # independent licensed provider from finding the correct person.
        return "pdl_identity_conflict"
    if status == "rejected":
        # A provider rejected its own proposed identity.  That does not reject
        # the source candidate and must not suppress another provider lookup.
        return "pdl_identity_conflict"
    # A review caused by weak/same-state PDL evidence is a useful fallback
    # condition, not a reason to stop. Enformion is queried independently from
    # the captured full name + city/state and must still pass exact-match rules.
    if identity_status == "review":
        return "pdl_identity_review"
    if _quality_gate(result) and not _quality_gate(result).get("accepted"):
        return "pdl_below_quality_gate"
    if status in ("no_match", "error", "disabled", ""):
        return "pdl_no_accepted_contact"
    return ""


def _request(candidate: dict, reason: str) -> dict | None:
    raw_location = str(candidate.get("location") or "").strip()
    location = verification.us_city_state(raw_location) or raw_location
    name = person_name.normalize_person_name(candidate.get("name"))
    if len(name.split()) < 2 or not _specific_location(location):
        return None
    source_facts = source_context.extract(candidate)
    companies = source_facts["companies"]
    schools = source_facts["schools"]
    roles = source_facts["roles"]
    aliases = source_facts["aliases"]
    # Only explicit source-provided relatives enter this list. Provider-returned
    # household relatives remain discovery evidence handled by the Tahoe pivot.
    relatives = source_facts["relatives"]
    # Never seed a fallback lookup with PDL's uncertain phone/email: doing so
    # would make the second result circular rather than independent.
    material = "|".join([
        _REQUEST_POLICY_VERSION, name.casefold(), location.casefold(),
        ",".join(value.casefold() for value in aliases),
        ",".join(value.casefold() for value in companies),
        ",".join(value.casefold() for value in schools),
        ",".join(value.casefold() for value in roles),
        ",".join(value.casefold() for value in relatives),
    ])
    return {
        "name": name,
        "location": location,
        "phone": "",
        "email": "",
        "reason": reason,
        "independent": True,
        "seed_type": _REQUEST_POLICY_VERSION,
        "aliases": aliases,
        "companies": companies,
        "schools": schools,
        "roles": roles,
        "relatives": relatives,
        "request_key": hashlib.sha256(material.encode("utf-8")).hexdigest(),
    }


def _call(request: dict) -> dict:
    context = {
        "aliases": request.get("aliases") or [],
        "companies": request.get("companies") or [],
        "schools": request.get("schools") or [],
        "roles": request.get("roles") or [],
        "relatives": request.get("relatives") or [],
    }
    parameters = inspect.signature(ef.enrich).parameters.values()
    accepts_context = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        or parameter.name == "aliases"
        for parameter in parameters
    )
    return ef.enrich(
        request["name"], request["location"],
        **(context if accepts_context else {}),
    )


def _relative_request(request: dict, result: dict) -> dict | None:
    """Create one cacheable Tahoe-ID follow-up without household contacts.

    A Person Search response may expose the requested candidate only as a
    relative of the returned household owner.  That response is discovery
    evidence, never contact evidence.  Only the candidate's stable Tahoe ID is
    copied into the second request; phone/email/address values from the
    household result are deliberately ignored.
    """
    if str(result.get("status") or "") != "relative_pivot":
        return None
    selection = result.get("selection") or {}
    if selection.get("selection") != "unique_relative_pivot":
        return None
    tahoe_id = str(result.get("relative_tahoe_id") or "").strip()
    if not tahoe_id or tahoe_id != str(
        selection.get("relative_tahoe_id") or ""
    ).strip():
        return None
    material = "|".join((
        _RELATIVE_REQUEST_POLICY_VERSION,
        str(request.get("request_key") or ""),
        tahoe_id,
    ))
    return {
        **request,
        "seed_type": _RELATIVE_REQUEST_POLICY_VERSION,
        "relative_tahoe_id": tahoe_id,
        "initial_request_key": str(request.get("request_key") or ""),
        "request_key": hashlib.sha256(material.encode("utf-8")).hexdigest(),
        # Retain only non-contact audit context from the household discovery.
        "relative_pivot_selection": dict(selection),
    }


def _call_relative(request: dict) -> dict:
    """Resolve a relative's own Tahoe record with the original source facts."""
    context = {
        "aliases": request.get("aliases") or [],
        "companies": request.get("companies") or [],
        "schools": request.get("schools") or [],
        "roles": request.get("roles") or [],
        "relatives": request.get("relatives") or [],
    }
    resolver = ef.resolve_tahoe_id
    parameters = inspect.signature(resolver).parameters.values()
    accepts_context = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        or parameter.name == "aliases"
        for parameter in parameters
    )
    return resolver(
        request["relative_tahoe_id"], request["name"], request["location"],
        **(context if accepts_context else {}),
    )


def _safe_relative_result(request: dict, result: dict) -> dict:
    """Fail closed unless the follow-up returned the requested owner's record."""
    resolved = dict(result or {})
    target = str(request.get("relative_tahoe_id") or "").strip()
    returned_target = str(resolved.get("bridge_target_tahoe_id") or "").strip()
    owner_bound = bool(
        resolved.get("status") == "success"
        and resolved.get("relative_bridge_used") is True
        and resolved.get("household_contact_used") is False
        and target and returned_target == target
    )
    audit = {
        "attempted": True,
        "owner_bound": owner_bound,
        "target_tahoe_id": target,
        "household_contact_used": False,
        "discovery": dict(request.get("relative_pivot_selection") or {}),
    }
    if owner_bound:
        return {
            **resolved,
            "relative_resolution": audit,
            "household_contact_used": False,
        }
    # A malformed, ambiguous, or wrong-ID response must not leak even a
    # syntactically valid household contact into downstream verification.
    return {
        **resolved,
        "status": "no_match",
        "phones": [], "emails": [], "addresses": [],
        "phone_evidence": [], "email_evidence": [], "address_evidence": [],
        "provider_location": "",
        "relative_bridge_used": False,
        "household_contact_used": False,
        "relative_resolution": audit,
        "message": (
            str(resolved.get("message") or "").strip()
            or "The candidate's own relative record could not be verified."
        ),
    }


def _unresolved_relative_result(
    request: dict, initial_result: dict, status: str, message: str,
) -> dict:
    """Represent an unexecuted Tahoe follow-up without retaining contacts."""
    return {
        "status": "no_match", "source": "enformion",
        "matched_name": "", "phones": [], "emails": [], "addresses": [],
        "phone_evidence": [], "email_evidence": [], "address_evidence": [],
        "confidence": 0, "household_contact_used": False,
        "relative_bridge_used": False,
        "relative_resolution": {
            "attempted": False, "owner_bound": False,
            "target_tahoe_id": request.get("relative_tahoe_id") or "",
            "status": status,
            "discovery": dict(
                request.get("relative_pivot_selection")
                or initial_result.get("selection") or {}
            ),
        },
        "selection": {
            "selection": "relative_pivot_unresolved",
            "relative_tahoe_id": request.get("relative_tahoe_id") or "",
        },
        "message": message,
    }


def _cached(
    request_key: str, *, candidate: dict | None = None,
) -> dict | None:
    """Return exact-key cache or a same-candidate result that still verifies.

    A request-policy bump must invalidate old negative/ambiguous observations,
    but it should not force a paid re-purchase of a recent contact record that
    still passes today's deterministic name/location/owner checks.  Migrated
    evidence is limited to the same local candidate ID and is re-assessed by
    the current Enformion gate before use.
    """
    entry = store.get_provider_lookup(_PROVIDER, request_key)
    if entry and (
        time.time() - float(entry.get("updated") or 0)
        <= config.ENFORMION_CACHE_TTL_SECONDS
    ):
        result = entry.get("result") or {}
        if _cacheable_result(result):
            return result

    candidate_id = int((candidate or {}).get("id") or 0)
    if not candidate_id:
        return None
    for historical in store.recent_provider_lookups_for_candidate(
        _PROVIDER, candidate_id, limit=10,
    ):
        if historical.get("request_key") == request_key:
            continue
        if (
            time.time() - float(historical.get("updated") or 0)
            > config.ENFORMION_CACHE_TTL_SECONDS
        ):
            continue
        result = historical.get("result") or {}
        if str(result.get("status") or "") != "success":
            continue
        if not _cacheable_result(result):
            continue
        accepted, _assessment, emails, phones = _enformion_match(
            candidate or {}, result,
        )
        if not accepted or not (emails or phones):
            continue
        return {
            **result,
            "cache_migrated": True,
            "cache_migration_basis": "same_candidate_revalidated",
        }
    return None


def _cacheable_result(result: dict) -> bool:
    """Only persist/replay deterministic Enformion person-search outcomes."""
    if not isinstance(result, dict):
        return False
    status = str(result.get("status") or "").strip().casefold()
    if status not in {"success", "no_match", "relative_pivot"}:
        return False
    if result.get("retryable") is True:
        return False
    try:
        http_status = int(result.get("http_status") or 0)
    except (TypeError, ValueError):
        http_status = 0
    if http_status in {408, 425, 429} or http_status >= 500:
        return False
    # A deterministic no-match uses ``message`` for its explanation.  Any
    # populated error/code means the provider attempt failed, even if a wrapper
    # accidentally rewrote its status to ``no_match`` (as the safe relative
    # bridge does when it fails closed).
    if str(result.get("error") or "").strip():
        return False
    if str(result.get("provider_error_code") or "").strip():
        return False
    return True


def _provider_throttle_deferred() -> dict:
    return {
        "status": "error", "source": "enformion",
        "emails": [], "phones": [], "addresses": [],
        "error": (
            "Enformion lookup was deferred because the shared provider "
            "credential is rate-limited; retry the batch later."
        ),
        "provider_error_code": "429", "http_status": 429,
        "retryable": True, "rate_limit_deferred": True,
        "credits_spent": 0,
    }


def _allowed_contacts(result: dict, assessment: dict) -> tuple[list[str], list[str]]:
    email_checks = {item["value"]: item for item in assessment.get("emails") or []}
    phone_checks = {item["value"]: item for item in assessment.get("phones") or []}
    email_evidence = {item.get("value"): item for item in result.get("email_evidence") or []}
    preferred_phone_values = set(phone_policy.preferred_phone_values(result))
    return tuple(store.filter_dnc_groups({
        "emails": [
            value for value in result.get("emails") or []
            if email_checks.get(value, {}).get("format_valid")
            and email_checks.get(value, {}).get("deliverability") not in ("invalid", "disposable")
            and email_evidence.get(value, {}).get("is_current") is not False
        ],
        "phones": [
            value for value in result.get("phones") or []
            if phone_checks.get(value, {}).get("format_valid")
            and phone_checks.get(value, {}).get("line_status") != "invalid"
            and value in preferred_phone_values
        ],
    })[key] for key in ("emails", "phones"))


def _enformion_match(candidate: dict, result: dict) -> tuple[bool, dict, list[str], list[str]]:
    assessment = verification.assess(candidate, result)
    evidence = assessment.get("evidence") or {}
    name = evidence.get("name") or {}
    location = evidence.get("location") or {}
    selection = result.get("selection") or {}
    selected_identity = selection.get("identity_evidence") or {}
    ranked_identity = selection.get("selection") == "ranked_identity_match"
    alias_match = bool(
        name.get("alias_match")
        or name.get("source_alias_match")
        or selected_identity.get("alias_match") is True
        or selection.get("matched_name_type") == "provider_alias"
    )
    try:
        winner_margin = float(selection.get("winner_margin") or 1.0)
    except (TypeError, ValueError):
        winner_margin = 0.0
    emails, phones = _allowed_contacts(result, assessment)
    relative_owner_safe = bool(
        result.get("status") != "relative_pivot"
        and result.get("household_contact_used") is not True
        and (
            not result.get("bridge_target_tahoe_id")
            or result.get("relative_bridge_used") is True
        )
    )
    accepted = bool(
        result.get("status") == "success"
        and relative_owner_safe
        and assessment.get("identity_status") == "verified"
        and (name.get("exact") or alias_match)
        and (
            location.get("exact")
            or (
                ranked_identity
                and (selected_identity.get("admissible") is True)
            )
        )
        and winner_margin >= config.IDENTITY_AMBIGUITY_MARGIN
        and not evidence.get("conflicts")
        and float(assessment.get("identity_confidence") or 0) >= config.IDENTITY_MATCH_THRESHOLD
        and (emails or phones)
    )
    return accepted, assessment, emails, phones


def _evidence(
    status: str, *, request=None, result=None, assessment=None,
    called=False, cached=False, fresh_credits_spent: int | None = None,
    fresh_calls: int | None = None,
) -> dict:
    request = request or {}
    result = result or {}
    assessment = assessment or {}
    assessment_evidence = assessment.get("evidence") or {}
    return {
        "status": status,
        "phone_policy": phone_policy.selected_phone_policy(result),
        "checked_at": time.time(),
        "confidence_tier": (
            "high" if status in ("pdl_high_confidence", "enformion_fallback", "enformion_supplemented")
            else "review"
        ),
        "automatic_use_allowed": status in (
            "pdl_high_confidence", "enformion_fallback", "enformion_supplemented"
        ),
        "candidate_confirmed": False,
        "fallback_reason": request.get("reason") or "",
        "fallback_called": bool(called),
        "fallback_fresh_calls": (
            max(0, int(fresh_calls or 0))
            if fresh_calls is not None else (1 if called else 0)
        ),
        "fallback_cached": bool(cached),
        "fallback_credits_spent": (
            max(0, int(fresh_credits_spent or 0))
            if fresh_credits_spent is not None
            else (0 if cached else max(0, int(result.get("credits_spent") or 0)))
        ),
        "independent_lookup": bool(request.get("independent")),
        "lookup_method": request.get("seed_type") or "",
        "enformion_status": result.get("status") or "not_called",
        "enformion_error": str(result.get("error") or "")[:700],
        "enformion_error_code": str(result.get("provider_error_code") or "")[:120],
        "enformion_selection": dict(result.get("selection") or {}),
        "enformion_identity_status": assessment.get("identity_status") or "",
        "enformion_identity_confidence": assessment.get("identity_confidence") or 0,
        "enformion_name_exact": bool((assessment_evidence.get("name") or {}).get("exact")),
        "enformion_name_alias_match": bool(
            (assessment_evidence.get("name") or {}).get("alias_match")
            or (assessment_evidence.get("name") or {}).get("source_alias_match")
            or ((result.get("selection") or {}).get("identity_evidence") or {}).get("alias_match") is True
            or (result.get("selection") or {}).get("matched_name_type") == "provider_alias"
        ),
        "enformion_location_exact": bool((assessment_evidence.get("location") or {}).get("exact")),
        "enformion_ranked_identity_match": bool(
            (result.get("selection") or {}).get("selection") == "ranked_identity_match"
            and ((result.get("selection") or {}).get("identity_evidence") or {}).get("admissible") is True
        ),
        "enformion_winner_margin": (
            (result.get("selection") or {}).get("winner_margin")
            if (result.get("selection") or {}).get("winner_margin") is not None
            else 1.0
        ),
        "enformion_conflicts": list(assessment_evidence.get("conflicts") or []),
        "enformion_addresses": list(result.get("addresses") or [])[:20],
        "enformion_phone_evidence": list(result.get("phone_evidence") or [])[:20],
        "enformion_email_evidence": list(result.get("email_evidence") or [])[:20],
        "enformion_relative_bridge_used": bool(result.get("relative_bridge_used")),
        "enformion_household_contact_used": bool(result.get("household_contact_used")),
        "enformion_relative_resolution": dict(result.get("relative_resolution") or {}),
        "disclaimer": (
            "High-confidence provider data is not candidate-confirmed. Current ownership or "
            "deliverability requires a candidate reply, verification link, or phone verification."
        ),
    }


def _save_verification(
    candidate: dict, verification_record: dict, contact_evidence: dict,
    *, provider_contacts: dict | None = None, **fields,
) -> dict:
    updated = dict(verification_record or candidate.get("verification") or {})
    evidence = dict(updated.get("evidence") or {})
    evidence["contact_verification"] = contact_evidence
    if provider_contacts is not None:
        evidence["phone_policy"] = phone_policy.selected_phone_policy({
            "phones": provider_contacts.get("phones") or [],
            "verification": {"evidence": evidence},
        })
    else:
        evidence.setdefault(
            "phone_policy",
            str(contact_evidence.get("phone_policy") or phone_policy.MOBILE_PHONE_POLICY),
        )
    evidence["contact_trust_policy"] = trust_policy.CONTACT_TRUST_POLICY
    evidence["source_identity_fingerprint"] = trust_policy.source_identity_fingerprint(candidate)
    updated["evidence"] = evidence
    updated["contact_verification_status"] = contact_evidence["status"]
    if provider_contacts is not None:
        updated["provider_contacts"] = provider_contacts
    store.update_candidate(int(candidate["id"]), verification=updated, **fields)
    return updated


def _mark_primary_success(candidate: dict, result: dict) -> dict:
    phones = phone_policy.preferred_phone_values(result)
    safe_result = {**result, "phones": phones}
    if result.get("trusted_cache"):
        return {**safe_result, "contacts_trusted": True}
    evidence = _evidence("pdl_high_confidence", result=result)
    verification_record = _save_verification(
        candidate, safe_result.get("verification") or {}, evidence,
        provider_contacts={
            "emails": list(safe_result.get("emails") or []),
            "phones": phones,
            "addresses": list(safe_result.get("addresses") or []),
        },
    )
    return {
        **safe_result, "contact_verification": evidence,
        "verification": verification_record, "contacts_trusted": True,
    }


def _apply_fallback(
    candidate: dict, pdl_result: dict, ef_result: dict, request: dict, *,
    cached: bool, fresh_credits_spent: int | None = None,
    fresh_calls: int | None = None,
) -> dict:
    accepted, assessment, ef_emails, ef_phones = _enformion_match(candidate, ef_result)
    pdl_usable = _pdl_accepted(pdl_result)
    pdl_phones = phone_policy.preferred_phone_values(pdl_result)
    pdl_mobile = phone_policy.accepted_mobile_phone_values(pdl_result)
    if not accepted:
        status = "pdl_high_confidence_incomplete" if pdl_usable else "fallback_no_match"
        evidence = _evidence(
            status, request=request, result=ef_result, assessment=assessment,
            called=not cached, cached=cached,
            fresh_credits_spent=fresh_credits_spent,
            fresh_calls=fresh_calls,
        )
        base = pdl_result.get("verification") or candidate.get("verification") or {}
        verification_record = _save_verification(candidate, base, evidence)
        if pdl_usable:
            return {**pdl_result, "contact_verification": evidence, "verification": verification_record}
        provider_error = str(ef_result.get("error") or "").strip()
        return {
            **pdl_result,
            "status": "no_match", "provider": "enformion_fallback",
            "emails": [], "phones": [], "addresses": [],
            "contact_verification": evidence, "verification": verification_record,
            "message": (
                f"Enformion fallback could not run: {provider_error}"
                if ef_result.get("status") == "error" and provider_error
                else "PDL produced no accepted contact and Enformion fallback did not pass exact identity checks."
            ),
        }

    # An Enformion name+city lookup is independent of PDL, but it is not bound
    # to a LinkedIn member slug.  For an explicit /in/ capture, exact name and
    # city can still identify the wrong same-name person.  Enformion may
    # supplement a PDL result that already confirmed the social profile, but it
    # must not become the sole automatic identity proof for LinkedIn.
    pdl_evidence = (pdl_result.get("verification") or {}).get("evidence") or {}
    linkedin_pdl_confirmed = bool(
        pdl_usable
        and (_quality_gate(pdl_result).get("accepted"))
        and pdl_evidence.get("social_profile_match")
    )
    if (
        trust_policy.linkedin_profile_identity(candidate)
        and not linkedin_pdl_confirmed
    ):
        evidence = _evidence(
            "enformion_identity_review", request=request, result=ef_result,
            assessment=assessment, called=not cached, cached=cached,
            fresh_credits_spent=fresh_credits_spent,
            fresh_calls=fresh_calls,
        )
        base = pdl_result.get("verification") or candidate.get("verification") or {}
        verification_record = _save_verification(
            candidate, base, evidence, emails=[], phones=[], addresses=[],
            enrich_status="review", contact_verified_at=0, contact_expires_at=0,
        )
        return {
            **pdl_result,
            "status": "review", "provider": "enformion_identity_review",
            "emails": [], "phones": [], "addresses": [],
            "contacts_trusted": False,
            "contact_verification": evidence, "verification": verification_record,
            "message": (
                "Enformion matched the name and location, but the result was not "
                "bound to the captured LinkedIn profile. Recruiter review or an "
                "independently confirmed social-profile match is required."
            ),
        }

    if pdl_usable:
        cross_provider_overlap_emails = []
        cross_provider_overlap_phones = []
        ef_mobile = phone_policy.accepted_mobile_phone_values(ef_result)
        pdl_other = bool(pdl_phones and not pdl_mobile)
        ef_other = bool(ef_phones and not ef_mobile)
        supplied_missing_type = bool(
            (not pdl_result.get("emails") and ef_emails)
            or (not pdl_mobile and ef_mobile)
            or (not pdl_phones and ef_phones)
            or (pdl_other and ef_other)
        )
        if not supplied_missing_type:
            evidence = _evidence(
                "pdl_high_confidence_incomplete", request=request, result=ef_result,
                assessment=assessment, called=not cached, cached=cached,
                fresh_credits_spent=fresh_credits_spent,
                fresh_calls=fresh_calls,
            )
            verification_record = _save_verification(
                candidate, pdl_result.get("verification") or {}, evidence,
            )
            return {
                **pdl_result, "contact_verification": evidence,
                "verification": verification_record,
            }
        if trust_policy.linkedin_profile_identity(candidate):
            pdl_email_keys = {_email_key(value) for value in pdl_result.get("emails") or []}
            ef_email_keys = {_email_key(value) for value in ef_emails}
            pdl_phone_keys = {_phone_key(value) for value in pdl_phones}
            ef_phone_keys = {_phone_key(value) for value in ef_phones}
            cross_provider_overlap_emails = sorted(pdl_email_keys & ef_email_keys)
            cross_provider_overlap_phones = sorted(pdl_phone_keys & ef_phone_keys)
            contact_overlap = bool(
                cross_provider_overlap_emails or cross_provider_overlap_phones
            )
            if not contact_overlap:
                supplement_review = _evidence(
                    "enformion_supplement_review", request=request,
                    result=ef_result, assessment=assessment,
                    called=not cached, cached=cached,
                    fresh_credits_spent=fresh_credits_spent,
                    fresh_calls=fresh_calls,
                )
                base = pdl_result.get("verification") or candidate.get("verification") or {}
                base_contact = dict(
                    (base.get("evidence") or {}).get("contact_verification")
                    or _evidence("pdl_high_confidence")
                )
                base_contact["enformion_supplement_review"] = supplement_review
                verification_record = _save_verification(candidate, base, base_contact)
                return {
                    **pdl_result,
                    "contact_verification": base_contact,
                    "enformion_supplement_review": supplement_review,
                    "verification": verification_record,
                    "contacts_trusted": True,
                    "message": (
                        "PDL confirmed the LinkedIn profile, but Enformion did not "
                        "overlap a PDL contact value. Its supplemental contact was "
                        "withheld for review."
                    ),
                }
        # The fallback may fill a missing type, but it must not add alternate
        # values for a contact type already accepted from PDL.
        emails = list(pdl_result.get("emails") or ef_emails)
        phones = list(pdl_mobile or ef_mobile or ef_phones or pdl_phones)
        addresses = _unique(ef_result.get("addresses"), pdl_result.get("addresses"))
        status = "enformion_supplemented"
        base_verification = pdl_result.get("verification") or {}
        provider = "people_data_labs+enformion"
    else:
        emails, phones = ef_emails, ef_phones
        addresses = list(ef_result.get("addresses") or [])[:20]
        status = "enformion_fallback"
        base_verification = assessment
        provider = "enformion"
    evidence = _evidence(
        status, request=request, result=ef_result, assessment=assessment,
        called=not cached, cached=cached,
        fresh_credits_spent=fresh_credits_spent,
        fresh_calls=fresh_calls,
    )
    if status == "enformion_supplemented":
        evidence["cross_provider_contact_overlap"] = True
        evidence["cross_provider_overlap_emails"] = cross_provider_overlap_emails
        evidence["cross_provider_overlap_phones"] = cross_provider_overlap_phones
    now = evidence["checked_at"]
    verification_record = _save_verification(
        candidate, base_verification, evidence,
        provider_contacts={"emails": emails, "phones": phones, "addresses": addresses},
        emails=emails, phones=phones, addresses=addresses,
        enrich_status="success", confidence=assessment.get("identity_confidence") or 0,
        canonical_name=str(ef_result.get("matched_name") or "").strip()
            or candidate.get("canonical_name") or candidate.get("name") or "",
        identity_status="verified", identity_score=assessment.get("identity_confidence") or 0,
        identity_provider=provider,
        identity_evidence=assessment.get("evidence") or {}, identity_verified_at=now,
        contact_verified_at=now, contact_expires_at=now + config.CONTACT_FRESHNESS_SECONDS,
        **({"stage": "enriched"} if candidate.get("stage") == "new" else {}),
    )
    return {
        **pdl_result,
        "status": "success", "provider": provider,
        "contacts_trusted": True,
        "emails": emails, "phones": phones, "addresses": addresses,
        "contact_verification": evidence, "verification": verification_record,
        "enformion": {
            "status": ef_result.get("status") or "error",
            "emails": list(ef_result.get("emails") or []),
            "phones": list(ef_result.get("phones") or []),
            "addresses": list(ef_result.get("addresses") or []),
        },
    }


def _skipped_fallback_result(
    candidate: dict,
    pdl_result: dict,
    request: dict,
    status: str,
    message: str,
) -> dict:
    """Describe a skipped fresh fallback without hiding valid PDL data."""
    if _pdl_accepted(pdl_result):
        return _mark_primary_success(candidate, pdl_result)
    evidence = _evidence(status, request=request)
    return {
        **pdl_result,
        "contact_verification": evidence,
        "contacts_trusted": False,
        "message": message,
    }


def verify_batch(
    candidate_ids,
    pdl_results: dict,
    run_id: str,
    *,
    allow_enformion: bool = False,
    max_enformion_calls: int | None = None,
    location_overrides: dict | None = None,
) -> dict:
    """Apply a cache-first PDL-to-Enformion fallback waterfall.

    Fresh calls require request consent and are bounded by both the request
    maximum and ``ENFORMION_RUN_CREDIT_LIMIT``. A zero server limit disables
    fresh calls; cached results are evaluated before either gate.
    """
    output = {int(key): dict(value) for key, value in (pdl_results or {}).items()}
    overrides = {
        int(key): " ".join(str(value or "").split())[:500]
        for key, value in (location_overrides or {}).items()
        if str(value or "").strip()
    }
    configured_limit = max(0, int(config.ENFORMION_RUN_CREDIT_LIMIT or 0))
    requested_limit = (
        configured_limit
        if max_enformion_calls is None
        else max(0, int(max_enformion_calls or 0))
    )
    effective_limit = min(configured_limit, requested_limit)
    workflow_enabled = bool(
        config.ENFORMION_VERIFY_PDL and config.ENFORMION_FALLBACK_ONLY
    )
    summary = {
        "enabled": enabled(), "workflow_enabled": workflow_enabled,
        "consented": bool(allow_enformion),
        "configured_limit": configured_limit,
        "requested_limit": requested_limit,
        "effective_limit": effective_limit,
        "eligible": 0, "called": 0, "matches": 0, "cached": 0,
        "skipped_budget": 0, "skipped_consent": 0,
        "skipped_unavailable": 0,
        "skipped_cost": 0, "skipped_missing_identity": 0,
        "relative_pivots": 0, "relative_pivot_called": 0,
        "relative_pivot_cached": 0, "relative_pivot_skipped": 0,
        "deferred_rate_limit": 0,
        "run_credits_spent": 0,
        "reason_counts": {},
        # Backward-compatible summary fields for older extension builds.
        "checked": 0, "verified": 0, "conflicts": 0,
    }
    if not workflow_enabled:
        return {"results": output, "summary": summary}

    pending, context = {}, {}
    for candidate_id in dict.fromkeys(int(value) for value in candidate_ids or []):
        candidate = store.get_candidate(candidate_id)
        if not candidate:
            continue
        if _recruiter_rejected(candidate):
            # A human rejection is authoritative and is not equivalent to one
            # enrichment provider returning a conflicting identity.
            continue
        override = overrides.get(candidate_id, "")
        if override:
            candidate = {
                **candidate,
                "_source_identity_location": candidate.get("location") or "",
                "location": override,
            }
        pdl_result = output.get(candidate_id, {"status": "no_match", "emails": [], "phones": []})
        reason = _fallback_reason(pdl_result)
        if not reason:
            if _pdl_accepted(pdl_result):
                output[candidate_id] = _mark_primary_success(candidate, pdl_result)
                summary["skipped_cost"] += 1
            continue
        request = _request(candidate, reason)
        if not request:
            summary["skipped_missing_identity"] += 1
            explanation = (
                "Enformion was not called because an exact US city/state or US ZIP "
                "was not available for this candidate."
            )
            evidence = _evidence(
                "fallback_ineligible", request={"reason": reason},
            )
            evidence["fallback_ineligibility"] = "exact_us_location_required"
            output[candidate_id] = {
                **pdl_result,
                "contact_verification": evidence,
                "contacts_trusted": False,
                "message": " ".join(filter(None, [
                    str(pdl_result.get("message") or "").strip(), explanation,
                ])),
            }
            continue
        summary["eligible"] += 1
        context[candidate_id] = (candidate, pdl_result, request)
        cached_result = _cached(
            request["request_key"], candidate=candidate,
        )
        if cached_result is not None:
            pending[candidate_id] = (cached_result, True, 0, 0)
            summary["cached"] += 1

    fresh_ids = [candidate_id for candidate_id in context if candidate_id not in pending]
    fresh_groups = {}
    for candidate_id in fresh_ids:
        request_key = context[candidate_id][2]["request_key"]
        fresh_groups.setdefault(request_key, []).append(candidate_id)
    fresh_keys = list(fresh_groups)
    allowed_keys = []
    skipped_keys = []
    skipped_status = ""
    skipped_message = ""
    # The local backend can receive concurrent requests. Keep a run's database
    # spend check, provider calls and persistence inside one process-level lock
    # so two requests cannot each reserve the same remaining budget.
    with _run_budget_lock(run_id):
        # Another request may have populated this identity cache while this
        # request waited for the run lock. Recheck before reserving paid calls.
        for request_key in list(fresh_keys):
            migrated = {}
            for candidate_id in fresh_groups[request_key]:
                candidate = context[candidate_id][0]
                cached_result = _cached(request_key, candidate=candidate)
                if cached_result is not None:
                    migrated[candidate_id] = cached_result
            if not migrated:
                continue
            for candidate_id, cached_result in migrated.items():
                pending[candidate_id] = (cached_result, True, 0, 0)
                summary["cached"] += 1
            # Same request-key groups normally share the exact identity. If a
            # legacy same-candidate row exists for only part of a group, leave
            # the remainder eligible for a fresh lookup under its own key.
            remaining_ids = [
                candidate_id for candidate_id in fresh_groups[request_key]
                if candidate_id not in migrated
            ]
            if remaining_ids:
                fresh_groups[request_key] = remaining_ids
            else:
                fresh_keys.remove(request_key)
        run_credits = store.provider_run_credits(_PROVIDER, run_id)
        summary["run_credits_spent"] = run_credits
        run_calls = max(run_credits, _RUN_CALL_COUNTS.get(run_id, 0))
        summary["run_calls_used"] = run_calls
        fresh_candidate_count = sum(len(fresh_groups[key]) for key in fresh_keys)
        if not fresh_keys:
            pass
        elif not allow_enformion:
            skipped_keys = fresh_keys
            summary["skipped_consent"] = fresh_candidate_count
            skipped_status = "fallback_not_authorized"
            skipped_message = (
                "Enformion fallback was not authorized for this request; no fresh "
                "Enformion call was made."
            )
        elif not enabled():
            skipped_keys = fresh_keys
            summary["skipped_unavailable"] = fresh_candidate_count
            skipped_status = "fallback_unavailable"
            skipped_message = (
                "Enformion fallback is unavailable; no fresh Enformion call was made."
            )
        else:
            remaining = max(0, effective_limit - run_calls)
            allowed_keys = fresh_keys[:remaining]
            skipped_keys = fresh_keys[remaining:]
            summary["skipped_budget"] = sum(
                len(fresh_groups[request_key]) for request_key in skipped_keys
            )
            skipped_status = "fallback_budget_exhausted"
            skipped_message = (
                "Enformion fallback was skipped because this run reached its "
                f"{effective_limit}-call paid fallback limit."
            )

        for request_key in skipped_keys:
            for candidate_id in fresh_groups[request_key]:
                candidate, pdl_result, request = context[candidate_id]
                output[candidate_id] = _skipped_fallback_result(
                    candidate, pdl_result, request, skipped_status, skipped_message,
                )

        provider_throttled = False
        for request_key in allowed_keys:
            candidate_ids_for_key = fresh_groups[request_key]
            candidate_id = candidate_ids_for_key[0]
            made_call = not provider_throttled
            if provider_throttled:
                result = _provider_throttle_deferred()
                summary["deferred_rate_limit"] += len(candidate_ids_for_key)
            else:
                try:
                    result = _call(context[candidate_id][2])
                except Exception as exc:
                    result = {
                        "status": "error", "source": "enformion", "emails": [],
                        "phones": [], "addresses": [],
                        "error": f"Enformion processing failed ({type(exc).__name__}).",
                    }
            call_credits = max(0, int(result.get("credits_spent") or 0))
            for grouped_candidate_id in candidate_ids_for_key:
                pending[grouped_candidate_id] = (
                    result, False, call_credits, 1 if made_call else 0,
                )
            request = context[candidate_id][2]
            if made_call and _cacheable_result(result):
                store.save_provider_lookup(
                    _PROVIDER, run_id, candidate_id, request["request_key"],
                    str(result.get("status") or "error"), result,
                    credits_spent=call_credits,
                )
            if made_call:
                summary["called"] += 1
                summary["run_credits_spent"] += call_credits
                _RUN_CALL_COUNTS[run_id] = _RUN_CALL_COUNTS.get(run_id, run_calls) + 1
                run_calls = _RUN_CALL_COUNTS[run_id]
                summary["run_calls_used"] = run_calls
            if (
                made_call and result.get("retryable") is True
                and int(result.get("http_status") or 0) == 429
            ):
                provider_throttled = True

        # A relative result is only a contact-free discovery bridge. Resolve
        # its stable Tahoe ID in a second, independently cached request and
        # validate the candidate's own returned record before applying it.
        pivot_groups: dict[str, list[int]] = {}
        pivot_requests: dict[str, dict] = {}
        for candidate_id, (
            initial_result, initial_cached, fresh_spend, initial_fresh_calls,
        ) in list(pending.items()):
            request = context[candidate_id][2]
            pivot_request = _relative_request(request, initial_result)
            if pivot_request is None:
                continue
            summary["relative_pivots"] += 1
            pivot_key = pivot_request["request_key"]
            cached_pivot = _cached(pivot_key)
            if cached_pivot is not None:
                pending[candidate_id] = (
                    _safe_relative_result(pivot_request, cached_pivot),
                    bool(initial_cached), fresh_spend, initial_fresh_calls,
                )
                summary["cached"] += 1
                summary["relative_pivot_cached"] += 1
                continue
            pivot_requests.setdefault(pivot_key, pivot_request)
            pivot_groups.setdefault(pivot_key, []).append(candidate_id)

        # Recheck pivot cache rows under the same run lock. A concurrent
        # request may have resolved the Tahoe ID after the first cache probe.
        for pivot_key in list(pivot_groups):
            cached_pivot = _cached(pivot_key)
            if cached_pivot is None:
                continue
            pivot_request = pivot_requests[pivot_key]
            for candidate_id in pivot_groups[pivot_key]:
                _initial, initial_cached, fresh_spend, initial_fresh_calls = pending[candidate_id]
                pending[candidate_id] = (
                    _safe_relative_result(pivot_request, cached_pivot),
                    bool(initial_cached), fresh_spend, initial_fresh_calls,
                )
                summary["cached"] += 1
                summary["relative_pivot_cached"] += 1
            pivot_groups.pop(pivot_key)

        pivot_keys = list(pivot_groups)
        allowed_pivot_keys: list[str] = []
        skipped_pivot_keys: list[str] = []
        pivot_skip_status = ""
        pivot_skip_message = ""
        pivot_candidate_count = sum(len(pivot_groups[key]) for key in pivot_keys)
        if not pivot_keys:
            pass
        elif not allow_enformion:
            skipped_pivot_keys = pivot_keys
            summary["skipped_consent"] += pivot_candidate_count
            pivot_skip_status = "relative_pivot_not_authorized"
            pivot_skip_message = (
                "The candidate's own relative record was not resolved because "
                "a fresh follow-up was not authorized."
            )
        elif not enabled():
            skipped_pivot_keys = pivot_keys
            summary["skipped_unavailable"] += pivot_candidate_count
            pivot_skip_status = "relative_pivot_unavailable"
            pivot_skip_message = (
                "The candidate's own relative record could not be resolved "
                "because the fallback service is unavailable."
            )
        else:
            remaining = max(0, effective_limit - run_calls)
            allowed_pivot_keys = pivot_keys[:remaining]
            skipped_pivot_keys = pivot_keys[remaining:]
            skipped_count = sum(
                len(pivot_groups[pivot_key]) for pivot_key in skipped_pivot_keys
            )
            summary["skipped_budget"] += skipped_count
            pivot_skip_status = "relative_pivot_budget_exhausted"
            pivot_skip_message = (
                "The candidate's own relative record was not resolved because "
                f"this run reached its {effective_limit}-call fallback limit."
            )

        for pivot_key in skipped_pivot_keys:
            pivot_request = pivot_requests[pivot_key]
            for candidate_id in pivot_groups[pivot_key]:
                (
                    initial_result, initial_cached, fresh_spend, initial_fresh_calls,
                ) = pending[candidate_id]
                pending[candidate_id] = (
                    _unresolved_relative_result(
                        pivot_request, initial_result,
                        pivot_skip_status, pivot_skip_message,
                    ),
                    initial_cached, fresh_spend, initial_fresh_calls,
                )
                summary["relative_pivot_skipped"] += 1

        pivot_throttled = provider_throttled
        for pivot_key in allowed_pivot_keys:
            candidate_ids_for_key = pivot_groups[pivot_key]
            candidate_id = candidate_ids_for_key[0]
            pivot_request = pivot_requests[pivot_key]
            made_call = not pivot_throttled
            if pivot_throttled:
                raw_result = _provider_throttle_deferred()
                summary["deferred_rate_limit"] += len(candidate_ids_for_key)
            else:
                try:
                    raw_result = _call_relative(pivot_request)
                except Exception as exc:
                    raw_result = {
                        "status": "error", "source": "enformion",
                        "emails": [], "phones": [], "addresses": [],
                        "error": (
                            "Enformion relative resolution failed "
                            f"({type(exc).__name__})."
                        ),
                    }
            call_credits = max(0, int(raw_result.get("credits_spent") or 0))
            safe_result = _safe_relative_result(pivot_request, raw_result)
            for grouped_candidate_id in candidate_ids_for_key:
                (
                    _initial, initial_cached, fresh_spend, initial_fresh_calls,
                ) = pending[grouped_candidate_id]
                pending[grouped_candidate_id] = (
                    safe_result, False, fresh_spend + call_credits,
                    initial_fresh_calls + (1 if made_call else 0),
                )
            if made_call and _cacheable_result(safe_result):
                store.save_provider_lookup(
                    _PROVIDER, run_id, candidate_id, pivot_key,
                    str(safe_result.get("status") or "error"), safe_result,
                    credits_spent=call_credits,
                )
            if made_call:
                summary["called"] += 1
                summary["relative_pivot_called"] += 1
                summary["run_credits_spent"] += call_credits
                _RUN_CALL_COUNTS[run_id] = _RUN_CALL_COUNTS.get(run_id, run_calls) + 1
                run_calls = _RUN_CALL_COUNTS[run_id]
                summary["run_calls_used"] = run_calls
            if (
                made_call and raw_result.get("retryable") is True
                and int(raw_result.get("http_status") or 0) == 429
            ):
                pivot_throttled = True

    for candidate_id, (ef_result, cached, fresh_spend, fresh_calls) in pending.items():
        candidate, pdl_result, request = context[candidate_id]
        merged = _apply_fallback(
            candidate, pdl_result, ef_result, request, cached=cached,
            fresh_credits_spent=fresh_spend,
            fresh_calls=fresh_calls,
        )
        output[candidate_id] = merged
        summary["checked"] += 1
        if merged.get("contact_verification", {}).get("status") in (
            "enformion_fallback", "enformion_supplemented",
        ):
            summary["matches"] += 1
            summary["verified"] += 1
    reason_counts = {}
    for result in output.values():
        contact_status = str(
            (result.get("contact_verification") or {}).get("status") or ""
        )
        quality_rule = str(_quality_gate(result).get("rule") or "")
        reason = str(
            result.get("reason_code") or contact_status or quality_rule
            or result.get("status") or "unknown"
        )
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    summary["reason_counts"] = dict(sorted(reason_counts.items()))
    return {"results": output, "summary": summary}
