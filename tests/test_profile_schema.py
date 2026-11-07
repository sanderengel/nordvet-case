from nordvet.explore.profile_schema import (
    ID_PATTERNS,
    case_collisions,
    conformance,
    field_profile,
    key_sets,
    non_ascii_chars,
    shape,
    type_name,
)


def test_type_name_keeps_bool_apart_from_int():
    assert [type_name(v) for v in (None, True, 3, 1.5, "x", [1], {})] == [
        "null", "bool", "int", "float", "str", "list", "object"]


def test_shape_maps_character_classes():
    assert shape("apt_03942c73") == "aaa_99999a99"
    assert shape("CL-316") == "AA-999"


def test_non_ascii_chars():
    assert non_ascii_chars("Linköping Køge") == ["ö", "ø"]
    assert non_ascii_chars("Hillerod") == []


def test_conformance_matches_whole_string_only():
    ok, bad = conformance(["CL-316", "CL-3160", "cl-316", " CL-316"], ID_PATTERNS["clinic_id"])
    assert ok == 1 and bad == ["CL-3160", "cl-316", " CL-316"]


def test_case_collisions():
    assert case_collisions(["CL-1", "cl-1", "CL-2"]) == [["CL-1", "cl-1"]]
    assert case_collisions(["a", "b"]) == []


def test_key_sets_group_by_status():
    rows = [{"status": "booked", "id": 1}, {"status": "booked", "id": 2},
            {"status": "cancelled", "id": 3, "cancelled_at": "x"}]
    sets = key_sets(rows, "status")
    assert sets["booked"][("id", "status")] == 2
    assert sets["cancelled"][("cancelled_at", "id", "status")] == 1


def test_field_profile_counts_presence_types_and_oddities():
    rows = [{"name": "Køge", "n": 1}, {"name": " x ", "n": 2.5}, {"name": ""}]
    p = field_profile(rows)
    assert p["name"]["present"] == 3 and p["n"]["present"] == 2
    assert p["name"]["non_ascii"] == 1 and p["name"]["padded"] == 1 and p["name"]["empty"] == 1
    assert p["name"]["maxlen"] == 4 and p["name"]["maxbytes"] == 5 and p["name"]["minlen"] == 0
    assert dict(p["n"]["types"]) == {"int": 1, "float": 1}
