from __future__ import annotations

import api as api_module


def test_profile_import_serializes_bounded_structured_context():
    row = api_module._profile_row(api_module.ProfileImportIn(
        name="Jane Doe, RN",
        location="Atlanta, GA",
        headline="Registered Nurse at Current Health",
        roles=["ICU Nurse", "ICU Nurse"],
        employers=["Previous Hospital", "Previous Hospital"],
        schools=["Example University"],
        alternate_names=["Jane Smith Doe", "Janey"],
        notes="Visible source text",
        source="linkedin",
    ))

    assert row["name"] == "Jane Doe"
    assert "Headline: Registered Nurse at Current Health" in row["notes"]
    assert "Role: Registered Nurse" in row["notes"]
    assert "Role: ICU Nurse" in row["notes"]
    assert row["notes"].count("Role: ICU Nurse") == 1
    assert "Employer: Current Health" in row["notes"]
    assert "Employer: Previous Hospital" in row["notes"]
    assert "School: Example University" in row["notes"]
    assert "Source alternate name: Jane Smith Doe" in row["notes"]
    assert "Source alternate name: Janey" not in row["notes"]
    assert row["notes"].endswith("Visible source text")


def test_profile_import_adds_headline_tag_even_when_raw_text_contains_headline():
    row = api_module._profile_row(api_module.ProfileImportIn(
        name="Jane Doe",
        headline="Registered Nurse",
        notes="Jane Doe\nRegistered Nurse\nExample Hospital\n2020-Present",
        source="indeed",
    ))

    assert row["notes"].startswith("Headline: Registered Nurse\nRole: Registered Nurse\n")

