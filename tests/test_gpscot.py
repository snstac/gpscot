import asyncio
import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock, MagicMock

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


def test_cot_transport_attempt_builds_a_fresh_client(monkeypatch):
    """Each retry must replace the failed transport, queues, and workers."""
    config = {"COT_URL": "udp+wo://127.0.0.1:28087"}
    gpsd = object()
    clitool = MagicMock()
    clitool.tx_queue = object()
    clitool.setup = AsyncMock()
    clitool.run = AsyncMock()
    worker = object()

    monkeypatch.setattr(gpscot.pytak, "CLITool", MagicMock(return_value=clitool))
    monkeypatch.setattr(gpscot, "GpsWorker", MagicMock(return_value=worker))

    asyncio.run(gpscot.run_cot_client(config, gpsd))

    clitool.setup.assert_awaited_once_with()
    gpscot.GpsWorker.assert_called_once_with(clitool.tx_queue, config, gpsd)
    clitool.add_tasks.assert_called_once_with({worker})
    clitool.run.assert_awaited_once_with()


def test_main_places_custom_client_under_reconnect_supervisor(monkeypatch):
    """GPSCOT must not bypass PyTAK's process-local outage supervisor."""
    monkeypatch.delenv("COT_URL", raising=False)
    monkeypatch.delenv("NMEA_TARGETS", raising=False)
    gpsd = MagicMock()
    gpsd.run = AsyncMock()
    supervisor = AsyncMock()
    monkeypatch.setattr(gpscot, "GpsdClient", MagicMock(return_value=gpsd))
    monkeypatch.setattr(gpscot.pytak, "supervise_with_reconnect", supervisor)

    asyncio.run(gpscot.main())

    supervisor.assert_awaited_once()
    config, run_once = supervisor.await_args.args
    assert config["COT_URL"] == "udp+broadcast://255.255.255.255:4349"
    assert callable(run_once)
    gpsd.run.assert_awaited_once_with()
