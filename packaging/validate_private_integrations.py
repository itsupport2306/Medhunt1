"""Read-only readiness checks for Medhunt's private backend integrations.

The script intentionally prints only boolean/status information. It never
creates a candidate, uploads an object, or emits endpoint/credential values.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src_pkg"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from sourcing import config, nexus_sync, storage


def database_ready() -> bool:
    if config.DATABASE_BACKEND == "sqlite" or not config.DATABASE_URL:
        return False
    try:
        import psycopg

        with psycopg.connect(
            config.DATABASE_URL,
            connect_timeout=config.DATABASE_CONNECT_TIMEOUT,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                return cursor.fetchone() == (1,)
    except Exception:
        return False


def storage_ready() -> bool:
    if not storage.enabled():
        return False
    try:
        settings = storage._settings()
        storage._client().head_bucket(Bucket=settings["bucket"])
        return True
    except Exception:
        return False


def nexus_ready() -> bool:
    if not config.NEXUS_SYNC_ENABLED:
        return False
    client = nexus_sync.NexusClient(nexus_sync.NexusSettings.from_config(config))
    try:
        # Authenticates and reads tenant master data, but performs no candidate
        # search/create/update and no resume upload.
        return isinstance(client.get_master("professions"), list)
    except nexus_sync.NexusDeliveryError as exc:
        print(f"nexus_error_category={exc.category}")
        print(f"nexus_error_operation={exc.operation}")
        print(f"nexus_error_status={exc.status_code or 0}")
        return False
    except Exception as exc:
        print(f"nexus_error_type={type(exc).__name__}")
        return False
    finally:
        client.close()


def reference_nexus_ready(project_root: Path) -> bool:
    """Run the same read-only check through the original Nexus app client."""
    root = project_root.expanduser().resolve(strict=True)
    sys.path.insert(0, str(root))
    try:
        from app.nexus_client import NexusClient as ReferenceNexusClient

        client = ReferenceNexusClient()
        try:
            return isinstance(client.get_master("professions"), (list, dict))
        except Exception as exc:
            body = str(getattr(exc, "body", "") or "").casefold()
            if "invalid_client" in body:
                reason = "invalid_oauth_client"
            elif "organization code" in body or "organizationcode" in body:
                reason = "organization_code_rejected"
            elif "invalid_grant" in body or "bad credentials" in body:
                reason = "api_user_rejected"
            else:
                reason = "request_rejected"
            print(f"reference_nexus_error_type={type(exc).__name__}")
            print(
                "reference_nexus_error_status="
                + str(int(getattr(exc, "status_code", 0) or 0))
            )
            print(f"reference_nexus_error_reason={reason}")
            return False
        finally:
            client._http.close()
    finally:
        if sys.path and sys.path[0] == str(root):
            sys.path.pop(0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", action="store_true")
    parser.add_argument("--storage", action="store_true")
    parser.add_argument("--nexus", action="store_true")
    parser.add_argument("--reference-nexus", type=Path)
    args = parser.parse_args()
    checks = {
        "database": database_ready,
        "storage": storage_ready,
        "nexus": nexus_ready,
    }
    selected = [name for name in checks if getattr(args, name)]
    if not selected and args.reference_nexus is None:
        selected = list(checks)
    results = {name: checks[name]() for name in selected}
    if args.reference_nexus is not None:
        results["reference_nexus"] = reference_nexus_ready(args.reference_nexus)
    for name, ready in results.items():
        print(f"{name}_ready={str(ready).lower()}")
    if not all(results.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
