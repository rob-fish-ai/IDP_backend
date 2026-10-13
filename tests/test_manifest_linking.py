"""Linking the payload's rows to the records Cartograph already holds.

Cartograph sends a manifest of the case's existing members, income and
asset records. A payload row that corresponds to one carries its id back
(`existing_member_id` / `person_id`, `existing_record_id`) so their import
updates the row in place; a wrong link writes one person's figures onto
another's record, so the linking is strict.
"""

from app.services.cartograph.manifest import link_members, link_payload, link_records, normalise_manifest


def _member(ref, first, last, dob=None):
    return {"ref": ref, "first_name": first, "last_name": last, "date_of_birth": dob}


def _row(id, first, last, dob=None, person_id=None, aka=()):
    return {"id": id, "first_name": first, "last_name": last, "date_of_birth": dob,
            "person_id": person_id, "aka_names": list(aka), "verified": False}


def test_a_member_links_on_date_of_birth_and_name_and_carries_both_ids_back():
    members = [_member("m01", "Valerie", "Thomas", "1959-07-14"), _member("m02", "Jamal", "Thomas", "2010-02-01")]
    manifest = {"household_members": [_row(4629, "Valerie", "Thomas", "1959-07-14", person_id=1981)]}
    warnings: list[str] = []
    linked = link_members(members, manifest, warnings)
    assert linked == {"m01": 4629}
    assert members[0]["existing_member_id"] == 4629 and members[0]["person_id"] == 1981
    assert members[0]["aka_names"] == []
    # A member Cartograph does not hold is sent to be created, with a warning, and the keys present.
    assert members[1]["existing_member_id"] is None and members[1]["person_id"] is None and members[1]["aka_names"] == []
    assert any("Jamal Thomas" in w and "none of the 1" in w for w in warnings)


def test_a_name_cartograph_does_not_have_for_the_person_is_sent_as_an_aka_suggestion():
    """J-CCAC-07105: the packet reads "Victoria Pole", Cartograph holds
    "Victoria Poll". Same date of birth: one person. The packet's spelling
    goes back in aka_names for staff to accept or dismiss, never as a
    rename. A spelling they already list as an aka is not suggested again."""
    members = [_member("m01", "Victoria", "Pole", "1950-01-01")]
    manifest = {"household_members": [_row(1, "Victoria", "Poll", "1950-01-01")]}
    link_members(members, manifest, [])
    assert members[0]["existing_member_id"] == 1 and members[0]["aka_names"] == ["Victoria Pole"]

    members = [_member("m01", "Victoria", "Pole", "1950-01-01")]
    manifest = {"household_members": [_row(1, "Victoria", "Poll", "1950-01-01", aka=["Victoria Pole"])]}
    link_members(members, manifest, [])
    assert members[0]["existing_member_id"] == 1 and members[0]["aka_names"] == []


def test_aka_names_are_matched_as_names_too():
    """A surname changed by marriage: the packet says "Maria Santos-Reyes",
    Cartograph holds "Maria Reyes" with "Maria Santos-Reyes" as an aka and
    no date of birth on file. The aka links her."""
    members = [_member("m01", "Maria", "Santos-Reyes", "1980-05-05")]
    manifest = {"household_members": [_row(7, "Maria", "Reyes", None, aka=["Maria Santos-Reyes"])]}
    link_members(members, manifest, [])
    assert members[0]["existing_member_id"] == 7 and members[0]["aka_names"] == []


def test_a_disagreeing_date_of_birth_links_only_a_near_identical_uncontested_name_and_is_reported():
    """J-CCAC-06736: "Joyce Perkins" read as 1956-02-13, held as 1958-02-13,
    the household's one member. One of the two dates is wrong; she is not
    two people. Linked on the name, and the disagreement is reported.
    "Maurice Carr" against "MIRA CARR" with different dates is not linked."""
    members = [_member("m01", "Joyce", "Perkins", "1956-02-13")]
    manifest = {"household_members": [_row(3, "Joyce", "Perkins", "1958-02-13")]}
    warnings: list[str] = []
    link_members(members, manifest, warnings)
    assert members[0]["existing_member_id"] == 3
    assert any("1956-02-13" in w and "1958-02-13" in w and "linked on the name" in w for w in warnings)

    members = [_member("m01", "Maurice", "Carr", "1958-01-16")]
    manifest = {"household_members": [_row(3, "MIRA", "CARR", "1958-11-16")]}
    link_members(members, manifest, [])
    assert members[0]["existing_member_id"] is None

    # Two John Smiths on file, one read with a date matching neither: nothing to tell them apart.
    members = [_member("m01", "John", "Smith", "1970-01-01")]
    manifest = {"household_members": [_row(1, "John", "Smith", "1971-01-01"), _row(2, "John", "Smith", "2001-06-06")]}
    link_members(members, manifest, [])
    assert members[0]["existing_member_id"] is None


def test_each_manifest_member_is_awarded_once_to_the_best_claimant():
    members = [_member("m01", "Ann", "Lee", "1960-01-01"), _member("m02", "Ann", "Lee", "1990-01-01")]
    manifest = {"household_members": [_row(10, "Ann", "Lee", "1990-01-01", person_id=5)]}
    link_members(members, manifest, [])
    assert members[0]["existing_member_id"] is None
    assert members[1]["existing_member_id"] == 10 and members[1]["person_id"] == 5


def test_an_income_record_links_on_member_and_type_with_cartographs_aliases_applied():
    """J-CCAC-07512: the packet's wages_and_salaries from "Addus Homecare";
    Cartograph's row is still typed non_federal_wages, "Addus Home Care".
    Their alias table says the two types are one. A record of a type the
    member has no row for is created on their side, with no id."""
    linked = {"m01": 4629}
    records = [
        {"member_ref": "m01", "income_type": "wages_and_salaries", "source_name": "Addus Homecare"},
        {"member_ref": "m01", "income_type": "ssi", "source_name": "Social Security Administration"},
        {"member_ref": "m01", "income_type": "pension", "source_name": "Teamsters"},
        {"member_ref": None, "income_type": "ssi", "source_name": "Social Security Administration"},
    ]
    rows = [
        {"id": 4329, "cert_household_member_id": 4629, "income_type": "ssi", "source_name": "SI - SSI"},
        {"id": 4330, "cert_household_member_id": 4629, "income_type": "non_federal_wages", "source_name": "Addus Home Care"},
        {"id": 4331, "cert_household_member_id": 9999, "income_type": "pension", "source_name": "Teamsters"},
    ]
    link_records(records, rows, linked, type_key="income_type", source_key="source_name",
                 manifest_source_key="source_name", context="income_records", warnings=[],
                 aliases={"non_federal_wage": "wages_and_salaries"})
    assert [r["existing_record_id"] for r in records] == [4330, 4329, None, None]


def test_several_rows_of_one_type_are_told_apart_by_source_name_or_left_unlinked():
    linked = {"m01": 1}
    records = [
        {"member_ref": "m01", "asset_type": "checking", "institution_name": "Bank of America"},
        {"member_ref": "m01", "asset_type": "checking", "institution_name": "Chase"},
        {"member_ref": "m01", "asset_type": "checking", "institution_name": None},
    ]
    rows = [
        {"id": 50, "cert_household_member_id": 1, "asset_type": "checking", "institution_name": "JPMorgan Chase Bank"},
        {"id": 51, "cert_household_member_id": 1, "asset_type": "checking", "institution_name": "Bank of America"},
    ]
    link_records(records, rows, linked, type_key="asset_type", source_key="institution_name",
                 manifest_source_key="institution_name", context="asset_records", warnings=[])
    assert records[0]["existing_record_id"] == 51
    # "Chase" against "JPMorgan Chase Bank" is below the source threshold with two rows to choose from.
    assert records[1]["existing_record_id"] is None and records[2]["existing_record_id"] is None

    # One row of the type: the type decides, whatever the institution reads.
    records = [{"member_ref": "m01", "asset_type": "savings", "institution_name": "Direct Express"}]
    rows = [{"id": 60, "cert_household_member_id": 1, "asset_type": "savings", "institution_name": None}]
    link_records(records, rows, linked, type_key="asset_type", source_key="institution_name",
                 manifest_source_key="institution_name", context="asset_records", warnings=[])
    assert records[0]["existing_record_id"] == 60


def test_the_manifest_is_taken_in_any_shape_and_the_keys_are_always_present():
    assert normalise_manifest(None) == {"household_members": [], "income_records": [], "asset_records": []}
    assert normalise_manifest([]) == {"household_members": [], "income_records": [], "asset_records": []}
    assert normalise_manifest({"household_members": [{"first_name": "No", "last_name": "Id"}, "junk"]})["household_members"] == []
    payload = {
        "household_members": [_member("m01", "A", "B", "2000-01-01")],
        "income_records": [{"member_ref": "m01", "income_type": "ssi", "source_name": None}],
        "asset_records": [{"member_ref": "m01", "asset_type": "checking", "institution_name": None}],
    }
    link_payload(payload, None, [])
    assert payload["household_members"][0]["existing_member_id"] is None
    assert payload["household_members"][0]["person_id"] is None
    assert payload["household_members"][0]["aka_names"] == []
    assert payload["income_records"][0]["existing_record_id"] is None
    assert payload["asset_records"][0]["existing_record_id"] is None
