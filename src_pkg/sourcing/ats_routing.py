"""Keep each Medhunt recruiter on exactly one assigned ATS workflow."""
from __future__ import annotations

import logging
from collections.abc import Mapping

from . import contact_access, healthboard_auth, nexus_eligibility, store


def destination_for(identity: Mapping | None) -> str:
    """Use an explicit exclusive assignment, defaulting to Nexus.

    Conflicting legacy flags resolve to Nexus; Halo prevents creating new
    conflicts, and no candidate is sent to both ATSs.
    """
    targets = (identity or {}).get("delivery_targets") or {}
    return "ceipal" if targets.get("ceipal") is True and targets.get("nexus") is not True else "nexus"


def set_candidate_target(candidate_id: int, destination: str, user_id: str = "") -> None:
    if not store.get_candidate(int(candidate_id)):
        return
    owner = str(user_id or "local")
    store.set_candidate_ats_route(candidate_id, owner, destination)


def stored(
    candidate: Mapping | None, user_id: str = "", default_destination: str = "nexus",
) -> dict:
    source = candidate or {}
    user_route = store.get_candidate_ats_route(
        int(source.get("id") or 0), str(user_id or "local"),
    ) if source.get("id") else None
    routing = user_route or {}
    destination = str(
        routing.get("destination") or default_destination or "nexus"
    ).casefold()
    if user_route:
        eligibility = routing.get("eligibility") or {}
    elif destination == "nexus":
        eligibility = nexus_eligibility.stored_result(source)
    else:
        # ATS assignment results are user-specific; never reuse another
        # recruiter's legacy candidate-level Ceipal status.
        eligibility = {}
    return {"destination": destination, "eligibility": dict(eligibility or {})}


def _ceipal_payload(candidate: dict) -> dict:
    projected = contact_access.project_candidate(candidate)
    phones = [str(value).strip() for value in projected.get("phones") or [] if str(value).strip()]
    wireless = []
    for item in projected.get("phone_contacts") or []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("kind") or "").casefold() in {
            "mobile", "wireless", "cell", "cellular",
        }:
            value = str(item.get("value") or "").strip()
            if value and value not in wireless:
                wireless.append(value)
    emails = [str(value).strip() for value in projected.get("emails") or [] if str(value).strip()]
    return {
        "name": str(candidate.get("name") or candidate.get("canonical_name") or "").strip(),
        "location": str(candidate.get("location") or "").strip(),
        "emails": list(dict.fromkeys(emails)),
        "phones": list(dict.fromkeys(phones)),
        "wireless_phones": wireless,
    }


def check_after_enrichment(candidate_id: int, destination: str, user_id: str = "") -> dict:
    """Check the assigned ATS only after Quick Sourcer has saved contacts."""
    target = "ceipal" if destination == "ceipal" else "nexus"
    candidate = store.get_candidate(int(candidate_id)) or {}
    existing_route = stored(candidate, user_id)
    if (
        target == "ceipal"
        and existing_route.get("destination") == "ceipal"
        and (existing_route.get("eligibility") or {}).get("state") == "created_in_ceipal"
        and not (existing_route.get("eligibility") or {}).get("blocked")
    ):
        return dict(existing_route["eligibility"])
    try:
        if target == "ceipal":
            result = healthboard_auth.medhunt_ceipal_candidate(
                user_id=user_id, candidate=_ceipal_payload(candidate),
            )
        else:
            result = nexus_eligibility.check_candidate(candidate, fresh=True)
    except (nexus_eligibility.NexusEligibilityUnavailable, Exception) as exc:
        message = "Ceipal check unavailable" if target == "ceipal" else "Nexus check unavailable"
        logging.getLogger("medhunt.ats").warning(
            "%s failed for candidate %s (%s).", target, candidate_id, type(exc).__name__,
        )
        result = {"state": "unavailable", "blocked": True, "checked": False, "error": message}
    result = {**dict(result or {}), "target": target}
    store.set_candidate_ats_route(candidate_id, user_id or "local", target, result)
    return result
