"""Safely consolidate resume objects into one Cloudflare R2 bucket.

The migration is intentionally copy-first and non-destructive:

1. Read each resume row still pointing at the source bucket.
2. Copy its object to the destination bucket using the same key.
3. Verify destination size and SHA-256 metadata when available.
4. Update that Neon row only after verification succeeds.

Source objects are retained as a rollback copy. Running the command again is
safe: verified destination objects are reused and already-migrated rows are not
selected.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import sys
from urllib.parse import quote, urlsplit, urlunsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import dotenv_values
import psycopg
from psycopg.rows import dict_row


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def merged_settings(source: Path, override: Path) -> dict[str, str]:
    values = dict(dotenv_values(source.resolve(strict=True)))
    if override.exists():
        values.update(dotenv_values(override.resolve(strict=True)))
    return {key: str(value or "").strip() for key, value in values.items()}


def database_url(settings: dict[str, str]) -> str:
    value = settings.get("DATABASE_URL", "")
    name = settings.get("DATABASE_NAME", "")
    if value and name:
        parsed = urlsplit(value)
        value = urlunsplit(parsed._replace(path=f"/{quote(name)}"))
    if not value:
        raise RuntimeError("DATABASE_URL is not configured.")
    return value


def r2_client(settings: dict[str, str]):
    required = (
        "S3_ENDPOINT_URL", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_REGION",
    )
    missing = [key for key in required if not settings.get(key)]
    if missing:
        raise RuntimeError("Missing R2 settings: " + ", ".join(missing))
    return boto3.client(
        "s3",
        endpoint_url=settings["S3_ENDPOINT_URL"],
        aws_access_key_id=settings["S3_ACCESS_KEY"],
        aws_secret_access_key=settings["S3_SECRET_KEY"],
        region_name=settings["S3_REGION"],
        config=Config(
            connect_timeout=10,
            read_timeout=60,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def object_metadata(client, bucket: str, key: str) -> dict | None:
    try:
        response = client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    return {
        "size": int(response.get("ContentLength") or 0),
        "etag": str(response.get("ETag") or "").strip('"'),
        "sha256": str((response.get("Metadata") or {}).get("sha256") or ""),
    }


def metadata_matches(source: dict, destination: dict) -> bool:
    if source["size"] != destination["size"]:
        return False
    source_sha = source.get("sha256", "")
    destination_sha = destination.get("sha256", "")
    if source_sha and destination_sha and source_sha != destination_sha:
        return False
    return True


def migrate_object(client, row: dict, destination_bucket: str) -> dict:
    resume_id = int(row["id"])
    source_bucket = str(row["bucket"])
    key = str(row["object_key"])
    if not key:
        return {"id": resume_id, "status": "error", "error": "empty object key"}

    source = object_metadata(client, source_bucket, key)
    if source is None:
        return {"id": resume_id, "status": "error", "error": "source object missing"}

    destination = object_metadata(client, destination_bucket, key)
    if destination is not None and not metadata_matches(source, destination):
        return {
            "id": resume_id,
            "status": "error",
            "error": "destination key contains a different object",
        }

    copied = destination is None
    if copied:
        client.copy_object(
            Bucket=destination_bucket,
            Key=key,
            CopySource={"Bucket": source_bucket, "Key": key},
            MetadataDirective="COPY",
        )
        destination = object_metadata(client, destination_bucket, key)
        if destination is None or not metadata_matches(source, destination):
            return {
                "id": resume_id,
                "status": "error",
                "error": "destination verification failed after copy",
            }

    return {
        "id": resume_id,
        "status": "copied" if copied else "already_present",
        "key": key,
    }


def migrate(
    settings: dict[str, str],
    source_bucket: str,
    destination_bucket: str,
    workers: int,
    apply_changes: bool,
) -> dict:
    if not source_bucket or not destination_bucket:
        raise ValueError("Both source and destination buckets are required.")
    if source_bucket == destination_bucket:
        raise ValueError("Source and destination buckets must be different.")

    connection_url = database_url(settings)
    client = r2_client(settings)
    client.head_bucket(Bucket=source_bucket)
    client.head_bucket(Bucket=destination_bucket)

    with psycopg.connect(
        connection_url,
        row_factory=dict_row,
        autocommit=True,
        connect_timeout=10,
    ) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """SELECT id,bucket,object_key,size,etag,checksum_sha256
                   FROM resumes
                   WHERE bucket=%s
                   ORDER BY id""",
                (source_bucket,),
            ).fetchall()
        ]
        if not apply_changes:
            return {
                "mode": "dry_run",
                "source_bucket": source_bucket,
                "destination_bucket": destination_bucket,
                "eligible_rows": len(rows),
            }

        outcomes = {
            "copied": 0,
            "already_present": 0,
            "updated_rows": 0,
            "errors": 0,
        }
        failures: list[dict] = []
        public_base = settings.get("S3_PUBLIC_BASE_URL", "").rstrip("/")
        with ThreadPoolExecutor(max_workers=max(1, min(32, workers))) as pool:
            futures = {
                pool.submit(migrate_object, client, row, destination_bucket): row
                for row in rows
            }
            for future in as_completed(futures):
                row = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # keep other independent rows moving
                    result = {
                        "id": int(row["id"]),
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                status = str(result.get("status") or "error")
                if status == "error":
                    outcomes["errors"] += 1
                    if len(failures) < 25:
                        failures.append(result)
                    continue

                outcomes[status] += 1
                key = str(result["key"])
                public_url = f"{public_base}/{quote(key, safe='/')}" if public_base else ""
                updated = connection.execute(
                    """UPDATE resumes
                       SET bucket=%s, public_url=%s
                       WHERE id=%s AND bucket=%s""",
                    (destination_bucket, public_url, int(result["id"]), source_bucket),
                ).rowcount
                outcomes["updated_rows"] += int(updated or 0)

        remaining = int(
            connection.execute(
                "SELECT COUNT(*) AS count FROM resumes WHERE bucket=%s",
                (source_bucket,),
            ).fetchone()["count"]
        )
        return {
            "mode": "apply",
            "source_bucket": source_bucket,
            "destination_bucket": destination_bucket,
            "eligible_rows": len(rows),
            **outcomes,
            "remaining_source_rows": remaining,
            "failures": failures,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-env", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--override-env", type=Path, default=PROJECT_ROOT / ".env.local")
    parser.add_argument("--source-bucket", required=True)
    parser.add_argument("--destination-bucket", required=True)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    try:
        result = migrate(
            merged_settings(args.source_env, args.override_env),
            args.source_bucket.strip(),
            args.destination_bucket.strip(),
            args.workers,
            args.apply,
        )
    except Exception as exc:
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result, indent=2))
    return 1 if int(result.get("errors") or 0) else 0


if __name__ == "__main__":
    sys.exit(main())
