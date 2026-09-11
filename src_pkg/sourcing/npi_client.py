"""Read-only NPPES corroboration for US healthcare candidates.

NPPES contributes public identity/location/taxonomy evidence only. It does not
provide personal contact information and it is not proof of active licensure.
"""
from __future__ import annotations

import re

import httpx

from . import config


def _normal(value) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def is_healthcare(candidate: dict) -> bool:
    text = _normal(f"{candidate.get('notes', '')} {candidate.get('name', '')}")
    terms = (
        "nurse", "registered nurse", "rn", "lpn", "lvn", "physician",
        "doctor", "therapist", "pharmacist", "radiology", "technologist",
        "healthcare", "medical", "clinical",
    )
    return any(re.search(rf"\b{re.escape(term)}\b", text) for term in terms)


def _state(location: str) -> str:
    parts = [part.strip() for part in str(location or "").split(",")]
    if len(parts) < 2:
        return ""
    value = re.sub(r"[^A-Za-z]", "", parts[1]).upper()
    return value if len(value) == 2 else ""


def search(candidate: dict) -> list[dict]:
    if not config.NPI_ENABLED or not is_healthcare(candidate):
        return []
    name = _normal(candidate.get("name", "")).split()
    if not name:
        return []
    params = {
        "version": "2.1", "enumeration_type": "NPI-1",
        "first_name": name[0], "limit": 200,
    }
    # NPPES wildcard matching is useful for a platform that masks the surname.
    masked_last_initial = name[-1] if len(name) > 1 and len(name[-1]) == 1 else ""
    if len(name) > 1 and not masked_last_initial:
        params["last_name"] = name[-1]
    state = _state(candidate.get("location", ""))
    if state:
        params["state"] = state
    response = httpx.get(config.NPI_BASE_URL, params=params, timeout=config.NPI_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    output = []
    for row in payload.get("results", []) if isinstance(payload, dict) else []:
        basic = row.get("basic") or {}
        first = basic.get("first_name") or basic.get("authorized_official_first_name")
        last = basic.get("last_name") or basic.get("authorized_official_last_name")
        full_name = " ".join(str(value or "").strip() for value in (first, last) if value)
        if masked_last_initial and not _normal(last).startswith(masked_last_initial):
            continue
        addresses = row.get("addresses") or []
        location = ""
        for address in addresses:
            if str(address.get("address_purpose") or "").upper() == "LOCATION":
                location = ", ".join(
                    str(address.get(key) or "").strip()
                    for key in ("city", "state") if address.get(key)
                )
                break
        taxonomies = [
            item.get("desc") for item in row.get("taxonomies") or []
            if isinstance(item, dict) and item.get("desc")
        ]
        if full_name:
            output.append({
                "full_name": full_name,
                "location_name": location,
                "job_title": taxonomies[0] if taxonomies else "",
                "roles": taxonomies,
                "organizations": [],
                "provider_person_id": str(row.get("number") or ""),
                "provider": "nppes",
                "matched_inputs": ["name", "region"],
                "raw": {"npi": row.get("number"), "taxonomies": taxonomies[:10]},
            })
    return output
