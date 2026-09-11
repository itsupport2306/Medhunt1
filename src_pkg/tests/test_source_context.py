"""Focused tests for shared, conservative source-profile context."""
from sourcing import multi_provider, pdl_client, source_context


def _history_candidate() -> dict:
    return {
        "name": "Heather Godwin",
        "location": "Port Saint Lucie, FL",
        "source": "indeed",
        "notes": "\n".join((
            "Headline: ICU Nurse at Current Health",
            "Employer: Current Health",
            "School: State University",
            "Alternate name: Heather Marie Godwin",
            "Relative: Robert Godwin",
            "Experience",
            "Travel Nurse",
            "Regional Medical Center",
            "Jan 2020 – Present · 4 yrs",
            "Old Nurse",
            "Old Hospital",
            "(2015-2018)",
            "Associate of Science, Nursing",
            "Community College of Allegheny County",
            "Unstructured biography text is not an organization",
        )),
    }


def test_extracts_explicit_and_stable_resume_history_without_arbitrary_prose():
    context = source_context.extract(_history_candidate())

    assert context == {
        "companies": [
            "Current Health", "Regional Medical Center", "Old Hospital",
        ],
        "schools": [
            "State University", "Community College of Allegheny County",
        ],
        "roles": ["ICU Nurse", "Travel Nurse", "Old Nurse"],
        "aliases": ["Heather Marie Godwin"],
        "relatives": ["Robert Godwin"],
    }


def test_supports_school_then_degree_and_bounded_deduped_context():
    school_first = source_context.extract({
        "name": "Jane Doe",
        "location": "Atlanta, GA",
        "notes": "Example University\nBachelor of Science in Nursing\n2010 - 2014",
    })
    assert school_first["schools"] == ["Example University"]

    notes = [
        *(f"Employer: Employer {index}" for index in range(12)),
        *(f"Role: Role {index}" for index in range(12)),
        *(f"School: School {index}" for index in range(8)),
        "Example University",
        "Bachelor of Science in Nursing",
        "2010 - 2014",
        "Employer: employer 0",
    ]
    context = source_context.extract({
        "name": "Jane Doe", "location": "Atlanta, GA", "notes": "\n".join(notes),
    })

    assert len(context["companies"]) == source_context.COMPANY_LIMIT == 8
    assert len(context["roles"]) == source_context.ROLE_LIMIT == 8
    assert len(context["schools"]) == source_context.SCHOOL_LIMIT == 6
    assert context["companies"].count("Employer 0") == 1


def test_pdl_and_enformion_waterfall_share_the_same_source_facts():
    candidate = _history_candidate()
    context = source_context.extract(candidate)

    assert pdl_client._profile_match_inputs(candidate) == (
        context["companies"], context["schools"],
    )
    request = multi_provider._request(candidate, "pdl_no_accepted_contact")
    assert request is not None
    assert request["companies"] == context["companies"]
    assert request["schools"] == context["schools"]
    assert request["roles"] == context["roles"]
    assert request["aliases"] == context["aliases"]
    assert request["relatives"] == context["relatives"]
    assert request["seed_type"] == "enformion-person-search-v11-source-alias-relative"
    assert pdl_client._REQUEST_POLICY_VERSION == (
        "pdl-person-enrich-v22-combined-inputs"
    )


def test_indeed_imported_untagged_headline_is_only_a_last_resort_role():
    context = source_context.extract({
        "name": "Jane Doe",
        "location": "Atlanta, GA",
        "source": "indeed",
        "notes": "Senior Software Engineer at Example Labs\nJane Doe\nAtlanta, GA",
    })
    assert context["roles"] == ["Senior Software Engineer"]
    assert context["companies"] == ["Example Labs"]

    arbitrary = source_context.extract({
        "name": "Jane Doe",
        "location": "Atlanta, GA",
        "source": "linkedin",
        "notes": "Enjoys hiking and mentoring new graduates.",
    })
    assert arbitrary["roles"] == []
    assert arbitrary["companies"] == []
    assert arbitrary["schools"] == []

    indeed_prose = source_context.extract({
        "name": "Jane Doe",
        "location": "Atlanta, GA",
        "source": "indeed",
        "notes": "Passionate nurse seeking a new role.",
    })
    assert indeed_prose["roles"] == []


def test_explicit_profession_is_available_as_a_candidate_role():
    context = source_context.extract({
        "name": "Alexandra Rivera",
        "location": "Albany, NY",
        "source": "nysed",
        "notes": "Profession: Medicine\nLicense: 012345",
    })
    assert context["roles"] == ["Medicine"]
