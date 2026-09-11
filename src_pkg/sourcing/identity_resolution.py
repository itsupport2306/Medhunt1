"""Source-agnostic identity resolution before contact enrichment."""
from __future__ import annotations

import time

from . import config, npi_client, person_name, store, verification


def _source_roles(candidate: dict) -> list[str]:
    notes = str(candidate.get("notes") or "")
    values = []
    for line in notes.splitlines():
        clean = line.strip()
        if clean.casefold().startswith(("role:", "headline:", "job title:")):
            values.append(clean.split(":", 1)[-1].strip())
    if not values and notes.strip():
        values.append(notes.splitlines()[0].strip())
    return values


def _source_orgs(candidate: dict) -> list[str]:
    output = []
    for line in str(candidate.get("notes") or "").splitlines():
        clean = line.strip()
        if clean.casefold().startswith(("employer:", "company:", "school:")):
            output.append(clean.split(":", 1)[-1].strip())
    return output


def _result_shape(person: dict) -> dict:
    return {
        "status": "success",
        "source": person.get("provider") or "identity_candidate",
        "matched_name": person.get("full_name") or person.get("name") or "",
        "provider_location": person.get("location_name") or person.get("location") or "",
        "addresses": [person.get("location_name") or person.get("location") or ""],
        "provider_job_title": person.get("job_title") or "",
        "provider_company": person.get("job_company_name") or "",
        "provider_roles": person.get("roles") or [],
        "provider_organizations": person.get("organizations") or [],
        "matched_inputs": person.get("matched_inputs") or [],
        "emails": [],
        "phones": [],
        "confidence": person.get("confidence", 1.0),
    }


def score(candidate: dict, person: dict) -> dict:
    shaped = _result_shape(person)
    assessed = verification.assess(candidate, shaped, identity_only=True)
    evidence = assessed["evidence"]
    name = evidence["name"]
    location = evidence["location"]
    # Discovery accepts an identity only with exact city/state. A matched state
    # or other provider input is corroboration, not enough to link a person or
    # inherit contacts: common names in the same state are not unique people.
    name_tokens = str(candidate.get("name") or "").split()
    partial_source = bool(name_tokens and len(name_tokens[-1]) == 1)
    corroborator = max(evidence["role_overlap"], evidence["organization_overlap"])
    verified = (
        not evidence["conflicts"]
        and (name["exact"] or name["partial"])
        and location["exact"]
        and (not partial_source or corroborator >= 0.35)
    )
    if person.get("provider") == "internal_database" and corroborator < 0.35:
        verified = False
    value = assessed["identity_confidence"]
    threshold = (
        max(config.IDENTITY_REVIEW_THRESHOLD, 0.70)
        if partial_source else config.IDENTITY_MATCH_THRESHOLD
    )
    return {
        "person": person, "score": round(value, 3),
        "eligible": bool(verified and value >= threshold),
        "evidence": evidence,
    }


def choose(candidate: dict, people: list[dict], provider: str) -> dict:
    ranked = sorted(
        (score(candidate, person) for person in people or []),
        key=lambda item: item["score"], reverse=True,
    )
    eligible = [item for item in ranked if item["eligible"]]
    if not eligible:
        return {"status": "no_match", "provider": provider, "candidates_reviewed": len(ranked)}
    best = eligible[0]
    runner_up = eligible[1] if len(eligible) > 1 else None
    margin = best["score"] - (runner_up["score"] if runner_up else 0)
    if runner_up and margin < config.IDENTITY_AMBIGUITY_MARGIN:
        return {
            "status": "review", "provider": provider,
            "score": best["score"], "margin": round(margin, 3),
            "candidates_reviewed": len(ranked),
            "message": "More than one identity has nearly equal evidence.",
            "alternatives": [
                {"name": item["person"].get("full_name", ""), "score": item["score"]}
                for item in eligible[:3]
            ],
        }
    person = best["person"]
    return {
        "status": "verified", "provider": provider,
        "canonical_name": person.get("full_name") or person.get("name") or "",
        "provider_person_id": str(person.get("provider_person_id") or person.get("id") or ""),
        "score": best["score"], "margin": round(margin, 3),
        "evidence": best["evidence"], "candidates_reviewed": len(ranked),
    }


def _internal_people(candidate: dict) -> list[dict]:
    people = []
    for row in store.find_identity_candidates(candidate.get("name", "")):
        if int(row.get("id") or 0) == int(candidate.get("id") or 0):
            continue
        evidence = row.get("identity_evidence") or {}
        people.append({
            "full_name": row.get("canonical_name") or row.get("name") or "",
            "location_name": row.get("location") or evidence.get("provider_location") or "",
            "job_title": evidence.get("provider_job_title") or "",
            "job_company_name": evidence.get("provider_company") or "",
            "roles": evidence.get("provider_roles") or [],
            "organizations": evidence.get("provider_organizations") or [],
            "provider_person_id": row.get("provider_person_id") or "",
            "provider": "internal_database",
            "confidence": row.get("identity_score") or 1,
            "matched_inputs": ["name", "location"],
        })
    return people


def resolve_internal(candidate: dict) -> dict:
    """Resolve against verified first-party records without an external call."""
    result = choose(candidate, _internal_people(candidate), "internal_database")
    result["credits_spent"] = 0
    return result


def resolve(candidate: dict, discover=None) -> dict:
    """Resolve a captured identity using internal, NPPES, then paid discovery."""
    if candidate.get("identity_status") in ("verified", "recruiter_confirmed") and candidate.get("canonical_name"):
        return {
            "status": "verified", "provider": candidate.get("identity_provider") or "stored",
            "canonical_name": candidate["canonical_name"],
            "provider_person_id": candidate.get("provider_person_id") or "",
            "score": candidate.get("identity_score") or 1,
            "evidence": candidate.get("identity_evidence") or {}, "credits_spent": 0,
        }
    safe_name = person_name.normalize_person_name(candidate.get("name") or "")
    if len(safe_name.split()) < 2:
        return {
            "status": "no_match", "provider": "identity_resolution",
            "message": "A full two-part person name is required before contact lookup.",
            "attempts": [], "credits_spent": 0,
        }
    candidate = {**candidate, "name": safe_name}
    attempts = []
    internal = resolve_internal(candidate)
    attempts.append(internal)
    if internal["status"] == "verified":
        return {**internal, "attempts": attempts, "credits_spent": 0}
    if npi_client.is_healthcare(candidate):
        try:
            npi = choose(candidate, npi_client.search(candidate), "nppes")
        except Exception as exc:
            npi = {"status": "error", "provider": "nppes", "error": type(exc).__name__}
        attempts.append(npi)
        if npi.get("status") == "verified":
            return {**npi, "attempts": attempts, "credits_spent": 0}
    if discover:
        paid = discover(candidate)
        people = paid.get("people") or []
        found = choose(candidate, people, paid.get("provider") or "people_data_labs_discovery")
        found["credits_spent"] = int(paid.get("credits_spent") or 0)
        attempts.append({**found, "provider_response": paid.get("status")})
        if found.get("status") == "verified":
            return {**found, "attempts": attempts}
    reviews = [item for item in attempts if item.get("status") == "review"]
    review = reviews[-1] if reviews else {}
    return {
        "status": "review" if reviews else "no_match",
        "provider": (review if reviews else attempts[-1]).get("provider", "identity_resolution"),
        "score": review.get("score", 0), "margin": review.get("margin", 0),
        "message": review.get("message", ""),
        "alternatives": review.get("alternatives", []),
        "attempts": attempts, "credits_spent": sum(int(item.get("credits_spent") or 0) for item in attempts),
    }


def persist(candidate_id: int, resolution: dict) -> None:
    status = resolution.get("status") or "no_match"
    now = time.time()
    provider_person_id = resolution.get("provider_person_id") or ""
    duplicate = store.get_candidate_by_provider_person_id(provider_person_id, exclude_id=candidate_id)
    fields = dict(
        canonical_name=resolution.get("canonical_name") or "",
        identity_status=status,
        identity_score=float(resolution.get("score") or 0),
        identity_provider=resolution.get("provider") or "",
        provider_person_id=provider_person_id,
        identity_evidence={
            **(resolution.get("evidence") or {}),
            "resolution_attempts": resolution.get("attempts") or [],
        },
        identity_verified_at=now if status == "verified" else 0,
    )
    if duplicate and status == "verified":
        fields["master_candidate_id"] = int(
            duplicate.get("master_candidate_id") or duplicate["id"]
        )
    store.update_candidate(candidate_id, **fields)
