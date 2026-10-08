"""Deliver Ceipal-assigned, verified contacts through the Halo service channel."""
from __future__ import annotations

import logging

from . import contact_access, healthboard_auth, store


def upload_candidate(candidate_id: int, user_id: str) -> str:
    candidate = store.get_candidate(candidate_id)
    if not candidate:
        return "failed"
    owner = str(user_id or "local")
    route = store.get_candidate_ats_route(candidate_id, owner) or {}
    if route.get("destination") != "ceipal":
        return "disabled"
    eligibility = dict(route.get("eligibility") or {})
    previous = eligibility.get("ceipal_upload") or {}
    if previous.get("state") in {"uploaded_to_ceipal", "already_in_ceipal"}:
        return "uploaded" if previous.get("state") == "uploaded_to_ceipal" else "already_in_ceipal"

    projected = contact_access.project_candidate(candidate)
    if not projected.get("contacts_trusted") or not (
        projected.get("emails") or projected.get("phones")
    ):
        return "waiting_for_contact"
    wireless_phones = [
        str(item.get("value") or "").strip()
        for item in projected.get("phone_contacts") or []
        if isinstance(item, dict)
        and str(item.get("kind") or "").casefold() in {"wireless", "mobile"}
    ]
    payload = {
        "candidate_id": str(candidate_id),
        "name": str(candidate.get("canonical_name") or candidate.get("name") or "").strip(),
        "location": str(candidate.get("location") or "").strip(),
        "emails": list(projected.get("emails") or []),
        "phones": list(projected.get("phones") or []),
        "wireless_phones": list(dict.fromkeys(wireless_phones)),
    }
    try:
        result = healthboard_auth.medhunt_ceipal_candidate(
            user_id=owner, candidate=payload,
        )
        state = str(result.get("state") or "uploaded_to_ceipal")
        eligibility["ceipal_upload"] = {
            "state": state,
            "applicant_id": str(result.get("applicant_id") or ""),
            "checked": bool(result.get("checked", True)),
        }
        store.set_candidate_ats_route(candidate_id, owner, "ceipal", eligibility)
        return "uploaded" if state == "uploaded_to_ceipal" else state
    except Exception as exc:
        eligibility["ceipal_upload"] = {
            "state": "failed",
            "error": str(exc)[:240],
            "checked": False,
        }
        store.set_candidate_ats_route(candidate_id, owner, "ceipal", eligibility)
        logging.getLogger("medhunt.ceipal").warning(
            "Ceipal upload failed for candidate %s: %s", candidate_id,
            str(exc)[:240],
        )
        return "failed"
