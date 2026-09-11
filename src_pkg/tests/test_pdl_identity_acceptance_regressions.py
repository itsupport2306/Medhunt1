"""Focused regression coverage for PDL identity acceptance boundaries.

These tests deliberately exercise deterministic verification separately from
contact-provider transport.  They never call PDL, Enformion, or any network
service.
"""
import os
import sys

import pytest


os.environ["DATABASE_URL"] = ""
os.environ["STORAGE_ENABLED"] = "0"
os.environ["PDL_API_KEY"] = ""
os.environ["PDL_ENABLED"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sourcing import config, enrich, verification  # noqa: E402


def _pdl_result(**overrides) -> dict:
    result = {
        "source": "people_data_labs",
        "status": "success",
        "matched_name": "Jane Doe",
        "provider_location": "Atlanta, Georgia",
        "addresses": ["Atlanta, Georgia"],
        "emails": ["jane@example.test"],
        "phones": [],
        "confidence": 0.9,
        "likelihood": max(9, config.PDL_AUTO_ACCEPT_MIN_LIKELIHOOD),
        "matched_inputs": ["name", "location"],
    }
    result.update(overrides)
    return result


def _assess(candidate: dict, provider_result: dict) -> tuple[dict, dict]:
    assessment = verification.assess(candidate, provider_result)
    return assessment, enrich._pdl_quality_gate(assessment, provider_result)


def test_organization_mismatch_does_not_reject_exact_name_and_location():
    candidate = {
        "name": "Jane Doe",
        "location": "Atlanta, GA",
        "notes": "Employer: Emory Healthcare",
        "source": "indeed",
    }
    provider_result = _pdl_result(
        provider_company="Northside Hospital",
        provider_organizations=["Northside Hospital"],
    )

    assessment, gate = _assess(candidate, provider_result)

    assert assessment["evidence"]["organization_overlap"] == 0
    assert "organization" not in assessment["evidence"]["conflicts"]
    assert assessment["identity_status"] == "verified"
    assert gate["accepted"] is True
    assert gate["rule"] == "exact_name_location_and_high_likelihood"


def test_exact_name_and_location_need_no_organization_at_sufficient_likelihood():
    candidate = {
        "name": "Jane Doe",
        "location": "Atlanta, GA",
        "notes": "Role: Registered Nurse",
        "source": "indeed",
    }
    provider_result = _pdl_result(
        provider_company="",
        provider_organizations=[],
        likelihood=config.PDL_AUTO_ACCEPT_MIN_LIKELIHOOD,
    )

    assessment, gate = _assess(candidate, provider_result)

    assert assessment["evidence"]["name"]["exact"] is True
    assert assessment["evidence"]["location"]["exact"] is True
    assert assessment["evidence"]["organization_overlap"] == 0
    assert assessment["identity_status"] == "verified"
    assert gate["accepted"] is True
    assert gate["rule"] == "exact_name_location_and_high_likelihood"


def test_genuine_name_and_location_conflicts_remain_rejected():
    candidate = {
        "name": "Jane Doe",
        "location": "Atlanta, GA",
        "notes": "Employer: Emory Healthcare",
        "source": "indeed",
    }
    provider_result = _pdl_result(
        matched_name="John Smith",
        provider_location="Seattle, Washington",
        addresses=["Seattle, Washington"],
        provider_company="Northside Hospital",
        provider_organizations=["Northside Hospital"],
    )

    assessment, gate = _assess(candidate, provider_result)

    assert assessment["evidence"]["name"]["conflict"] is True
    assert assessment["evidence"]["location"]["conflict"] is True
    assert set(assessment["evidence"]["conflicts"]) == {"name", "location"}
    assert assessment["identity_status"] == "rejected"
    assert gate["accepted"] is False
    assert gate["rule"] == "identity_not_verified"


def test_exact_social_profile_and_name_use_targeted_floor_not_global_floor(
    monkeypatch,
):
    monkeypatch.setattr(config, "PDL_PROFILE_ACCEPT_MIN_LIKELIHOOD", 4)
    monkeypatch.setattr(config, "PDL_AUTO_ACCEPT_MIN_LIKELIHOOD", 6)
    candidate = {
        "name": "Jane Doe", "location": "Atlanta, GA", "notes": "",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/in/jane-doe-rn/",
    }
    provider_result = _pdl_result(
        provider_location="", addresses=[], likelihood=4,
        profile_url="https://linkedin.com/in/jane-doe-rn",
        matched_inputs=["name", "profile"],
    )

    assessment, gate = _assess(candidate, provider_result)

    assert assessment["identity_status"] == "verified"
    assert gate["accepted"] is True
    assert gate["rule"] == "exact_name_and_social_profile"


@pytest.mark.parametrize("likelihood", [1, 2, 3])
def test_exact_social_profile_below_targeted_floor_is_withheld(
    monkeypatch, likelihood,
):
    monkeypatch.setattr(config, "PDL_PROFILE_ACCEPT_MIN_LIKELIHOOD", 4)
    candidate = {
        "name": "Jane Doe", "location": "Atlanta, GA", "notes": "",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/in/jane-doe-rn/",
    }
    provider_result = _pdl_result(
        provider_location="", addresses=[], likelihood=likelihood,
        profile_url="https://linkedin.com/in/jane-doe-rn",
        matched_inputs=["name", "profile"],
    )

    _assessment, gate = _assess(candidate, provider_result)

    assert gate["accepted"] is False


def test_different_social_profile_is_not_social_confirmation(monkeypatch):
    monkeypatch.setattr(config, "PDL_PROFILE_ACCEPT_MIN_LIKELIHOOD", 4)
    candidate = {
        "name": "Jane Doe", "location": "Atlanta, GA", "notes": "",
        "source": "linkedin",
        "source_url": "https://www.linkedin.com/in/jane-doe-rn/",
    }
    provider_result = _pdl_result(
        provider_location="", addresses=[], likelihood=4,
        profile_url="https://linkedin.com/in/different-person",
        matched_inputs=["name"],
    )

    _assessment, gate = _assess(candidate, provider_result)

    assert gate["accepted"] is False
    assert gate["social_profile_match"] is False


@pytest.mark.parametrize(
    ("source_fact", "matched_input"),
    [
        pytest.param("School: Emory University", "school", id="school"),
        pytest.param("Employer: Emory Healthcare", "company", id="employer"),
    ],
)
def test_historical_address_and_explicit_organization_input_can_corroborate(
    source_fact, matched_input,
):
    candidate = {
        "name": "Jane Doe",
        "location": "Atlanta, GA",
        "notes": source_fact,
        "source": "indeed",
    }
    provider_result = _pdl_result(
        provider_location="Seattle, Washington",
        addresses=["Atlanta, Georgia", "Seattle, Washington"],
        address_evidence=[{
            "value": "Atlanta, Georgia",
            "is_current": False,
            "last_seen": "2018-01-01",
        }],
        provider_company="",
        provider_organizations=[],
        matched_inputs=["name", matched_input],
    )

    assessment, gate = _assess(candidate, provider_result)

    assert assessment["evidence"]["matched_address_kind"] == "historical"
    assert assessment["evidence"]["provider_organization_input_match"] is True
    assert assessment["identity_status"] == "verified"
    assert gate["accepted"] is True


def test_resume_history_context_can_corroborate_without_becoming_a_veto():
    candidate = {
        "name": "Jane Doe", "location": "Atlanta, GA", "source": "indeed",
        "notes": "Registered Nurse\nEmory Healthcare\n2021-Present",
    }
    provider_result = _pdl_result(
        provider_location="Seattle, Washington",
        addresses=["Atlanta, Georgia", "Seattle, Washington"],
        address_evidence=[{
            "value": "Atlanta, Georgia", "is_current": False,
            "last_seen": "2018-01-01",
        }],
        provider_company="Emory Healthcare",
        provider_organizations=["Emory Healthcare"],
        matched_inputs=["name"],
    )

    assessment, gate = _assess(candidate, provider_result)

    assert assessment["evidence"]["organization_overlap"] == 1.0
    assert gate["independent_context_overlap"] == 1.0
    assert gate["accepted"] is True
