"""Offline regression tests for PDL contact/evidence normalization."""
import os
import sys
import tempfile

import httpx


os.environ["SOURCING_DB"] = os.path.join(tempfile.mkdtemp(), "pdl-recall-test.db")
os.environ["DATABASE_URL"] = ""
os.environ["STORAGE_ENABLED"] = "0"
os.environ["PDL_ENABLED"] = "0"
os.environ["PDL_API_KEY"] = ""
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sourcing import pdl_client as pdl, phone_policy  # noqa: E402


def _candidate(source="linkedin"):
    return {
        "name": "Jane Doe",
        "location": "Austin, TX",
        "source": source,
        "source_url": (
            "https://www.linkedin.com/in/jane-doe/?trk=profile"
            if source == "linkedin"
            else "https://www.facebook.com/jane.doe/?ref=profile"
        ),
        "notes": "Role: Registered Nurse\nEmployer: Example Health",
    }


def test_required_expression_accepts_mobile_email_or_associated_phone(monkeypatch):
    assert pdl._REQUIRED_PROFILE_FIELDS == (
        "(mobile_phone OR phone_numbers OR recommended_personal_email OR personal_emails OR work_email)"
    )
    assert "phone_numbers" in pdl._REQUIRED_PROFILE_FIELDS
    assert " emails" not in pdl._REQUIRED_PROFILE_FIELDS

    captured = {}

    class FakeClient:
        def get(self, url, *, params, headers):
            captured.update(params)
            return httpx.Response(404, json={})

    monkeypatch.setattr(pdl, "_client", lambda: FakeClient())
    pdl._call("Jane Doe", "Austin, TX")
    assert captured["required"] == pdl._REQUIRED_PROFILE_FIELDS


def test_bulk_request_uses_same_usable_contact_policy(monkeypatch):
    captured = {}

    class FakeClient:
        def post(self, url, *, json, headers, timeout):
            captured.update(json)
            return httpx.Response(200, json=[])

    monkeypatch.setattr(pdl, "_client", lambda: FakeClient())
    pdl._bulk_call([("Jane Doe", "Austin, TX", [], [], "")])
    assert captured["requests"][0]["params"]["required"] == (
        pdl._REQUIRED_PROFILE_FIELDS
    )


def test_historical_generic_emails_are_evidence_not_outreach_contacts():
    person = {
        "recommended_personal_email": "best@example.test",
        "personal_emails": ["best@example.test", "personal@example.test"],
        "work_email": "jane@example-health.test",
        "emails": [
            {
                "address": "best@example.test",
                "first_seen": "2022-01-01",
                "last_seen": "2026-01-01",
                "num_sources": 4,
            },
            {
                "address": "historical@example.test",
                "first_seen": "2015-01-01",
                "last_seen": "2018-01-01",
                "num_sources": 1,
            },
        ],
    }
    assert pdl._email_values(person) == [
        "best@example.test", "personal@example.test", "jane@example-health.test",
    ]
    evidence = {item["value"]: item for item in pdl._email_evidence(person)}
    assert evidence["best@example.test"]["accepted_for_outreach"] is True
    assert evidence["best@example.test"]["last_seen"] == "2026-01-01"
    assert evidence["historical@example.test"]["accepted_for_outreach"] is False


def test_only_explicit_mobile_phone_is_automatically_usable():
    person = {
        "mobile_phone": "+15125550100",
        "phone_numbers": ["+15125550100", "+15125550101"],
        "phones": [
            {
                "number": "+15125550101",
                "first_seen": "2019-01-01",
                "last_seen": "2023-01-01",
                "num_sources": 2,
            }
        ],
    }
    assert pdl._phone_values(person) == ["+15125550100"]
    evidence = {item["value"]: item for item in pdl._phone_evidence(person)}
    assert evidence["+15125550100"]["accepted"] is True
    assert evidence["+15125550101"]["accepted"] is False
    assert evidence["+15125550101"]["last_seen"] == "2023-01-01"


def test_explicitly_stale_pdl_associated_phone_is_not_displayable():
    evidence = pdl._phone_evidence({
        "phone_numbers": [{
            "number": "+15125550102", "type": "landline", "is_current": False,
        }],
    })
    assert evidence[0]["is_current"] is False
    assert phone_policy.preferred_phone_values({
        "phones": ["+15125550102"], "associated_phone_evidence": evidence,
    }) == []


def test_documented_location_history_and_metadata_are_retained():
    person = {
        "location_name": "Austin, Texas, United States",
        "location_names": [
            "Austin, Texas, United States",
            "Boston, Massachusetts, United States",
        ],
        "street_addresses": [
            {
                "name": "Austin, Texas, United States",
                "street_address": "123 Main St",
                "address_line_2": "Apt 4",
                "locality": "Austin",
                "region": "Texas",
                "postal_code": "78701",
                "country": "United States",
                "first_seen": "2024-01-01",
                "last_seen": "2026-01-01",
                "num_sources": 5,
            },
            {
                "name": "Boston, Massachusetts, United States",
                "street_address": "9 Old Rd",
                "locality": "Boston",
                "region": "Massachusetts",
                "postal_code": "02108",
                "country": "United States",
                "first_seen": "2014-01-01",
                "last_seen": "2019-01-01",
                "num_sources": 2,
            },
        ],
    }
    evidence = pdl._address_evidence(person)
    assert evidence[0]["value"] == "Austin, Texas, United States"
    assert evidence[0]["is_current"] is True
    current_street = next(item for item in evidence if "123 Main St" in item["value"])
    assert current_street["is_current"] is True
    assert current_street["num_sources"] == 5
    old = next(item for item in evidence if "9 Old Rd" in item["value"])
    assert old["is_current"] is False
    assert old["last_seen"] == "2019-01-01"
    assert "Boston, Massachusetts, United States" in pdl._address_values(person)


def test_aliases_and_explicit_source_alternate_names_are_separate_evidence():
    aliases = pdl._name_alias_evidence({
        "full_name": "Kathy Sirianni",
        "name_aliases": ["Kathy Sirianni", "Kathy Scott Sirianni", "Kathy Scott"],
    })
    assert [item["value"] for item in aliases] == [
        "Kathy Scott Sirianni", "Kathy Scott",
    ]

    source = pdl._source_name_alias_evidence({
        "name": "Kathy Sirianni",
        "notes": "Alternate name: Kathy Scott Sirianni\nAlternate name: Liz",
    })
    assert source == [{
        "value": "Kathy Scott Sirianni",
        "source_field": "captured_alternate_name",
        "independent_source": True,
    }]
    base = {"name": "Kathy Sirianni", "location": "Indian Land, SC", "notes": ""}
    full_alias = {**base, "notes": "Alternate name: Kathy Scott Sirianni"}
    nickname = {**base, "notes": "Alternate name: Liz"}
    assert pdl._request_key(full_alias) != pdl._request_key(base)
    assert pdl._request_key(nickname) == pdl._request_key(base)


def test_full_source_aliases_share_one_request_instead_of_spending_extra_credits(
    monkeypatch,
):
    candidate = {
        "name": "Elizabeth Cruz",
        "location": "Virginia Beach, VA",
        "notes": "Source alternate name: Elizabeth Scott Cruz\nAlternate name: Liz",
    }
    names = pdl._lookup_name_parameter(candidate)
    assert names == ["Elizabeth Cruz", "Elizabeth Scott Cruz"]

    captured = {}

    class FakeClient:
        def get(self, url, *, params, headers):
            captured["single"] = params
            return httpx.Response(404, json={})

        def post(self, url, *, json, headers, timeout):
            captured["bulk"] = json
            return httpx.Response(200, json=[])

    monkeypatch.setattr(pdl, "_client", lambda: FakeClient())
    pdl._call(names, candidate["location"])
    pdl._bulk_call([(names, candidate["location"], [], [], "")])

    assert captured["single"]["name"] == names
    assert captured["bulk"]["requests"][0]["params"]["name"] == names


def test_profiles_array_confirms_exact_social_url_despite_formatting():
    candidate = _candidate("linkedin")
    person = {
        "profiles": [{
            "network": "linkedin",
            "url": "linkedin.com/in/jane-doe",
            "username": "jane-doe",
            "first_seen": "2020-01-01",
            "last_seen": "2026-01-01",
            "num_sources": 3,
        }],
    }
    assert pdl._matching_profile_url(candidate, person) == (
        "https://www.linkedin.com/in/jane-doe"
    )
    evidence = pdl._profile_evidence(person)
    assert evidence[0]["last_seen"] == "2026-01-01"
    assert evidence[0]["num_sources"] == 3


def test_build_result_exposes_full_evidence_but_only_safe_contacts():
    candidate = _candidate("facebook")
    person = {
        "id": "pdl-person-1",
        "full_name": "Jane Marie Doe",
        "location_name": "Austin, Texas, United States",
        "location_names": [
            "Austin, Texas, United States", "Denver, Colorado, United States",
        ],
        "street_addresses": [{
            "name": "Denver, Colorado, United States",
            "street_address": "44 Previous Ave",
            "locality": "Denver",
            "region": "Colorado",
            "last_seen": "2021-01-01",
            "num_sources": 2,
        }],
        "name_aliases": ["Jane Marie Doe", "Jane Smith Doe"],
        "profiles": [{
            "network": "facebook", "url": "facebook.com/jane.doe",
        }],
        "recommended_personal_email": "jane@example.test",
        "emails": [{"address": "old@example.test", "last_seen": "2017-01-01"}],
        "mobile_phone": "+15125550100",
        "phone_numbers": ["+15125550100", "+15125550199"],
    }
    result = pdl._build_result(candidate, person, 8, ["name", "profile"])
    assert result["status"] == "success"
    assert result["emails"] == ["jane@example.test"]
    assert result["phones"] == ["+15125550100"]
    assert result["name_aliases"] == ["Jane Smith Doe"]
    assert "https://www.facebook.com/jane.doe" == result["profile_url"]
    assert "facebook.com/jane.doe" in result["profile_urls"]
    assert any("44 Previous Ave" in value for value in result["addresses"])
    assert any(
        item["value"] == "old@example.test"
        and item["accepted_for_outreach"] is False
        for item in result["email_evidence"]
    )
    assert any(
        item["value"] == "+15125550199" and item["accepted"] is False
        for item in result["associated_phone_evidence"]
    )
    assert [item["value"] for item in result["phone_evidence"]] == [
        "+15125550100"
    ]


def test_email_only_dedicated_contact_succeeds_but_generic_history_does_not():
    candidate = _candidate()
    accepted = pdl._build_result(candidate, {
        "full_name": "Jane Doe",
        "location_name": "Austin, Texas",
        "recommended_personal_email": "jane@example.test",
    }, 8, ["name", "location"])
    assert accepted["status"] == "success"
    assert accepted["phones"] == []

    historical_only = pdl._build_result(candidate, {
        "full_name": "Jane Doe",
        "location_name": "Austin, Texas",
        "emails": [{"address": "old@example.test", "last_seen": "2016-01-01"}],
    }, 8, ["name", "location"])
    assert historical_only["status"] == "no_match"
    assert historical_only["emails"] == []
    assert historical_only["email_evidence"][0]["value"] == "old@example.test"
