from nordvet.explore.explore import mask_shape


def test_mask_shape_hides_token_values_but_keeps_short_scalars():
    shape = mask_shape({"access_token": "abc", "token_type": "bearer", "expires_in": 3600})
    assert shape["access_token"] == "<str, 3 chars>"
    assert shape["token_type"] == "bearer"
    assert shape["expires_in"] == 3600


def test_mask_shape_hides_long_strings_and_truncates_lists():
    shape = mask_shape({"note": "x" * 50, "items": [1, 2, 3, 4, 5]})
    assert shape["note"] == "<str, 50 chars>"
    assert shape["items"] == [1, 2, 3]


def test_month_windows_cover_months_and_year_end():
    from datetime import date

    from nordvet.explore.explore_full import month_windows

    windows = list(month_windows(date(2025, 11, 15), date(2026, 1, 2)))
    assert windows == [
        (date(2025, 11, 1), date(2025, 12, 1)),
        (date(2025, 12, 1), date(2026, 1, 1)),
        (date(2026, 1, 1), date(2026, 2, 1)),
    ]


def test_jwt_info_shows_names_and_times_but_not_claim_values():
    import base64
    import json

    from nordvet.explore.explore_full import jwt_info

    def enc(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    token = ".".join([enc({"alg": "HS256"}), enc({"sub": "secret-value", "iat": 1000, "exp": 4600}), "sig"])
    info = jwt_info(token)
    assert info["claims"] == ["exp", "iat", "sub"]
    assert info["lifetime_seconds"] == 3600
    assert "secret-value" not in json.dumps(info)
    assert jwt_info("not-a-jwt") == {"is_jwt": False}


def test_profile_helpers_parse_times_and_buckets():
    from datetime import datetime, timedelta

    from nordvet.explore.profile_raw import expected_offset, fmt_offset, lead_bucket, parse_sched

    naive, off, utc = parse_sched("2026-10-30T08:00+01:00")
    assert naive == datetime(2026, 10, 30, 8, 0) and off == timedelta(hours=1) and utc == datetime(2026, 10, 30, 7, 0)
    assert fmt_offset(timedelta(hours=2)) == "+02:00"
    assert expected_offset(datetime(2026, 10, 24, 9, 0), "Europe/Copenhagen") == "+02:00"
    assert expected_offset(datetime(2026, 10, 26, 9, 0), "Europe/Stockholm") == "+01:00"
    assert [lead_bucket(h) for h in (-1, 1, 5, 48, 400)] == [
        "after the start", "under 2 h before", "2-24 h before", "1-7 days before", "7+ days before",
    ]
