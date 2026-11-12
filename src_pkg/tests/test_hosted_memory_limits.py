"""Hosted web instances must not allocate the full upstream API capacity."""

import json
import os
import subprocess
import sys
from pathlib import Path


def _limits(
    render: str, *, requested: str = "80", cap: str = "8", cpu: str = "0.5",
) -> dict:
    env = os.environ.copy()
    env.update({
        "RENDER": render,
        "RENDER_CPU_COUNT": cpu,
        "CONTACT_LOOKUP_MAX_CONCURRENT": requested,
        "CONTACT_LOOKUP_MEMORY_SAFE_CAP": cap,
        "CONTACT_LOOKUP_BACKFILL_MAX_CONCURRENT": "10",
    })
    result = subprocess.run(
        [sys.executable, "-c", (
            "import json; from sourcing import config; "
            "print(json.dumps([config.CONTACT_LOOKUP_MAX_CONCURRENT, "
            "config.CONTACT_LOOKUP_BACKFILL_MAX_CONCURRENT]))"
        )],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    workers, backfill = json.loads(result.stdout.strip())
    return {"workers": workers, "backfill": backfill}


def test_render_caps_80_configured_workers_for_small_instance():
    assert _limits("true") == {"workers": 8, "backfill": 2}
    assert _limits("true", requested="4") == {"workers": 4, "backfill": 1}


def test_local_worker_capacity_and_hosted_override():
    assert _limits("false") == {"workers": 80, "backfill": 10}
    assert _limits("true", cap="16") == {"workers": 16, "backfill": 4}
    assert _limits("true", cpu="2") == {"workers": 80, "backfill": 10}
