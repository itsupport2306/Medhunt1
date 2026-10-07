"""Repair extension-created Nexus candidates whose specialty is still Unknown.

The command is a dry run unless both ``--apply`` and the explicit confirmation
text are supplied. It never searches by contact data and never prints PII.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from src_pkg.sourcing import nexus_delivery, nexus_sync, store


CONFIRMATION = "APPLY_SPECIALTY_REPAIR"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--after-delivery-id", type=int, default=0)
    parser.add_argument("--candidate-id", action="append", type=int, default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    values = parser.parse_args()
    values.limit = max(1, min(500, values.limit))
    if values.apply and values.confirm != CONFIRMATION:
        parser.error(f"--apply requires --confirm {CONFIRMATION}")
    return values


def _master_id(row: Mapping[str, Any], kind: str) -> int | None:
    keys = (
        ("specialtyId", "id", "value")
        if kind == "specialties"
        else ("professionId", "id", "value")
    )
    for key in keys:
        try:
            value = int(row.get(key))
        except (TypeError, ValueError):
            continue
        if value:
            return value
    return None


def _name(row: Mapping[str, Any]) -> str:
    for key in ("name", "label", "description", "specialtyName", "professionName"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _ids(value: Any, *, specialty_context: bool = False) -> set[int]:
    found: set[int] = set()
    if isinstance(value, Mapping):
        for key, nested in value.items():
            folded = key.casefold()
            if folded in {
                "specialtyid", "specialityid", "primaryspecialtyid",
                "primaryspecialityid",
            } or (specialty_context and folded in {"id", "value"}):
                try:
                    found.add(int(nested))
                except (TypeError, ValueError):
                    pass
            elif folded in {
                "specialtyids", "specialityids", "specialties", "specialities",
            }:
                found.update(_ids(nested, specialty_context=True))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for nested in value:
            found.update(_ids(nested, specialty_context=specialty_context))
            if specialty_context and not isinstance(nested, (Mapping, Sequence)):
                try:
                    found.add(int(nested))
                except (TypeError, ValueError):
                    pass
    return {value for value in found if value > 0}


def _specialty_names(value: Any, *, specialty_context: bool = False) -> set[str]:
    names: set[str] = set()
    if isinstance(value, Mapping):
        for key, nested in value.items():
            folded = key.casefold()
            if folded in {"specialtyname", "specialityname"} or (
                specialty_context and folded in {"name", "label", "description"}
            ):
                text = str(nested or "").strip().casefold()
                if text:
                    names.add(text)
            elif folded in {"specialties", "specialities", "specialty", "speciality"}:
                names.update(_specialty_names(nested, specialty_context=True))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for nested in value:
            names.update(_specialty_names(nested, specialty_context=specialty_context))
    elif specialty_context and isinstance(value, str) and value.strip():
        names.add(value.strip().casefold())
    return names


def _is_unknown(candidate: Mapping[str, Any], unknown_ids: set[int]) -> bool | None:
    specialty_ids = _ids(candidate)
    if specialty_ids:
        return specialty_ids.issubset(unknown_ids)
    names = _specialty_names(candidate)
    if names:
        return all(name in {"unknown", "unknown specialty"} for name in names)
    return None


def _rows(args: argparse.Namespace) -> list[dict[str, Any]]:
    requested = set(args.candidate_id)
    rows = []
    for row in reversed(store.list_nexus_deliveries()):
        if int(row.get("id") or 0) <= args.after_delivery_id:
            continue
        if row.get("status") != "succeeded" or row.get("operation") != "candidate_created":
            continue
        if not str(row.get("nexus_candidate_id") or "").strip():
            continue
        if requested and int(row.get("candidate_id") or 0) not in requested:
            continue
        rows.append(row)
        if len(rows) >= args.limit:
            break
    return rows


def main() -> int:
    args = _args()
    counts: Counter[str] = Counter()
    details: list[dict[str, Any]] = []
    clients: dict[str, tuple[nexus_sync.NexusClient, dict[int, str], dict[int, str], set[int]]] = {}

    for delivery in _rows(args):
        counts["examined"] += 1
        local_id = int(delivery["candidate_id"])
        remote_id = str(delivery["nexus_candidate_id"])
        try:
            candidate = store.get_candidate(local_id)
            resume = store.get_resume(local_id, int(delivery["resume_id"]))
            if not candidate or not resume:
                counts["local_record_missing"] += 1
                continue
            settings = nexus_delivery._delivery_settings(
                str(delivery.get("requested_by") or "")
            )
            settings_key = json.dumps(
                {
                    "base_url": settings.base_url,
                    "org_code": settings.org_code,
                    "username": settings.username,
                    "auth_method": settings.auth_method,
                },
                sort_keys=True,
            )
            cached = clients.get(settings_key)
            if cached is None:
                client = nexus_sync.NexusClient(settings)
                profession_rows = client.get_master("professions")
                specialty_rows = client.get_master("specialties")
                profession_names = {
                    key: _name(row) for row in profession_rows
                    if (key := _master_id(row, "professions")) is not None
                }
                specialty_names = {
                    key: _name(row) for row in specialty_rows
                    if (key := _master_id(row, "specialties")) is not None
                }
                unknown_ids = {
                    key for key, value in specialty_names.items()
                    if value.casefold() in {"unknown", "unknown specialty"}
                }
                cached = (client, profession_names, specialty_names, unknown_ids)
                clients[settings_key] = cached
            client, profession_names, specialty_names, unknown_ids = cached

            payload = nexus_delivery._payload(delivery, candidate, resume)
            identity = nexus_sync._trusted_identity(payload)
            profile = nexus_sync._build_profile(client, identity, settings.default_profile)
            profession_id = int(profile["professionId"])
            specialty_id = int(profile["primarySpecialtyId"])
            if specialty_id in unknown_ids:
                counts["no_supported_specialty_evidence"] += 1
                continue

            current = client.get_candidate(remote_id)
            current_unknown = _is_unknown(current, unknown_ids)
            if current_unknown is False:
                counts["already_classified"] += 1
                continue
            if current_unknown is None:
                counts["current_classification_unreadable"] += 1
                continue

            item = {
                "delivery_id": int(delivery["id"]),
                "candidate_id": local_id,
                "nexus_candidate_id": remote_id,
                "profession_id": profession_id,
                "profession": profession_names.get(profession_id, ""),
                "specialty_id": specialty_id,
                "specialty": specialty_names.get(specialty_id, ""),
                "status": "eligible",
            }
            counts["eligible"] += 1
            if args.apply:
                client.update_candidate_classification(
                    remote_id,
                    profession_id=profession_id,
                    specialty_id=specialty_id,
                )
                verified = client.get_candidate(remote_id)
                if specialty_id not in _ids(verified):
                    raise RuntimeError("Nexus did not retain the requested specialty ID")
                item["status"] = "updated"
                counts["updated"] += 1
            details.append(item)
        except Exception as exc:  # continue the bounded repair and report safe type only
            counts["errors"] += 1
            details.append({
                "delivery_id": int(delivery.get("id") or 0),
                "candidate_id": local_id,
                "nexus_candidate_id": remote_id,
                "status": "error",
                "error_type": type(exc).__name__,
            })

    for client, _, _, _ in clients.values():
        client.close()

    print(json.dumps({
        "mode": "apply" if args.apply else "dry_run",
        "counts": dict(counts),
        "items": details,
    }, indent=2, sort_keys=True))
    return 1 if counts["errors"] else 0


if __name__ == "__main__":
    exit_code = 1
    try:
        exit_code = main()
    finally:
        pool = getattr(store, "_POSTGRES_POOL", None)
        if pool is not None:
            pool.close()
    raise SystemExit(exit_code)
