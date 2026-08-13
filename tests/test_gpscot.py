import xml.etree.ElementTree as ET

import gpscot.gpscot as gpscot


def test_cot_event_uses_standard_point_and_hostname_source():
    event = gpscot.cot_event(
        {"lat": 37.123456, "lon": -122.654321, "altHAE": 12.5, "epx": 3, "epy": 4},
        "GPSCOT-test-host",
        "a-f-G",
        10,
        "test-host",
    )

    cot = ET.fromstring(event)
    assert cot.get("uid") == "GPSCOT-test-host"
    point = cot.find("point")
    assert point is not None
    assert point.get("lat") == "37.1234"
    assert point.get("lon") == "-122.6543"
    assert point.get("hae") == "12.5"
    detail = cot.find("detail")
    assert detail is not None
    remarks = detail.find("remarks")
    assert remarks is not None
    assert remarks.text == "Network GPS from test-host"


def test_status_destination_redacts_credentials_and_token():
    safe = gpscot.redact_cot_url(
        "tak://user:password@example.test/enroll?token=secret&host=example.test"
    )
    assert "password" not in safe
    assert "secret" not in safe
    assert "token=REDACTED" in safe
