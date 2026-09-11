"""Offline tests for Enformion identity ranking; no provider credits are used."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sourcing import enformion_client as ef, phone_policy


def _person(name, city="Atlanta", state="GA", phone="4045550123", **extra):
    return {
        "fullName": name,
        "addresses": [{"city": city, "state": state, "isCurrent": True}],
        "phoneNumbers": [{
            "phoneNumber": phone, "phoneType": "Wireless", "isConnected": True,
        }],
        **extra,
    }


def test_middle_names_are_ignored_but_terminal_surname_must_match():
    accepted = ef._map_search(
        {"persons": [_person("Jane Marie Anne Doe")]},
        "Jane Doe", "Atlanta, GA",
    )
    assert accepted["status"] == "success"

    rejected = ef._map_search(
        {"persons": [_person("Jane Lee Smith")]},
        "Jane Lee", "Atlanta, GA",
    )
    assert rejected["status"] == "no_match"


def test_explicit_suffix_conflict_is_rejected_but_missing_suffix_is_allowed():
    conflict = ef._map_search(
        {"persons": [_person("John Michael Doe Sr")]},
        "John Doe Jr", "Atlanta, GA",
    )
    assert conflict["status"] == "no_match"

    missing = ef._map_search(
        {"persons": [_person("John Michael Doe")]},
        "John Doe Jr", "Atlanta, GA",
    )
    assert missing["status"] == "success"


def test_compound_surname_does_not_collapse_to_last_token():
    rejected = ef._map_search(
        {"persons": [_person("Silvia Clarke")]},
        "Silvia Lopez-Clarke", "Atlanta, GA",
    )
    accepted = ef._map_search(
        {"persons": [_person("Silvia Marie Lopez Clarke")]},
        "Silvia Lopez-Clarke", "Atlanta, GA",
    )

    assert rejected["status"] == "no_match"
    assert accepted["status"] == "success"


def test_alias_with_exact_city_is_admissible_without_other_context():
    result = ef._map_search({"persons": [_person(
        "Elizabeth Jones", aliases=[{"fullName": "Elizabeth Cruz"}],
    )]}, "Elizabeth Cruz", "Atlanta, GA")

    assert result["status"] == "success"
    assert result["matched_name"] == "Elizabeth Cruz"


def test_alias_with_state_only_location_still_needs_independent_context():
    result = ef._map_search({"persons": [_person(
        "Elizabeth Jones", city="Savannah",
        aliases=[{"fullName": "Elizabeth Cruz"}],
    )]}, "Elizabeth Cruz", "Atlanta, GA")

    assert result["status"] == "no_match"


def test_provider_alias_requires_location_and_independent_context():
    result = ef._map_search({"persons": [_person(
        "Elizabeth Marie Jones",
        aliases=[{"firstName": "Elizabeth", "lastName": "Cruz"}],
        workplaceSummary=[{"companyName": "Emory Healthcare"}],
        tahoeId="TH-123", dob="1984-02-03",
    )]}, "Elizabeth Cruz", "Atlanta, GA",
        expected_companies=["Emory Healthcare"])

    assert result["status"] == "success"
    assert result["matched_name"] == "Elizabeth Cruz"
    assert result["matched_name_type"] == "provider_alias"
    assert result["provider_primary_name"] == "Elizabeth Marie Jones"
    assert result["provider_aliases"] == ["Elizabeth Cruz"]
    assert result["provider_tahoe_ids"] == ["TH-123"]
    assert result["provider_birth_date"] == "1984-02-03"


def test_provider_relative_does_not_validate_primary_person_by_itself():
    result = ef._map_search({"persons": [_person(
        "Alice Smith", relatives=[{"fullName": "Jane Doe"}],
    )]}, "Jane Doe", "Atlanta, GA")

    assert result["status"] == "no_match"


def test_source_relative_can_support_but_never_replace_primary_name_match():
    result = ef._map_search({"persons": [_person(
        "Jane Doe", city="Savannah",
        relatives=[{"name": {"FirstName": "Robert", "LastName": "Doe"}}],
    )]}, "Jane Doe", "Atlanta, GA", expected_relatives=["Robert Doe"])

    assert result["status"] == "success"
    evidence = result["selection"]["identity_evidence"]
    assert evidence["location"]["state_match"] is True
    assert evidence["location"]["exact"] is False
    assert evidence["relative"]["score"] == 1.0


def test_current_address_outranks_old_address_for_same_name_people():
    old = _person("Jane Doe", phone="4045550101")
    old["addresses"] = [{
        "city": "Atlanta", "state": "GA", "isCurrent": False,
        "lastReportedDate": "2011-01-02",
    }]
    current = _person("Jane Doe", phone="4045550102")

    result = ef._map_search(
        {"persons": [old, current]}, "Jane Doe", "Atlanta, GA",
    )

    assert result["status"] == "success"
    assert result["phones"] == ["(404) 555-0102"]
    assert result["selection"]["selection"] == "ranked_identity_match"
    assert result["selection"]["winner_margin"] >= 0.08


def test_independent_workplace_breaks_same_state_city_mismatch_tie():
    wrong = _person(
        "Jane Doe", city="Savannah", phone="4045550101",
        workplaceSummary=[{"companyName": "Other Hospital"}],
    )
    right = _person(
        "Jane Doe", city="Augusta", phone="4045550102",
        workplaceSummary=[{"companyName": "Emory Healthcare"}],
    )
    result = ef._map_search(
        {"persons": [wrong, right]}, "Jane Doe", "Atlanta, GA",
        expected_companies=["Emory Healthcare"],
    )

    assert result["status"] == "success"
    assert result["phones"] == ["(404) 555-0102"]
    assert result["provider_workplaces"] == ["Emory Healthcare"]


def test_generic_job_title_alone_does_not_overcome_city_mismatch():
    result = ef._map_search({"persons": [_person(
        "Jane Doe", city="Savannah",
        workplaceSummary=[{"jobTitle": "Registered Nurse"}],
    )]}, "Jane Doe", "Atlanta, GA", expected_roles=["Registered Nurse"])

    assert result["status"] == "no_match"


def test_close_same_identity_candidates_remain_ambiguous():
    first = _person("Jane Doe", phone="4045550101")
    second = _person("Jane Doe", phone="4045550102")
    result = ef._map_search(
        {"persons": [first, second]}, "Jane Doe", "Atlanta, GA",
    )

    assert result["status"] == "no_match"
    assert result["selection"]["selection"] == "ambiguous_exact_matches"
    assert result["phones"] == []


def test_contact_rich_wrong_location_cannot_win_identity():
    wrong = _person("Jane Doe", city="Miami", state="FL", phone="3055550101")
    wrong["emailAddresses"] = [{"emailAddress": "wrong@example.com"}]
    right = _person("Jane Doe", phone="4045550102")
    result = ef._map_search(
        {"persons": [wrong, right]}, "Jane Doe", "Atlanta, GA",
    )

    assert result["status"] == "success"
    assert result["phones"] == ["(404) 555-0102"]
    assert result["emails"] == []


def test_connected_nonmobile_is_fallback_but_unconnected_mobile_is_rejected():
    person = _person("Jane Doe")
    person["phoneNumbers"] = [
        {"phoneNumber": "4045550101", "phoneType": "Landline", "isConnected": True},
        {"phoneNumber": "4045550102", "phoneType": "Wireless", "isConnected": False},
    ]
    result = ef._map_search(
        {"persons": [person]}, "Jane Doe", "Atlanta, GA",
    )

    assert result["status"] == "success"
    assert result["phones"] == ["(404) 555-0101"]
    assert result["phone_policy"] == phone_policy.OTHER_PHONE_POLICY


def test_pascal_case_provider_contacts_are_parsed_without_weakening_filters():
    result = ef._map_search({"Persons": [{
        "FullName": "Jane Doe",
        "Addresses": [{"City": "Atlanta", "State": "GA", "IsCurrent": True}],
        "PhoneNumbers": [
            {
                "PhoneNumber": "4045550101", "PhoneType": "Wireless",
                "IsConnected": True, "IsCurrent": True, "PhoneOrder": 1,
            },
            {
                "PhoneNumber": "4045550102", "PhoneType": "Wireless",
                "IsConnected": False, "IsCurrent": True, "PhoneOrder": 2,
            },
            {
                "PhoneNumber": "4045550103", "PhoneType": "Fax",
                "IsConnected": True, "IsCurrent": True, "PhoneOrder": 3,
            },
        ],
        "EmailAddresses": [
            {
                "EmailAddress": "jane@example.test", "IsCurrent": True,
                "EmailOrdinal": 1,
            },
            {
                "EmailAddress": "old@example.test", "IsCurrent": False,
                "EmailOrdinal": 2,
            },
        ],
    }]}, "Jane Doe", "Atlanta, GA")

    assert result["status"] == "success"
    assert result["phones"] == ["(404) 555-0101"]
    assert result["emails"] == ["jane@example.test"]
    assert result["phone_evidence"][0]["is_connected"] is True
    assert result["phone_evidence"][1]["accepted"] is False
    assert result["phone_evidence"][2]["accepted"] is False
    assert result["email_evidence"][1]["is_current"] is False


def test_fax_pager_stale_and_invalid_lines_are_not_callable_fallbacks():
    person = _person("Jane Doe")
    person["phoneNumbers"] = [
        {"phoneNumber": "4045550111", "phoneType": "Fax", "isConnected": True},
        {"phoneNumber": "4045550112", "phoneType": "Pager", "isConnected": True},
        {"phoneNumber": "4045550113", "phoneType": "Landline", "isConnected": True,
         "isCurrent": False},
        {"phoneNumber": "4045550114", "phoneType": "Landline", "isConnected": True,
         "status": "Disconnected"},
    ]
    result = ef._map_search({"persons": [person]}, "Jane Doe", "Atlanta, GA")
    assert result["status"] == "no_match"
    assert result["phones"] == []
    assert all(item["accepted"] is False for item in result["phone_evidence"])


def test_discovery_request_does_not_send_middle_name(monkeypatch):
    sent = {}

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"persons": []}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, *, json, headers):
            sent["body"] = json
            sent["headers"] = headers
            return Response()

    monkeypatch.setattr(ef.config, "DEMO_MODE", False)
    monkeypatch.setattr(ef.config, "ENFORMION_AP_NAME", "test")
    monkeypatch.setattr(ef.config, "ENFORMION_AP_PASSWORD", "test")
    monkeypatch.setattr(ef.httpx, "Client", Client)

    result = ef.enrich("Jane Marie Anne Doe", "Atlanta, GA")

    assert result["status"] == "no_match"
    assert sent["body"]["FirstName"] == "Jane"
    assert sent["body"]["MiddleName"] == ""
    assert sent["body"]["LastName"] == "Doe"


def test_discovery_request_sends_only_explicit_full_aliases_and_relatives(
    monkeypatch,
):
    sent = {}

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"persons": []}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, *, json, headers):
            sent["body"] = json
            return Response()

    monkeypatch.setattr(ef.config, "DEMO_MODE", False)
    monkeypatch.setattr(ef.config, "ENFORMION_AP_NAME", "test")
    monkeypatch.setattr(ef.config, "ENFORMION_AP_PASSWORD", "test")
    monkeypatch.setattr(ef.httpx, "Client", Client)

    ef.enrich(
        "Elizabeth Cruz", "Atlanta, GA",
        aliases=["Liz", "Elizabeth Marie Jones", "Elizabeth Cruz"],
        relatives=["Robert Alan Jones", "Bob"],
    )

    assert sent["body"]["Akas"] == [{
        "FirstName": "Elizabeth", "MiddleName": "Marie", "LastName": "Jones",
    }]
    assert sent["body"]["Relatives"] == [{
        "FirstName": "Robert", "MiddleName": "Alan", "LastName": "Jones",
    }]


def test_duplicate_fragments_with_same_owner_id_are_consolidated():
    identity = _person("Jane Doe", tahoeId="TH-JANE")
    identity["phoneNumbers"] = []
    contact = _person(
        "Jane Doe", phone="4045550198", tahoeId="TH-JANE",
        emailAddresses=[{"emailAddress": "jane@example.test"}],
    )

    result = ef._map_search(
        {"persons": [identity, contact]}, "Jane Doe", "Atlanta, GA",
    )

    assert result["status"] == "success"
    assert result["phones"] == ["(404) 555-0198"]
    assert result["emails"] == ["jane@example.test"]
    assert result["selection"]["candidates_reviewed"] == 1


def test_same_name_location_with_different_owner_ids_remains_ambiguous():
    first = _person("Jane Doe", phone="4045550101", tahoeId="TH-ONE")
    second = _person("Jane Doe", phone="4045550102", tahoeId="TH-TWO")

    result = ef._map_search(
        {"persons": [first, second]}, "Jane Doe", "Atlanta, GA",
    )

    assert result["status"] == "no_match"
    assert result["selection"]["selection"] == "ambiguous_exact_matches"


def test_structured_relative_records_preserve_ids_without_treating_ids_as_names():
    person = _person("Household Owner")
    person["relativesSummary"] = [{
        "Name": {"FirstName": "Jane", "MiddleName": "Marie", "LastName": "Doe"},
        "TahoeId": "TH-JANE-1",
        "PersonId": "PERSON-JANE-1",
        "DateOfBirth": "1985-04-03",
        "RelationshipType": "Sister",
        "SharedHouseholdIds": ["HOUSE-100", "HOUSE-200"],
        "MatchScore": 93,
    }, {
        "TahoeId": "123e4567-e89b-12d3-a456-426614174000",
        "PersonId": "ID-WITH-MANY-DASHES",
    }]

    records = ef._person_relative_records(person)

    assert records[0] == {
        "name": "Jane Marie Doe",
        "tahoe_ids": ["TH-JANE-1"],
        "person_ids": ["PERSON-JANE-1"],
        "birth_date": "1985-04-03",
        "age": None,
        "relationship": "Sister",
        "shared_household_ids": ["HOUSE-100", "HOUSE-200"],
        "provider_score": 0.93,
    }
    assert records[1]["name"] == ""
    assert ef._person_relatives(person) == ["Jane Marie Doe"]


def test_opaque_relative_ids_are_not_deduplicated_like_phone_numbers():
    person = _person("Household Owner")
    person["relatives"] = [{
        "fullName": "Jane Doe",
        "tahoeIds": [
            "TAHOE-100", "tahoe-100", "  TAHOE-100  ", "PERSON  100",
        ],
    }]
    assert ef._person_relative_records(person)[0]["tahoe_ids"] == [
        "TAHOE-100", "tahoe-100", "PERSON  100",
    ]


def test_unique_relative_pivot_is_contact_free_even_when_household_has_contacts():
    household = _person("Robert Smith", phone="4045550199")
    household["emailAddresses"] = [{"emailAddress": "household@example.com"}]
    household["relatives"] = [{
        "fullName": "Jane Marie Doe", "tahoeId": "TH-JANE",
        "personId": "P-JANE", "relationship": "Daughter",
    }]

    result = ef._map_search(
        {"persons": [household]}, "Jane Doe", "Atlanta, GA",
    )

    assert result["status"] == "relative_pivot"
    assert result["relative_tahoe_id"] == "TH-JANE"
    assert result["phones"] == []
    assert result["emails"] == []
    assert result["addresses"] == []
    assert result["household_contact_used"] is False


def test_relative_pivot_accepts_explicit_source_alias_only_at_exact_household_location():
    household = _person("Robert Smith")
    household["relatives"] = [{"fullName": "Elizabeth Jones", "tahoeId": "TH-LIZ"}]
    result = ef._map_search(
        {"persons": [household]}, "Elizabeth Cruz", "Atlanta, GA",
        expected_aliases=["Elizabeth Jones"],
    )
    assert result["status"] == "relative_pivot"
    assert result["selection"]["expected_name_type"] == "source_alias"

    household["addresses"] = [{"city": "Savannah", "state": "GA", "isCurrent": True}]
    rejected = ef._map_search(
        {"persons": [household]}, "Elizabeth Cruz", "Atlanta, GA",
        expected_aliases=["Elizabeth Jones"],
    )
    assert rejected["status"] == "no_match"


def test_relative_pivot_requires_one_unique_tahoe_id():
    household = _person("Robert Smith")
    household["relatives"] = [
        {"fullName": "Jane Doe", "tahoeId": "TH-ONE"},
        {"fullName": "Jane Marie Doe", "tahoeId": "TH-TWO"},
    ]
    result = ef._map_search(
        {"persons": [household]}, "Jane Doe", "Atlanta, GA",
    )
    assert result["status"] == "no_match"
    assert result["phones"] == []


def test_ambiguous_direct_owner_does_not_fall_through_to_relative_pivot():
    first = _person("Jane Doe", phone="4045550101")
    second = _person("Jane Doe", phone="4045550102")
    second["relatives"] = [{"fullName": "Jane Doe", "tahoeId": "TH-JANE"}]
    result = ef._map_search(
        {"persons": [first, second]}, "Jane Doe", "Atlanta, GA",
    )
    assert result["status"] == "no_match"
    assert result["selection"]["selection"] == "ambiguous_exact_matches"


def test_tahoe_resolution_returns_only_validated_target_contacts():
    household = _person("Robert Smith", phone="4045550199", tahoeId="TH-HOUSE")
    target = _person("Jane Marie Doe", phone="4045550105", tahoeId="TH-JANE")
    result = ef._map_tahoe_search(
        {"persons": [household, target]}, "TH-JANE", "Jane Doe", "Atlanta, GA",
    )

    assert result["status"] == "success"
    assert result["phones"] == ["(404) 555-0105"]
    assert "(404) 555-0199" not in result["phones"]
    assert result["relative_bridge_used"] is True
    assert result["household_contact_used"] is False


def test_tahoe_resolution_rejects_wrong_id_or_target_identity():
    target = _person("Another Person", phone="4045550105", tahoeId="TH-JANE")
    wrong_identity = ef._map_tahoe_search(
        {"persons": [target]}, "TH-JANE", "Jane Doe", "Atlanta, GA",
    )
    wrong_id = ef._map_tahoe_search(
        {"persons": [target]}, "TH-OTHER", "Another Person", "Atlanta, GA",
    )

    assert wrong_identity["status"] == "no_match"
    assert wrong_identity["phones"] == []
    assert wrong_id["status"] == "no_match"
    assert wrong_id["selection"]["selection"] == "tahoe_id_not_returned"


def test_tahoe_resolution_requires_exact_case_sensitive_opaque_id():
    target = _person("Jane Doe", phone="4045550105", tahoeId="th-jane")
    result = ef._map_tahoe_search(
        {"persons": [target]}, "TH-JANE", "Jane Doe", "Atlanta, GA",
    )

    assert result["status"] == "no_match"
    assert result["phones"] == []
    assert result["selection"]["selection"] == "tahoe_id_not_returned"


def test_resolve_tahoe_id_uses_tahoe_ids_request_body(monkeypatch):
    sent = {}

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"persons": [_person("Jane Doe", tahoeId="TH-JANE")]}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, *, json, headers):
            sent["body"] = json
            return Response()

    monkeypatch.setattr(ef.config, "DEMO_MODE", False)
    monkeypatch.setattr(ef.config, "ENFORMION_AP_NAME", "test")
    monkeypatch.setattr(ef.config, "ENFORMION_AP_PASSWORD", "test")
    monkeypatch.setattr(ef.httpx, "Client", Client)

    result = ef.resolve_tahoe_id("TH-JANE", "Jane Doe", "Atlanta, GA")

    assert sent["body"] == {"TahoeIds": ["TH-JANE"], "Page": 1, "ResultsPerPage": 10}
    assert result["status"] == "success"
    assert result["relative_bridge_used"] is True
    assert result["credits_spent"] == 1


def test_http_200_mapping_failures_still_report_one_spent_credit(monkeypatch):
    class Response:
        status_code = 200
        text = "not-json"

        @staticmethod
        def json():
            raise ValueError("malformed provider response")

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, *, json, headers):
            return Response()

    monkeypatch.setattr(ef.config, "DEMO_MODE", False)
    monkeypatch.setattr(ef.config, "ENFORMION_AP_NAME", "test")
    monkeypatch.setattr(ef.config, "ENFORMION_AP_PASSWORD", "test")
    monkeypatch.setattr(ef.httpx, "Client", Client)

    discovery = ef.enrich("Jane Doe", "Atlanta, GA")
    resolution = ef.resolve_tahoe_id("TH-JANE", "Jane Doe", "Atlanta, GA")

    assert discovery["status"] == "error"
    assert discovery["credits_spent"] == 1
    assert resolution["status"] == "error"
    assert resolution["credits_spent"] == 1
