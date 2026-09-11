"""
Batch enrichment orchestration: enrich stored candidates via Enformion, apply the
do-not-contact list, and advance the pipeline stage. Deterministic + testable.
"""
from __future__ import annotations

import time
import inspect

from . import (
    store, enformion_client as ef, verification, config, phone_policy, trust_policy,
    person_name,
)


def enrich_candidate(cid: int) -> dict:
    cand = store.get_candidate(cid)
    if not cand:
        return {"error": "candidate not found"}
    companies, schools, roles, aliases, relatives = [], [], [], [], []
    for line in str(cand.get("notes") or "").splitlines():
        cleaned = " ".join(line.split())
        lowered = cleaned.casefold()
        if lowered.startswith(("employer:", "company:")):
            companies.append(cleaned.split(":", 1)[1].strip())
        elif lowered.startswith("school:"):
            schools.append(cleaned.split(":", 1)[1].strip())
        elif lowered.startswith(("role:", "headline:", "job title:")):
            roles.append(cleaned.split(":", 1)[1].strip())
        elif lowered.startswith("relative:"):
            value = cleaned.split(":", 1)[1].strip()
            if len(value.split()) >= 2:
                relatives.append(value)
    aliases.extend(person_name.source_alternate_names(cand.get("notes") or ""))
    context = {
        "aliases": aliases, "companies": companies, "schools": schools,
        "roles": roles, "relatives": relatives,
    }
    parameters = inspect.signature(ef.enrich).parameters.values()
    accepts_context = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        or parameter.name == "aliases"
        for parameter in parameters
    )
    res = ef.enrich(
        cand["name"], cand.get("location", ""),
        **(context if accepts_context else {}),
    )
    status = res.get("status", "error")

    # enforce do-not-contact: drop any suppressed emails/phones
    emails = [e for e in res.get("emails", []) if not store.is_dnc(e)]
    preferred_values = set(phone_policy.preferred_phone_values(res))
    phones = [
        p for p in res.get("phones", [])
        if p in preferred_values and not store.is_dnc(p)
    ]
    res = {**res, "emails": emails, "phones": phones}
    verification_result = verification.assess(cand, res)
    verification_evidence = verification_result["evidence"]
    verification_evidence["phone_policy"] = phone_policy.selected_phone_policy(res)
    verification_evidence["enformion_phone_evidence"] = list(
        res.get("phone_evidence") or []
    )[:20]
    verification_evidence["enformion_email_evidence"] = list(
        res.get("email_evidence") or []
    )[:20]
    email_checks = {item["value"]: item for item in verification_result["emails"]}
    phone_checks = {item["value"]: item for item in verification_result["phones"]}
    emails = [
        value for value in emails
        if email_checks.get(value, {}).get("format_valid")
        and email_checks.get(value, {}).get("deliverability") not in ("invalid", "disposable")
    ]
    phones = [
        value for value in phones
        if phone_checks.get(value, {}).get("format_valid")
        and phone_checks.get(value, {}).get("line_status") != "invalid"
    ]
    if status == "success" and (
        verification_result["identity_status"] != "verified"
        or not (emails or phones)
    ):
        status = "no_match"
        emails = []
        phones = []

    automatic_use_allowed = bool(
        status == "success"
        and verification_result["identity_status"] == "verified"
        and (emails or phones)
    )
    contact_evidence = {
        "status": "enformion_direct",
        "phone_policy": phone_policy.selected_phone_policy(res),
        "checked_at": time.time(),
        "confidence_tier": "high" if automatic_use_allowed else "review",
        "automatic_use_allowed": automatic_use_allowed,
        "candidate_confirmed": False,
        "enformion_status": res.get("status") or "error",
        "enformion_error": str(res.get("error") or "")[:700],
        "enformion_error_code": str(res.get("provider_error_code") or "")[:120],
        "enformion_identity_status": verification_result["identity_status"],
        "enformion_identity_confidence": verification_result["identity_confidence"],
        "enformion_name_exact": bool((verification_evidence.get("name") or {}).get("exact")),
        "enformion_name_alias_match": bool(
            (verification_evidence.get("name") or {}).get("alias_match")
            or (verification_evidence.get("name") or {}).get("source_alias_match")
            or ((res.get("selection") or {}).get("identity_evidence") or {}).get("alias_match") is True
            or (res.get("selection") or {}).get("matched_name_type") == "provider_alias"
        ),
        "enformion_location_exact": bool(
            (verification_evidence.get("location") or {}).get("exact")
        ),
        "enformion_ranked_identity_match": bool(
            (res.get("selection") or {}).get("selection") == "ranked_identity_match"
            and ((res.get("selection") or {}).get("identity_evidence") or {}).get("admissible") is True
        ),
        "enformion_winner_margin": (
            (res.get("selection") or {}).get("winner_margin")
            if (res.get("selection") or {}).get("winner_margin") is not None
            else 1.0
        ),
        "enformion_conflicts": list(verification_evidence.get("conflicts") or []),
        "enformion_addresses": list(res.get("addresses") or [])[:20],
        "enformion_phone_evidence": list(res.get("phone_evidence") or [])[:20],
        "enformion_email_evidence": list(res.get("email_evidence") or [])[:20],
        "disclaimer": (
            "High-confidence provider data is not candidate-confirmed. Current ownership or "
            "deliverability requires a candidate reply, verification link, or phone verification."
        ),
    }
    selection = res.get("selection") or {}
    ranked_identity = selection.get("selection") == "ranked_identity_match"
    alias_identity = bool(
        contact_evidence["enformion_name_alias_match"]
        and selection.get("identity_evidence", {}).get("admissible") is True
    )
    if ranked_identity or alias_identity:
        automatic_use_allowed = bool(
            automatic_use_allowed
            and contact_evidence["enformion_winner_margin"]
                >= config.IDENTITY_AMBIGUITY_MARGIN
        )
        contact_evidence["automatic_use_allowed"] = automatic_use_allowed
        contact_evidence["confidence_tier"] = "high" if automatic_use_allowed else "review"
    # Keep a late ambiguity rejection from leaking provider contacts into the
    # canonical record. The provider evidence remains in the audit object for
    # review, but only an automatically admissible identity may supply contact
    # fields or address history.
    if status == "success" and not automatic_use_allowed:
        status = "no_match"
        emails = []
        phones = []
    verification_evidence.update({
        "contact_verification": contact_evidence,
        "contact_trust_policy": trust_policy.CONTACT_TRUST_POLICY,
        "source_identity_fingerprint": trust_policy.source_identity_fingerprint(cand),
    })
    verification_result["contact_verification_status"] = "enformion_direct"
    verification_result["provider_contacts"] = {
        "emails": list(emails) if automatic_use_allowed else [],
        "phones": list(phones) if automatic_use_allowed else [],
        "addresses": list(res.get("addresses") or [])[:20] if automatic_use_allowed else [],
    }

    fields = {
        "phones": phones, "emails": emails,
        "addresses": list(res.get("addresses") or []) if automatic_use_allowed else [],
        "enrich_status": status,
        "confidence": verification_result["identity_confidence"],
        "verification": verification_result,
    }
    if verification_result["identity_status"] == "verified":
        now = time.time()
        fields.update({
            "canonical_name": res.get("matched_name") or cand.get("name") or "",
            "identity_status": "verified",
            "identity_score": verification_result["identity_confidence"],
            "identity_provider": verification_result.get("source") or "enrichment_provider",
            "identity_evidence": verification_result.get("evidence") or {},
            "identity_verified_at": now,
            "contact_verified_at": now,
            "contact_expires_at": now + config.CONTACT_FRESHNESS_SECONDS,
        })
    # advance stage only on a real contactable match
    if status == "success" and (emails or phones) and cand["stage"] == "new":
        fields["stage"] = "enriched"
    store.update_candidate(cid, **fields)
    return {"id": cid, **fields, "error": res.get("error")}


def enrich_batch(job_id: int | None = None, only_pending: bool = True) -> dict:
    cands = store.list_candidates(job_id=job_id)
    done, matched, errors = 0, 0, 0
    for c in cands:
        if only_pending and c["enrich_status"] == "success":
            continue
        r = enrich_candidate(c["id"])
        done += 1
        if r.get("enrich_status") == "success" and (r.get("emails") or r.get("phones")):
            matched += 1
        elif r.get("error"):
            errors += 1
    return {"processed": done, "matched": matched, "errors": errors,
            "total_candidates": len(cands)}


def _unique(values: list[str], existing: list[str] | None = None) -> list[str]:
    output = []
    seen = set()
    for value in [*(existing or []), *(values or [])]:
        cleaned = str(value or "").strip()
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        output.append(cleaned)
        if len(output) >= 20:
            break
    return output


def _without_dnc(values: list[str], blocked: set[str]) -> list[str]:
    cleaned = [str(value or "").strip() for value in values or []]
    return [
        value for value in cleaned
        if value and store.contact_key(value) not in blocked
    ]


def _pdl_quality_gate(verification_result: dict, provider_result: dict) -> dict:
    """Decide whether one PDL identity is strong enough for automatic use.

    The provider's likelihood is not treated as a percentage and never replaces
    deterministic source evidence. Weak results remain eligible for Enformion
    fallback, but their contacts are not persisted first.
    """
    evidence = verification_result.get("evidence") or {}
    name = evidence.get("name") or {}
    location = evidence.get("location") or {}
    likelihood = int(provider_result.get("likelihood") or 0)
    social_match = bool(evidence.get("social_profile_match"))
    source_profile_expected = bool(evidence.get("expected_social_profile"))
    location_match = bool(
        location.get("exact") or evidence.get("provider_location_match")
    )
    context_match = max(
        float(evidence.get("role_overlap") or 0),
        float(evidence.get("organization_overlap") or 0),
    )
    # PDL's explicit matched-input evidence is stronger than our ability to
    # reproduce an organization string from the returned bundle.  A school or
    # employer can be abbreviated, omitted from the selected fields, or stale;
    # disagreement is never a hard conflict.  Agreement remains useful
    # corroboration, including when the provider confirms the supplied input.
    independent_context_match = max(
        float(evidence.get("organization_overlap") or 0),
        1.0 if evidence.get("provider_organization_input_match") is True else 0.0,
    )
    alias_match = bool(
        name.get("alias_match") or name.get("source_alias_match")
    )
    address_kind = str(evidence.get("matched_address_kind") or "")
    historical_location = address_kind in {"historical", "associated_undated"}
    accepted = False
    rule = "below_automatic_acceptance_threshold"
    if verification_result.get("identity_status") != "verified":
        rule = "identity_not_verified"
    elif not name.get("compatible"):
        rule = "name_not_exact"
    elif social_match and likelihood >= config.PDL_PROFILE_ACCEPT_MIN_LIKELIHOOD:
        accepted = True
        rule = "exact_name_and_social_profile"
    elif (
        alias_match
        and not (location.get("exact") and not historical_location)
        and independent_context_match < 0.8
    ):
        # A provider-explicit AKA plus the candidate's exact current/recent city
        # may identify the same person.  State-only or historical AKA matches
        # still require independent employer/school context (or the confirmed
        # social-profile route above).
        rule = "alias_needs_independent_corroboration"
    elif historical_location and independent_context_match < 0.8:
        rule = "historical_address_needs_independent_corroboration"
    elif (
        source_profile_expected and not social_match
        and location_match and independent_context_match >= 0.8
        and likelihood >= config.PDL_AUTO_ACCEPT_MIN_LIKELIHOOD
        and name.get("exact")
    ):
        # Absence of a social URL in PDL is missing evidence, not contradictory
        # evidence. Exact visible name + exact/PDL-matched location + a strong
        # independently captured employer/school match may still establish a
        # high-confidence identity. A different explicit social URL is never
        # treated as corroboration here.
        accepted = True
        rule = "exact_name_location_context_without_profile_confirmation"
    elif source_profile_expected and not social_match:
        rule = "source_social_profile_not_confirmed"
    elif (
        evidence.get("state_and_organization_match") is True
        and name.get("exact")
        and likelihood >= config.PDL_AUTO_ACCEPT_MIN_LIKELIHOOD
    ):
        accepted = True
        rule = "exact_name_state_and_organization"
    elif location_match and likelihood >= config.PDL_AUTO_ACCEPT_MIN_LIKELIHOOD:
        accepted = True
        rule = "exact_name_location_and_high_likelihood"
    elif context_match >= 0.5 and likelihood >= max(9, config.PDL_AUTO_ACCEPT_MIN_LIKELIHOOD):
        accepted = True
        rule = "exact_name_context_and_very_high_likelihood"
    return {
        "accepted": accepted,
        "rule": rule,
        "likelihood": likelihood,
        "minimum_likelihood": (
            config.PDL_PROFILE_ACCEPT_MIN_LIKELIHOOD
            if social_match else config.PDL_AUTO_ACCEPT_MIN_LIKELIHOOD
        ),
        "exact_name": bool(name.get("exact")),
        "compatible_name": bool(name.get("compatible")),
        "name_match_source": str(name.get("match_source") or "primary"),
        "source_name_match": str(name.get("expected_source") or "source_name"),
        "alias_match": alias_match,
        "matched_address_kind": address_kind,
        "location_match": location_match,
        "social_profile_match": social_match,
        "source_profile_expected": source_profile_expected,
        "context_overlap": context_match,
        "independent_context_overlap": independent_context_match,
    }


def save_provider_result(
    cid: int,
    result: dict,
    candidate: dict | None = None,
    *,
    persist: bool = True,
    dnc_blocked: set[str] | None = None,
) -> dict:
    """Validate and merge a licensed-provider result.

    ``dnc_blocked`` lets a batch caller resolve the do-not-contact list once for
    a whole run instead of querying the database for every candidate.
    """
    cand = candidate or store.get_candidate(cid)
    if not cand:
        return {"error": "candidate not found"}
    source = str(result.get("source") or "").strip().lower()
    if source != "people_data_labs":
        return {"error": "unsupported contact lookup provider"}

    raw_emails = _unique(result.get("emails", []))
    pdl_phone_evidence = list(result.get("phone_evidence") or [])
    raw_phones = _unique(phone_policy.preferred_phone_values(result))
    if dnc_blocked is None:
        allowed = store.filter_dnc_groups({
            "emails": raw_emails,
            "phones": raw_phones,
        })
    else:
        allowed = {
            "emails": _without_dnc(raw_emails, dnc_blocked),
            "phones": _without_dnc(raw_phones, dnc_blocked),
        }
    emails = _unique(allowed.get("emails", []))
    phones = _unique(allowed.get("phones", []))
    addresses = _unique(result.get("addresses", []))
    try:
        pdl_likelihood = max(0, min(10, int(result.get("likelihood") or 0)))
    except (TypeError, ValueError):
        pdl_likelihood = 0
    provider_result = {
        "source": source,
        "status": "success" if result.get("status") == "success" else "no_match",
        "matched_name": str(result.get("matched_name") or "")[:200],
        "emails": raw_emails,
        "phones": raw_phones,
        "addresses": addresses,
        "confidence": max(0.0, min(1.0, float(result.get("confidence") or 0))),
        "matched_inputs": [
            str(value).strip().casefold()
            for value in result.get("matched_inputs", [])
            if str(value).strip()
        ][:20],
        "provider_location": str(result.get("provider_location") or "")[:500],
        "provider_job_title": str(result.get("provider_job_title") or "")[:500],
        "provider_company": str(result.get("provider_company") or "")[:500],
        "pdl_id": str(result.get("pdl_id") or "")[:200],
        "likelihood": pdl_likelihood,
        "profile_url": str(result.get("profile_url") or "")[:2000],
        "provider_roles": result.get("provider_roles") or [],
        "provider_organizations": result.get("provider_organizations") or [],
        "name_aliases": result.get("name_aliases") or [],
        "name_alias_evidence": result.get("name_alias_evidence") or [],
        "source_name_aliases": result.get("source_name_aliases") or [],
        "source_name_alias_evidence": result.get("source_name_alias_evidence") or [],
        "profile_urls": result.get("profile_urls") or [],
        "profile_evidence": result.get("profile_evidence") or [],
        "address_evidence": result.get("address_evidence") or [],
        "email_evidence": result.get("email_evidence") or [],
        "associated_phone_evidence": result.get("associated_phone_evidence") or [],
        "required_field": str(result.get("required_field") or "usable_contact"),
        "allow_state_context_match": True,
        "phone_policy": str(result.get("phone_policy") or ""),
        "phone_evidence": pdl_phone_evidence[:20],
        "lookup_strategy": str(result.get("lookup_strategy") or "")[:80],
        "lookup_attempt_count": max(0, int(result.get("lookup_attempt_count") or 0)),
        "lookup_attempted_strategies": [
            str(value).strip()[:80]
            for value in result.get("lookup_attempted_strategies", [])
            if str(value).strip()
        ][:5],
    }
    verification_result = verification.assess(cand, provider_result)
    verification_result["profile_url"] = provider_result["profile_url"]
    verification_result["evidence"].update({
        "pdl_likelihood": provider_result["likelihood"],
        "provider_location": provider_result["provider_location"],
        "provider_job_title": provider_result["provider_job_title"],
        "provider_company": provider_result["provider_company"],
        "provider_roles": provider_result["provider_roles"],
        "provider_organizations": provider_result["provider_organizations"],
        "name_aliases": provider_result["name_aliases"],
        "name_alias_evidence": provider_result["name_alias_evidence"],
        "source_name_aliases": provider_result["source_name_aliases"],
        "source_name_alias_evidence": provider_result["source_name_alias_evidence"],
        "profile_urls": provider_result["profile_urls"],
        "profile_evidence": provider_result["profile_evidence"],
        "address_evidence": provider_result["address_evidence"],
        "pdl_email_evidence": provider_result["email_evidence"],
        "pdl_associated_phone_evidence": provider_result["associated_phone_evidence"],
        "required_field": provider_result["required_field"],
        "pdl_id": provider_result["pdl_id"],
        "phone_policy": phone_policy.selected_phone_policy(provider_result),
        "pdl_phone_evidence": provider_result["phone_evidence"],
        "lookup_strategy": provider_result["lookup_strategy"],
        "lookup_attempt_count": provider_result["lookup_attempt_count"],
        "lookup_attempted_strategies": provider_result["lookup_attempted_strategies"],
        "contact_trust_policy": trust_policy.CONTACT_TRUST_POLICY,
        "source_identity_fingerprint": trust_policy.source_identity_fingerprint(cand),
    })
    upstream = cand.get("identity_evidence") or {}
    upstream_provider = str(cand.get("identity_provider") or "").strip().casefold()
    # Preserve independent pre-enrichment identity evidence (for example an
    # NPPES resolution), but never wrap a previous PDL verification inside the
    # next PDL verification.  Repeated retries otherwise create recursively
    # growing ``upstream_resolution`` trees and needless database bloat.
    if upstream and upstream_provider not in {
        "people_data_labs", "people data labs", "pdl",
    }:
        compact_upstream = dict(upstream)
        compact_upstream.pop("upstream_resolution", None)
        verification_result["evidence"]["upstream_resolution"] = compact_upstream
    quality_gate = _pdl_quality_gate(verification_result, provider_result)
    verification_result["evidence"]["pdl_quality_gate"] = quality_gate
    email_checks = {item["value"]: item for item in verification_result["emails"]}
    phone_checks = {item["value"]: item for item in verification_result["phones"]}
    emails = [
        value for value in emails
        if email_checks.get(value, {}).get("format_valid")
        and email_checks.get(value, {}).get("deliverability") not in ("invalid", "disposable")
    ]
    phones = [
        value for value in phones
        if phone_checks.get(value, {}).get("format_valid")
        and phone_checks.get(value, {}).get("line_status") != "invalid"
    ]
    identity_status = verification_result["identity_status"]
    social_profile_unconfirmed = bool(
        quality_gate.get("rule") == "source_social_profile_not_confirmed"
    )
    accepted_provider_identity = bool(
        identity_status == "verified" and quality_gate.get("accepted")
    )
    persisted_identity_status = "review" if social_profile_unconfirmed else identity_status
    identity_fields = {
        "canonical_name": (
            provider_result["matched_name"]
            if persisted_identity_status == "verified" else ""
        ),
        "identity_status": persisted_identity_status,
        "identity_score": verification_result["identity_confidence"],
        "identity_provider": source,
        # A provider ID is a duplicate/reuse key, not harmless diagnostics.
        # Persist a newly returned ID only after the complete PDL quality gate.
        # Explicitly clear legacy IDs when LinkedIn/Facebook supplied a profile
        # but PDL did not confirm that profile.
        "provider_person_id": (
            provider_result["pdl_id"]
            if accepted_provider_identity
            else ("" if social_profile_unconfirmed else cand.get("provider_person_id") or "")
        ),
        "identity_evidence": verification_result["evidence"],
        "verification": verification_result,
        "confidence": verification_result["identity_confidence"],
    }
    if social_profile_unconfirmed:
        identity_fields["master_candidate_id"] = None
    if accepted_provider_identity:
        identity_fields["identity_verified_at"] = time.time()
    elif social_profile_unconfirmed:
        identity_fields["identity_verified_at"] = 0
    if (
        provider_result["status"] != "success" or not (emails or phones)
        or identity_status != "verified" or not quality_gate["accepted"]
    ):
        needs_review = identity_status == "review" or (
            identity_status == "verified" and not quality_gate["accepted"]
        )
        identity_fields["enrich_status"] = "review" if needs_review else "no_match"
        if (
            (cand.get("verification") or {}).get("source") == source
            and (identity_status in ("review", "rejected") or not quality_gate["accepted"])
        ):
            identity_fields.update({"emails": [], "phones": [], "addresses": []})
        if persist:
            store.update_candidate(cid, **identity_fields)
        reason = (
            "PDL returned a possible identity, but its likelihood/evidence was below "
            "the automatic acceptance threshold; Enformion fallback is required."
            if identity_status == "verified" and not quality_gate["accepted"]
            else {
            "verified": "The identity was verified, but no contact remained after do-not-contact suppression.",
            "review": "PDL returned contact data, but the identity needs recruiter review before contacts can be saved.",
            "rejected": "PDL returned a conflicting identity; no contact was saved.",
            "no_match": "People Data Labs returned no usable verified contact values.",
            }.get(identity_status, "PDL identity evidence was not sufficient to save contact data.")
        )
        return {
            "id": cid,
            "enrich_status": "review" if needs_review else "no_match",
            "emails": [],
            "phones": [],
            "addresses": [],
            "confidence": verification_result["identity_confidence"],
            "verification": verification_result,
            "candidate_updates": identity_fields,
            "error": reason,
        }

    duplicate = store.get_candidate_by_provider_person_id(
        provider_result["pdl_id"], exclude_id=cid,
    )
    if duplicate:
        master_id = int(duplicate.get("master_candidate_id") or duplicate["id"])
        identity_fields["master_candidate_id"] = master_id
        verification_result["evidence"]["existing_candidate"] = {
            "id": master_id,
            "source": duplicate.get("source") or "",
            "stage": duplicate.get("stage") or "new",
        }

    legacy_emails = _unique([
        *(cand.get("emails") or []), *((duplicate or {}).get("emails") or []),
    ])
    if legacy_emails:
        verification_result["evidence"]["previous_emails_replaced"] = legacy_emails
    merged_emails = _unique(emails)
    existing_phone_sources = [record for record in (cand, duplicate) if record]
    quarantined_legacy_phones = [
        value
        for record in existing_phone_sources
        if phone_policy.verification_phone_policy(record) not in {
            phone_policy.MOBILE_PHONE_POLICY, phone_policy.OTHER_PHONE_POLICY,
        }
        for value in (record.get("phones") or [])
    ]
    if quarantined_legacy_phones:
        verification_result["evidence"]["legacy_untyped_phones_quarantined"] = _unique(
            quarantined_legacy_phones
        )
    previous_provider_phones = _unique([
        value
        for record in existing_phone_sources
        for value in (record.get("phones") or [])
        if value not in phones
    ])
    if previous_provider_phones:
        verification_result["evidence"]["previous_phones_replaced"] = previous_provider_phones
    previous_addresses = _unique([
        *(cand.get("addresses") or []), *((duplicate or {}).get("addresses") or []),
    ])
    if previous_addresses:
        verification_result["evidence"]["previous_addresses_replaced"] = previous_addresses
    # Active contact columns contain only values from the current accepted
    # provider response. Older values remain in evidence/history for audit but
    # cannot leak into ATS views or a future database reuse.
    merged_phones = _unique(phones)
    merged_addresses = _unique(addresses)
    now = time.time()
    verification_result["provider_contacts"] = {
        "emails": emails, "phones": phones, "addresses": addresses,
    }
    fields = {
        "emails": merged_emails,
        "phones": merged_phones,
        "addresses": merged_addresses,
        "enrich_status": "success",
        "confidence": verification_result["identity_confidence"],
        "verification": verification_result,
        **identity_fields,
        "identity_verified_at": now,
        "contact_verified_at": now,
        "contact_expires_at": now + config.CONTACT_FRESHNESS_SECONDS,
    }
    if cand["stage"] == "new":
        fields["stage"] = "enriched"
    if persist:
        store.update_candidate(cid, **fields)
    return {
        "id": cid,
        **fields,
        "emails": emails,
        "phones": phones,
        "addresses": addresses,
        "stored_emails": merged_emails,
        "stored_phones": merged_phones,
        "stored_addresses": merged_addresses,
        "candidate_updates": fields,
        "error": None,
    }
