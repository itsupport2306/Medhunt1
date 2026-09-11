"""Create a minimal Medhunt lookup configuration from a private .env.

Only explicitly allowlisted lookup settings are copied.  The generated file
may be used for local administration or embedded in a trusted-team installer;
credential values are never printed by this helper.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src_pkg"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from sourcing.nexus_reference import load_reference as load_nexus_reference


PASSTHROUGH = (
    "PDL_API_KEY",
    "PDL_BASE_URL",
    "PDL_IDENTIFY_URL",
    "PDL_SEARCH_URL",
    "PDL_TIMEOUT",
    "PDL_BULK_TIMEOUT",
    "PDL_BULK_MAX",
    "PDL_MIN_LIKELIHOOD",
    "PDL_AUTO_ACCEPT_MIN_LIKELIHOOD",
    "PDL_PROFILE_ACCEPT_MIN_LIKELIHOOD",
    "PDL_STAGED_RETRY_ENABLED",
    "PDL_STAGED_RETRY_MAX",
    "PDL_RUN_CREDIT_LIMIT",
    "PDL_CACHE_TTL_SECONDS",
    "ENFORMION_URL",
    "ENFORMION_SEARCH_TYPE",
    "ENFORMION_HTTP_TIMEOUT",
    "ENFORMION_VERIFY_PDL",
    "ENFORMION_FALLBACK_ONLY",
    "ENFORMION_CACHE_TTL_SECONDS",
    "ENFORMION_MAX_WORKERS",
    "ENFORMION_RUN_CREDIT_LIMIT",
    "QUICK_SOURCER_BASE_URL",
    "QUICK_SOURCER_TIMEOUT",
    "QUICK_SOURCER_CACHE_TTL_SECONDS",
    "QUICK_SOURCER_TRUSTED_FOR_SYNC",
    "CONTACT_LOOKUP_PROVIDER",
    "DATABASE_BACKEND",
    "DATABASE_NAME",
    "DATABASE_URL",
    "DATABASE_CONNECT_TIMEOUT",
    "STORAGE_ENABLED",
    "S3_ENDPOINT_URL",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "S3_BUCKET",
    "S3_REGION",
    "S3_PUBLIC_BASE_URL",
    "WATCHER_EMAIL_NOTIFICATIONS_ENABLED",
    "WATCHER_NOTIFICATION_EMAILS",
    "SENDGRID_API_KEY",
    "EMAIL_FROM",
    "EMAIL_FROM_NAME",
    "SENDGRID_TIMEOUT",
    "NEXUS_SYNC_ENABLED",
    "NEXUS_BASE_URL",
    "NEXUS_AUTH_METHOD",
    "NEXUS_TOKEN_URL",
    "NEXUS_TOKEN_PAYLOAD_STYLE",
    "NEXUS_CLIENT_ID",
    "NEXUS_CLIENT_SECRET",
    "NEXUS_USERNAME",
    "NEXUS_PASSWORD",
    "NEXUS_STATIC_TOKEN",
    "NEXUS_TOKEN_BASIC",
    "NEXUS_ORG_CODE",
    "NEXUS_RESUME_DOC_TYPE_ID",
    "NEXUS_DEFAULT_PROFILE",
    "NEXUS_TIMEOUT",
    "NEXUS_CONNECT_TIMEOUT",
    "NEXUS_MASTER_CACHE_SECONDS",
    "NEXUS_MAX_RESUME_BYTES",
    "NEXUS_WORKER_INTERVAL_SECONDS",
    "NEXUS_MAX_ATTEMPTS",
    "NEXUS_LEASE_SECONDS",
)


def first(values: dict[str, str | None], *names: str) -> str:
    for name in names:
        value = str(values.get(name) or "").strip()
        if value:
            return value
    return ""


def dotenv_line(key: str, value: str) -> str:
    """Serialize one value without allowing it to inject another setting."""
    if any(character in value for character in ("\r", "\n", "\x00")):
        raise ValueError(f"Unsupported control character in {key}")
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"{key}='{escaped}'"


def _merged_values(source: Path, override: Path | None = None) -> dict[str, str | None]:
    source = source.resolve(strict=True)
    values: dict[str, str | None] = dict(dotenv_values(source))
    if override is not None and override.exists():
        values.update(dotenv_values(override.resolve(strict=True)))

    reference = load_nexus_reference(
        first(values, "NEXUS_REFERENCE_ENV"),
        first(values, "NEXUS_REFERENCE_CONFIG"),
    )
    for key, value in reference.items():
        if not first(values, key):
            values[key] = value
    return values


def nexus_ready(values: dict[str, str | None]) -> bool:
    if first(values, "NEXUS_SYNC_ENABLED").casefold() not in {"1", "true", "yes"}:
        return False
    method = (first(values, "NEXUS_AUTH_METHOD") or "static").casefold()
    has_client = bool(
        first(values, "NEXUS_TOKEN_BASIC")
        or (
            first(values, "NEXUS_CLIENT_ID")
            and first(values, "NEXUS_CLIENT_SECRET")
        )
    )
    if method == "static":
        auth_ready = bool(first(values, "NEXUS_STATIC_TOKEN"))
    elif method == "password":
        auth_ready = bool(
            first(values, "NEXUS_TOKEN_URL")
            and first(values, "NEXUS_USERNAME")
            and first(values, "NEXUS_PASSWORD")
            and has_client
        )
    elif method == "client_credentials":
        auth_ready = bool(first(values, "NEXUS_TOKEN_URL") and has_client)
    else:
        return False
    try:
        profile = json.loads(first(values, "NEXUS_DEFAULT_PROFILE") or "{}")
    except ValueError:
        return False
    return bool(
        auth_ready
        and first(values, "NEXUS_BASE_URL")
        and isinstance(profile, dict)
        and profile.get("jobTypeIds")
        and (profile.get("referralSourceId") or profile.get("referralSourceName"))
    )


def cloud_ready(values: dict[str, str | None]) -> bool:
    return bool(
        first(values, "DATABASE_BACKEND").casefold() != "sqlite"
        and first(values, "DATABASE_URL")
        and first(values, "STORAGE_ENABLED").casefold() in {"1", "true", "yes"}
        and first(values, "S3_ENDPOINT_URL")
        and first(values, "S3_ACCESS_KEY")
        and first(values, "S3_SECRET_KEY")
        and first(values, "S3_BUCKET")
    )


def provision(
    source: Path,
    destination: Path,
    override: Path | None = None,
) -> tuple[bool, bool, bool]:
    values = _merged_values(source, override)
    output: dict[str, str] = {
        key: str(values.get(key) or "").strip()
        for key in PASSTHROUGH
        if str(values.get(key) or "").strip()
    }
    output["DATABASE_BACKEND"] = (
        first(values, "DATABASE_BACKEND")
        or ("postgresql" if first(values, "DATABASE_URL") else "sqlite")
    )

    primary = first(values, "PDL_API_KEY")
    secondary_name = first(values, "ENFORMION_AP_NAME", "ENFORMION_PROFILE_NAME", "Profile_name")
    secondary_password = first(values, "ENFORMION_AP_PASSWORD", "ENFORMION_PASSWORD", "Password")
    if primary:
        output["PDL_API_KEY"] = primary
        output["PDL_ENABLED"] = "1"
    if secondary_name and secondary_password:
        output["ENFORMION_AP_NAME"] = secondary_name
        output["ENFORMION_AP_PASSWORD"] = secondary_password
        output["ENFORMION_ENABLED"] = "1"

    # Quick Sourcer answers candidate lookups in the packaged application, so
    # its key ships with the same baseline the administrator rotates.
    quick_sourcer = first(values, "QUICK_SOURCER_API_KEY")
    if quick_sourcer:
        output["QUICK_SOURCER_API_KEY"] = quick_sourcer
        output["QUICK_SOURCER_ENABLED"] = "1"
        output.setdefault("CONTACT_LOOKUP_PROVIDER", "quick_sourcer")

    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    body = [
        "# Managed by Medhunt Setup. Do not edit this packaged baseline.",
        "# Use .env.local for administrator overrides.",
        *(dotenv_line(key, value) for key, value in sorted(output.items())),
        "",
    ]
    handle, temporary_name = tempfile.mkstemp(
        prefix=".env.local.", suffix=".tmp", dir=destination.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(body))
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return (
        bool(primary),
        bool(secondary_name and secondary_password),
        bool(quick_sourcer),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--override", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--require-primary", action="store_true")
    parser.add_argument("--require-secondary", action="store_true")
    parser.add_argument("--require-quick-sourcer", action="store_true")
    parser.add_argument("--require-nexus", action="store_true")
    parser.add_argument("--require-cloud", action="store_true")
    args = parser.parse_args()
    primary, secondary, quick_sourcer = provision(
        args.source, args.destination, args.override,
    )
    if args.require_primary and not primary:
        args.destination.unlink(missing_ok=True)
        raise SystemExit("Required primary lookup configuration is missing.")
    if args.require_secondary and not secondary:
        args.destination.unlink(missing_ok=True)
        raise SystemExit("Required secondary lookup configuration is missing.")
    if args.require_quick_sourcer and not quick_sourcer:
        args.destination.unlink(missing_ok=True)
        raise SystemExit("Required Quick Sourcer configuration is missing.")
    generated = dict(dotenv_values(args.destination))
    nexus = nexus_ready(generated)
    cloud = cloud_ready(generated)
    if args.require_nexus and not nexus:
        args.destination.unlink(missing_ok=True)
        raise SystemExit("Required Nexus synchronization configuration is incomplete.")
    if args.require_cloud and not cloud:
        args.destination.unlink(missing_ok=True)
        raise SystemExit("Required Neon/R2 configuration is incomplete.")
    print(f"configured_primary={str(primary).lower()}")
    print(f"configured_secondary={str(secondary).lower()}")
    print(f"configured_quick_sourcer={str(quick_sourcer).lower()}")
    print(f"configured_nexus={str(nexus).lower()}")
    print(f"configured_cloud={str(cloud).lower()}")


if __name__ == "__main__":
    main()
