"""No-network regressions for PDL's one-combined-request policy."""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest


os.environ["SOURCING_DB"] = os.path.join(
    tempfile.mkdtemp(), "pdl-combined-request-test.db",
)
os.environ["DATABASE_URL"] = ""
os.environ["STORAGE_ENABLED"] = "0"
os.environ["PDL_API_KEY"] = ""
os.environ["PDL_ENABLED"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api as api_module  # noqa: E402
from sourcing import config, pdl_client, store  # noqa: E402


class FakeResponse:
    def __init__(self, status_code: int, payload=None, credits=0):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = {"X-Call-Credits-Spent": str(credits)}
        self.content = json.dumps(self._payload).encode("utf-8")

    def json(self):
        return self._payload


def _person(name="Jane Doe", location="Atlanta, Georgia", phone="+14045550121"):
    first, last = name.split(" ", 1)
    return {
        "id": f"pdl-{first.casefold()}-{last.casefold().replace(' ', '-')}",
        "full_name": name,
        "first_name": first,
        "last_name": last,
        "location_name": location,
        "mobile_phone": phone,
        "recommended_personal_email": (
            f"{first}.{last.replace(' ', '.')}@example.test".casefold()
        ),
    }


def _payload(name="Jane Doe", location="Atlanta, Georgia", matched=None):
    return {
        "data": _person(name, location),
        "likelihood": 9,
        "matched": list(matched or ["name", "location"]),
    }


def _candidate(name="Jane Doe", *, notes="Employer: Stale Health"):
    return {
        "name": name,
        "location": "Atlanta, GA",
        "notes": notes,
        "source": "indeed",
        "source_url": "",
    }


def _enable_pdl(monkeypatch, *, run_limit=20):
    monkeypatch.setattr(config, "PDL_API_KEY", "unit-test-key")
    monkeypatch.setattr(config, "PDL_ENABLED", True)
    monkeypatch.setattr(config, "PDL_RUN_CREDIT_LIMIT", run_limit)
    monkeypatch.setattr(config, "PDL_CACHE_TTL_SECONDS", 3600)


@pytest.fixture(autouse=True)
def _isolated_store():
    store.reset()
    yield
    store.reset()


def test_query_plan_sends_every_available_field_in_one_entry(monkeypatch):
    _enable_pdl(monkeypatch)
    candidate = {
        **_candidate(notes=(
            "Role: Registered Nurse\n"
            "Employer: North Okaloosa Medical Center\n"
            "Employer: North Walton Doctors Hospital\n"
            "School: Nevada State University"
        )),
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/in/jane-doe-rn/",
    }

    plan = pdl_client._query_attempts(candidate)

    assert len(plan) == 1
    assert plan[0]["strategy"] == "combined_context"
    names, location, companies, schools, profile = plan[0]["identity"]
    assert names == "Jane Doe"
    assert location == "Atlanta, GA"
    assert companies == [
        "North Okaloosa Medical Center", "North Walton Doctors Hospital",
    ]
    assert schools == ["Nevada State University"]
    assert profile == "https://www.linkedin.com/in/jane-doe-rn"


def test_old_staged_settings_cannot_split_a_candidate_request(monkeypatch):
    _enable_pdl(monkeypatch)
    monkeypatch.setattr(config, "PDL_STAGED_RETRY_ENABLED", True)
    monkeypatch.setattr(config, "PDL_STAGED_RETRY_MAX", 6)

    plan = pdl_client._query_attempts(_candidate(notes=(
        "Employer: Emory Healthcare\nSchool: Emory University"
    )))

    assert len(plan) == 1
    identity = plan[0]["identity"]
    assert identity[1:] == (
        "Atlanta, GA", ["Emory Healthcare"], ["Emory University"],
    )


def test_bulk_payload_contains_all_candidate_evidence_in_one_request(monkeypatch):
    _enable_pdl(monkeypatch)
    monkeypatch.setattr(config, "PDL_MIN_LIKELIHOOD", 2)
    captured = {}

    class Client:
        def post(self, url, *, json, headers, timeout):
            captured.update({"url": url, "json": json, "headers": headers})
            return FakeResponse(200, [{"status": 404}], credits=0)

    monkeypatch.setattr(pdl_client, "_client", lambda: Client())
    candidate = {
        **_candidate(notes=(
            "Employer: North Okaloosa Medical Center\n"
            "Employer: North Walton Doctors Hospital\n"
            "School: Nevada State University"
        )),
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/in/jane-doe-rn/",
    }
    identity = pdl_client._query_attempts(candidate)[0]["identity"]

    pdl_client._bulk_call([identity])

    assert len(captured["json"]["requests"]) == 1
    params = captured["json"]["requests"][0]["params"]
    assert params == {
        "name": ["Jane Doe"],
        "min_likelihood": 2,
        "include_if_matched": True,
        "required": pdl_client._REQUIRED_PROFILE_FIELDS,
        "location": ["Atlanta, GA"],
        "profile": ["https://www.linkedin.com/in/jane-doe-rn"],
        "company": [
            "North Okaloosa Medical Center", "North Walton Doctors Hospital",
        ],
        "school": ["Nevada State University"],
    }
    assert captured["json"]["include_if_matched"] is True


def test_single_404_is_terminal_and_does_not_retry_individual_fields(monkeypatch):
    _enable_pdl(monkeypatch)
    calls = []

    def no_match(*identity):
        calls.append(identity)
        return FakeResponse(404), {}

    monkeypatch.setattr(pdl_client, "_call", no_match)
    result, credits = pdl_client._run_single_query_plan(_candidate())

    assert len(calls) == 1
    assert calls[0][1:] == (
        "Atlanta, GA", ["Stale Health"], [],
    )
    assert result["status"] == "no_match"
    assert result["lookup_attempted_strategies"] == ["combined_context"]
    assert result["lookup_attempt_count"] == 1
    assert credits == 0


def test_returned_identity_conflict_is_terminal_without_second_purchase(monkeypatch):
    _enable_pdl(monkeypatch)
    calls = []

    def conflict(*identity):
        calls.append(identity)
        payload = _payload("John Smith", "Seattle, Washington")
        return FakeResponse(200, payload, credits=1), payload

    monkeypatch.setattr(pdl_client, "_call", conflict)
    result, credits = pdl_client._run_single_query_plan(_candidate())

    assert len(calls) == 1
    assert credits == 1
    assert result["status"] == "success"
    accepted, _reason = pdl_client._local_result_accepted(_candidate(), result)
    assert accepted is False
    assert result["lookup_attempted_strategies"] == ["combined_context"]


def test_rate_limit_response_is_terminal(monkeypatch):
    _enable_pdl(monkeypatch)
    calls = []

    def rate_limited(*identity):
        calls.append(identity)
        return FakeResponse(429), {}

    monkeypatch.setattr(pdl_client, "_call", rate_limited)
    result, credits = pdl_client._run_single_query_plan(_candidate())

    assert len(calls) == 1
    assert result["status"] == "error"
    assert "rate or account limit" in result["error"]
    assert result["lookup_attempted_strategies"] == ["combined_context"]
    assert credits == 0


def test_batch_uses_one_combined_bulk_round_per_candidate(monkeypatch):
    _enable_pdl(monkeypatch)
    candidate_ids = [
        store.add_candidate(
            name, "Atlanta, GA",
            notes="Employer: Emory Healthcare\nSchool: Emory University",
            source="indeed",
        )
        for name in ("Jane Doe", "John Smith")
    ]
    rounds = []

    def fake_bulk_call(identities):
        rounds.append(identities)
        items = []
        for identity in identities:
            names = identity[0] if isinstance(identity[0], list) else [identity[0]]
            items.append({"status": 200, **_payload(names[0])})
        return FakeResponse(200, credits=len(items)), items

    monkeypatch.setattr(pdl_client, "_bulk_call", fake_bulk_call)
    batch = pdl_client.enrich_candidates(candidate_ids, "combined_batch_123")

    assert len(rounds) == 1
    assert len(rounds[0]) == 2
    assert all(identity[1:] == (
        "Atlanta, GA", ["Emory Healthcare"], ["Emory University"],
    ) for identity in rounds[0])
    assert batch["credits_spent"] == 2
    for candidate_id in candidate_ids:
        result = batch["results"][candidate_id]
        assert result["status"] == "success"
        assert result["lookup_strategy"] == "combined_context"
        assert result["lookup_attempted_strategies"] == ["combined_context"]


def test_combined_404_is_cached_and_replayed_without_provider_call(monkeypatch):
    _enable_pdl(monkeypatch)
    candidate_id = store.add_candidate(
        "Jane Doe", "Atlanta, GA",
        notes="Employer: Stale Health\nSchool: Example University",
        source="indeed",
    )
    calls = []

    def no_match(*identity):
        calls.append(identity)
        return FakeResponse(404), {}

    monkeypatch.setattr(pdl_client, "_call", no_match)
    first = pdl_client.enrich_candidate(candidate_id, "combined_none_123")
    replay = pdl_client.enrich_candidate(candidate_id, "combined_none_456")

    assert first["status"] == "no_match"
    assert first["cached"] is False
    assert first["lookup_attempted_strategies"] == ["combined_context"]
    assert len(calls) == 1
    assert replay["status"] == "no_match"
    assert replay["cached"] is True
    assert replay["credits_spent"] == 0


def test_query_strategy_diagnostics_stay_in_backend_evidence():
    candidate = _candidate()
    internal = pdl_client._with_lookup_diagnostics({
        "status": "success",
        "contacts_trusted": True,
        "emails": ["jane@example.test"],
        "phones": [],
    }, candidate, ["combined_context"], selected="combined_context")

    assert internal["lookup_strategy"] == "combined_context"
    assert internal["lookup_evidence"]["attempted_strategies"] == [
        "combined_context",
    ]

    public = api_module._public_lookup_result(internal)
    assert public == {
        "status": "found",
        "emails": ["jane@example.test"],
        "phones": [],
        "phone_contacts": [],
        "resume_required": True,
        "location_match": None,
    }
    serialized = json.dumps(public).casefold()
    assert "lookup" not in serialized
    assert "strategy" not in serialized
    assert "combined_context" not in serialized


def test_current_contact_no_match_reason_allows_facebook_hometown_retry():
    assert api_module._hometown_retry_allowed({
        "status": "no_match",
        "reason_code": "pdl_required_contact_no_match",
    })
