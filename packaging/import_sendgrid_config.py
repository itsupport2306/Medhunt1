"""Import an existing SendGrid setup into Medhunt without printing secrets."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import tempfile

from dotenv import dotenv_values


TARGET_KEYS = (
    "WATCHER_EMAIL_NOTIFICATIONS_ENABLED",
    "WATCHER_NOTIFICATION_EMAILS",
    "SENDGRID_API_KEY",
    "EMAIL_FROM",
    "EMAIL_FROM_NAME",
    "SENDGRID_TIMEOUT",
)
_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _value(values: dict, *names: str) -> str:
    for name in names:
        value = str(values.get(name) or "").strip()
        if value:
            return value
    return ""


def _dotenv_line(key: str, value: str) -> str:
    if any(character in value for character in ("\r", "\n", "\x00")):
        raise ValueError(f"Unsupported control character in {key}")
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"{key}='{escaped}'"


def import_config(source: Path, destination: Path) -> int:
    values = dict(dotenv_values(source.resolve(strict=True)))
    api_key = _value(values, "SENDGRID_API_KEY")
    sender = _value(values, "EMAIL_FROM")
    recipients = _value(
        values,
        "WATCHER_NOTIFICATION_EMAILS",
        "RECRUITER_NOTIFICATION_EMAILS",
        "CEIPAL_EMAIL",
    )
    recipient_list = [
        email.strip().casefold() for email in re.split(r"[,;]", recipients) if email.strip()
    ]
    if not api_key:
        raise RuntimeError("The source configuration has no SENDGRID_API_KEY.")
    if not _EMAIL.fullmatch(sender):
        raise RuntimeError("The source EMAIL_FROM is missing or invalid.")
    if not recipient_list or any(not _EMAIL.fullmatch(email) for email in recipient_list):
        raise RuntimeError("The source recruiter notification email is missing or invalid.")

    imported = {
        "WATCHER_EMAIL_NOTIFICATIONS_ENABLED": "1",
        "WATCHER_NOTIFICATION_EMAILS": ",".join(dict.fromkeys(recipient_list)),
        "SENDGRID_API_KEY": api_key,
        "EMAIL_FROM": sender,
        "EMAIL_FROM_NAME": _value(values, "EMAIL_FROM_NAME") or "Medhunt",
        "SENDGRID_TIMEOUT": _value(values, "SENDGRID_TIMEOUT") or "15",
    }
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    body = destination.read_text(encoding="utf-8") if destination.exists() else ""
    retained = []
    for line in body.splitlines():
        match = _ASSIGNMENT.match(line)
        if match and match.group(1).upper() in TARGET_KEYS:
            continue
        retained.append(line)
    while retained and not retained[-1].strip():
        retained.pop()
    retained.extend([
        "",
        "# SendGrid watcher alerts imported by packaging/import_sendgrid_config.py.",
        *(_dotenv_line(key, imported[key]) for key in TARGET_KEYS),
        "",
    ])

    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(retained))
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return len(recipient_list)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    recipient_count = import_config(args.source, args.destination)
    print("configured_sendgrid=true")
    print(f"configured_recipients={recipient_count}")


if __name__ == "__main__":
    main()
