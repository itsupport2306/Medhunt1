"""Offline regressions for the PDL-to-Enformion fallback/cache boundary."""

from __future__ import annotations

import pytest

from sourcing import config, enformion_client as ef, multi_provider, store


@pytest.fixture(autouse=True)
def _isolated_store(monkeypatch, tmp_path):
    # config.py intentionally loads .env.local after process variables.  Pin
    # DB_PATH at the Python object boundary so a developer benchmark database
    # can never be touched by these tests.
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "fallback-cache-test.db"))
    monkeypatch.setattr(config, "DATABASE_URL", "")
    yield


def _enable_fallback(monkeypatch):
    monkeypatch.setattr(config, "ENFORMION_VERIFY_PDL", True)
    monkeypatch.setattr(config, "ENFORMION_FALLBACK_ONLY", True)
    monkeypatch.setattr(config, "ENFORMION_MAX_WORKERS", 1)
    monkeypatch.setattr(multi_provider, "enabled", lambda: True)


@pytest.mark.parametrize("result", [
    {
        "status": "error", "error": "Enformion HTTP 429: Rate Limit Exceeded",
        "http_status": 429,
    },
    {
        "status": "error", "error": "ReadTimeout: request timed out",
    },
    {
        "status": "error", "error": "Enformion HTTP 503: unavailable",
        "http_status": 503,
    },
    {
        "status": "error", "error": "Provider error while processing request",
    },
    # Be defensive if a transport adapter accidentally labels a transient
    # response as a no-match instead of an error.
    {
        "status": "no_match", "error": "Too many requests", "retryable": True,
    },
    {
        "status": "no_match", "error": "Relative resolution failed (ValueError)",
    },
])
def test_legacy_transient_provider_rows_are_never_replayed(result):
    request_key = "legacy-transient-" + str(abs(hash(repr(result))))
    store.save_provider_lookup(
        "enformion", "legacy_run", 1, request_key,
        str(result.get("status") or "error"), result, credits_spent=0,
    )

    assert multi_provider._cached(request_key) is None


def test_transient_failure_is_not_saved_and_next_lookup_runs_fresh(monkeypatch):
    _enable_fallback(monkeypatch)
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    candidate = store.get_candidate(candidate_id)
    request = multi_provider._request(candidate, "pdl_no_accepted_contact")
    calls = []
    responses = iter([
        {
            "status": "error", "source": "enformion", "emails": [],
            "phones": [], "addresses": [], "http_status": 429,
            "error": "Enformion HTTP 429: Rate Limit Exceeded",
            "credits_spent": 0,
        },
        {
            "status": "success", "source": "enformion",
            "matched_name": "Jane Doe", "provider_location": "Atlanta, GA",
            "addresses": ["Atlanta, GA"], "emails": ["jane@example.test"],
            "phones": ["(404) 555-0199"], "confidence": 0.95,
            "credits_spent": 1,
            "selection": {
                "selection": "unique_exact_name_location", "exact_candidates": 1,
            },
            "phone_evidence": [{
                "value": "(404) 555-0199", "type": "Wireless",
                "is_connected": True, "mobile_or_wireless": True,
                "accepted": True,
            }],
        },
    ])

    def fake_enrich(*args, **kwargs):
        calls.append((args, kwargs))
        return next(responses)

    monkeypatch.setattr(ef, "enrich", fake_enrich)
    pdl_no_match = {"status": "no_match", "emails": [], "phones": []}

    first = multi_provider.verify_batch(
        [candidate_id], {candidate_id: pdl_no_match}, "transient_first_12345678",
        allow_enformion=True, max_enformion_calls=1,
    )

    assert first["summary"]["called"] == 1
    assert store.get_provider_lookup("enformion", request["request_key"]) is None

    second = multi_provider.verify_batch(
        [candidate_id], {candidate_id: pdl_no_match}, "transient_second_12345678",
        allow_enformion=True, max_enformion_calls=1,
    )

    assert len(calls) == 2
    assert second["summary"]["called"] == 1
    assert second["summary"]["cached"] == 0
    assert second["results"][candidate_id]["status"] == "success"
    assert store.get_provider_lookup("enformion", request["request_key"])["status"] == "success"


def test_one_rate_limit_opens_run_circuit_instead_of_bursting_remaining_calls(
    monkeypatch,
):
    _enable_fallback(monkeypatch)
    candidate_ids = [
        store.add_candidate(name, location, source="indeed")
        for name, location in (
            ("Jane Doe", "Atlanta, GA"),
            ("John Smith", "Dallas, TX"),
            ("Mary Jones", "Miami, FL"),
        )
    ]
    calls = []

    def rate_limited(*_args, **_kwargs):
        calls.append(True)
        return {
            "status": "error", "source": "enformion", "emails": [],
            "phones": [], "addresses": [], "http_status": 429,
            "provider_error_code": "429", "retryable": True,
            "error": "Enformion HTTP 429: rate limited", "credits_spent": 0,
        }

    monkeypatch.setattr(ef, "enrich", rate_limited)
    checked = multi_provider.verify_batch(
        candidate_ids,
        {
            candidate_id: {"status": "no_match", "emails": [], "phones": []}
            for candidate_id in candidate_ids
        },
        "rate_limit_circuit_123", allow_enformion=True,
        max_enformion_calls=3,
    )

    assert calls == [True]
    assert checked["summary"]["called"] == 1
    assert checked["summary"]["deferred_rate_limit"] == 2
    assert all(
        result["status"] == "no_match"
        for result in checked["results"].values()
    )


def test_old_policy_success_is_revalidated_and_reused_without_paid_call(monkeypatch):
    _enable_fallback(monkeypatch)
    candidate_id = store.add_candidate(
        "Jane Doe", "Atlanta, GA", source="indeed",
    )
    historical = {
        "status": "success", "source": "enformion",
        "matched_name": "Jane Marie Doe", "provider_location": "Atlanta, GA",
        "addresses": ["Atlanta, GA"], "emails": ["jane@example.test"],
        "phones": ["(404) 555-0199"], "confidence": 0.95,
        "selection": {
            "selection": "unique_exact_name_location", "exact_candidates": 1,
            "winner_margin": 1.0,
        },
        "phone_evidence": [{
            "value": "(404) 555-0199", "type": "Wireless",
            "is_connected": True, "mobile_or_wireless": True,
            "accepted": True,
        }],
    }
    store.save_provider_lookup(
        "enformion", "old_policy_run", candidate_id, "old-policy-key",
        "success", historical, credits_spent=1,
    )
    monkeypatch.setattr(
        ef, "enrich",
        lambda *_args, **_kwargs: pytest.fail("validated history must be reused"),
    )

    checked = multi_provider.verify_batch(
        [candidate_id],
        {candidate_id: {"status": "no_match", "emails": [], "phones": []}},
        "cache_migration_12345678", allow_enformion=True,
        max_enformion_calls=1,
    )

    result = checked["results"][candidate_id]
    assert checked["summary"]["called"] == 0
    assert checked["summary"]["cached"] == 1
    assert result["status"] == "success"
    assert result["emails"] == ["jane@example.test"]
    assert result["phones"] == ["(404) 555-0199"]
    assert result["enformion"]["status"] == "success"


def test_old_policy_result_is_not_reused_after_identity_changes(monkeypatch):
    _enable_fallback(monkeypatch)
    candidate_id = store.add_candidate(
        "Jane Doe", "Atlanta, GA", source="indeed",
    )
    historical = {
        "status": "success", "source": "enformion",
        "matched_name": "Jane Doe", "provider_location": "Atlanta, GA",
        "addresses": ["Atlanta, GA"], "emails": ["jane@example.test"],
        "phones": [], "confidence": 0.95,
        "selection": {
            "selection": "unique_exact_name_location", "exact_candidates": 1,
        },
    }
    store.save_provider_lookup(
        "enformion", "old_policy_run", candidate_id, "old-policy-key",
        "success", historical, credits_spent=1,
    )
    store.update_candidate(candidate_id, name="Jane Smith", canonical_name="")
    calls = []

    def fresh_no_match(*_args, **_kwargs):
        calls.append(True)
        return {
            "status": "no_match", "source": "enformion", "emails": [],
            "phones": [], "addresses": [], "credits_spent": 1,
        }

    monkeypatch.setattr(ef, "enrich", fresh_no_match)
    checked = multi_provider.verify_batch(
        [candidate_id],
        {candidate_id: {"status": "no_match", "emails": [], "phones": []}},
        "cache_identity_change_123", allow_enformion=True,
        max_enformion_calls=1,
    )

    assert calls == [True]
    assert checked["summary"]["cached"] == 0
    assert checked["summary"]["called"] == 1


def test_provider_rejection_still_runs_independent_fallback(monkeypatch):
    _enable_fallback(monkeypatch)
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    calls = []

    def fake_enrich(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "status": "no_match", "source": "enformion", "emails": [],
            "phones": [], "addresses": [], "credits_spent": 0,
        }

    monkeypatch.setattr(ef, "enrich", fake_enrich)
    provider_rejected = {
        "status": "rejected", "emails": [], "phones": [],
        "verification": {
            "identity_status": "rejected",
            "identity_provider": "people_data_labs",
        },
    }

    checked = multi_provider.verify_batch(
        [candidate_id], {candidate_id: provider_rejected},
        "provider_rejected_12345678", allow_enformion=True,
        max_enformion_calls=1,
    )

    assert len(calls) == 1
    assert checked["summary"]["called"] == 1
    assert multi_provider._fallback_reason(provider_rejected) == "pdl_identity_conflict"


def test_explicit_recruiter_rejection_remains_terminal(monkeypatch):
    _enable_fallback(monkeypatch)
    candidate_id = store.add_candidate("Jane Doe", "Atlanta, GA", source="indeed")
    store.update_candidate(
        candidate_id, identity_status="rejected", identity_provider="recruiter_review",
    )
    monkeypatch.setattr(
        ef, "enrich", lambda *args, **kwargs: pytest.fail("provider must not be called"),
    )

    checked = multi_provider.verify_batch(
        [candidate_id],
        {candidate_id: {"status": "no_match", "emails": [], "phones": []}},
        "recruiter_rejected_12345678", allow_enformion=True,
        max_enformion_calls=1,
    )

    assert checked["summary"]["called"] == 0
    assert checked["summary"]["eligible"] == 0
    assert multi_provider._fallback_reason({
        "status": "rejected",
        "verification": {
            "identity_status": "rejected",
            "identity_provider": "recruiter_review",
        },
    }) == ""
