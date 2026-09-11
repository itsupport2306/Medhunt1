"""Offline coverage-accounting regressions; no provider calls are made."""
from __future__ import annotations

import pytest

from sourcing import config, lookup_metrics, store


@pytest.fixture(autouse=True)
def _isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lookup-metrics.db"))
    monkeypatch.setattr(config, "DATABASE_URL", "")
    yield


def _candidate(candidate_id, *, source_id="one"):
    return {
        "id": candidate_id, "name": "Jane Doe", "location": "Atlanta, GA",
        "source": "indeed", "source_id": source_id,
        "source_url": f"https://indeed.example/candidate/{source_id}",
    }


def test_cohort_deduplicates_only_exact_source_identity():
    candidates = {
        1: _candidate(1, source_id="same"),
        2: _candidate(2, source_id="same"),
        3: _candidate(3, source_id="different"),
    }
    cohort = lookup_metrics.freeze_cohort(
        [1, 2, 3], candidates.get,
    )

    assert cohort["selected_count"] == 3
    assert cohort["unique_count"] == 2
    assert cohort["eligible_count"] == 2
    assert [item["candidate_id"] for item in cohort["items"]] == [1, 3]


def test_review_rate_limit_and_missing_results_stay_in_denominator():
    candidates = {index: _candidate(index, source_id=str(index)) for index in range(1, 5)}
    cohort = lookup_metrics.freeze_cohort([1, 2, 3, 4], candidates.get)
    items = cohort["items"]
    trusted = {
        "status": "success", "contacts_trusted": True,
        "emails": ["hidden@example.test"], "phones": [],
    }
    classified = [
        lookup_metrics.classify(
            items[0], trusted,
            {"status": "found", "emails": ["hidden@example.test"]},
        ),
        lookup_metrics.classify(
            items[1], {"status": "review", "emails": ["hidden@example.test"]},
            {"status": "not_found"},
        ),
        lookup_metrics.classify(
            items[2], {
                "status": "error", "error": "Enformion HTTP 429 rate limit",
            }, {"status": "failed"},
        ),
        lookup_metrics.classify(items[3], None, None),
    ]
    summary = lookup_metrics.summarize(classified)

    assert summary["eligible_count"] == 4
    assert summary["found_count"] == 1
    assert summary["found_rate"] == 0.25
    assert summary["target_met"] is False
    assert summary["outcomes"] == {
        "backend_missing_result": 1,
        "contact_bearing_review": 1,
        "provider_rate_limited": 1,
        "trusted_found": 1,
    }


def test_run_events_are_append_only_and_store_no_contact_values():
    candidate_id = store.add_candidate(
        "Jane Doe", "Atlanta, GA", source="indeed", source_id="jane-one",
    )
    cohort = lookup_metrics.freeze_cohort([candidate_id], store.get_candidate)
    store.begin_lookup_run("immutable_run_123", cohort)
    item = lookup_metrics.classify(
        cohort["items"][0],
        {
            "status": "success", "contacts_trusted": True,
            "emails": ["private@example.test"], "phones": ["(404) 555-0199"],
            "trusted_cache": True,
        },
        {"status": "found", "emails": ["private@example.test"]},
    )
    store.finish_lookup_run("immutable_run_123", [item])
    # A replay with the same run ID cannot rewrite the frozen outcome.
    store.finish_lookup_run("immutable_run_123", [{
        **item, "outcome": "provider_error", "found": False,
    }])

    stored = store.lookup_run_items("immutable_run_123")

    assert len(stored) == 1
    assert stored[0]["outcome"] == "trusted_found_cache"
    assert stored[0]["found"] is True
    assert "private@example.test" not in repr(stored)
    assert "404" not in repr(stored)
