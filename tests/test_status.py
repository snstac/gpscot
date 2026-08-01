#!/usr/bin/env python3
# Copyright Sensors & Signals LLC https://www.snstac.com/
# SPDX-License-Identifier: Apache-2.0
"""Tests for the GPSTAK runtime status surface.

These drive the gpsd reader with real gpsd JSON reports rather than invented
ones, because the whole difficulty here is that a GPS gateway has several
distinct failure modes that look identical from outside -- gpsd down, gpsd up
with no lock, locked but no position in the report -- and a status surface that
collapses them into "quiet" is worse than none at all.

There is no `async def` test below. pytest-asyncio is NOT in this repo's
requirements_test.txt, and a bare `async def` test under plain pytest is
SKIPPED while still being reported in the pass count -- tests that cannot fail.
The one coroutine we need is driven with asyncio.run().
"""

import json

import pytest

import pytak

import gpstak.gpstak as gpstak


needs_statuswriter = pytest.mark.skipif(
    not hasattr(pytak, "StatusWriter"),
    reason="installed pytak predates pytak.StatusWriter (added in 7.4.0)",
)

# Real gpsd reports, as emitted by gpsd 3.22 with a u-blox receiver.
TPV_3D = {
    "class": "TPV",
    "device": "/dev/ttyACM0",
    "mode": 3,
    "lat": 37.7601,
    "lon": -122.4977,
    "altHAE": 61.2,
    "epx": 4.1,
    "epy": 5.3,
    "epv": 9.9,
    "track": 121.4,
    "speed": 0.031,
}

# gpsd keeps reporting TPV while the receiver is still searching. mode 1 is
# "no fix": the commonest real fault (antenna indoors, cold start).
TPV_NO_FIX = {"class": "TPV", "device": "/dev/ttyACM0", "mode": 1}

# Satellite detail arrives as a SEPARATE report; a TPV never carries it.
SKY = {
    "class": "SKY",
    "device": "/dev/ttyACM0",
    "hdop": 0.87,
    "vdop": 1.31,
    "satellites": [
        {"PRN": 5, "el": 62, "az": 122, "ss": 41, "used": True},
        {"PRN": 13, "el": 44, "az": 291, "ss": 38, "used": True},
        {"PRN": 20, "el": 12, "az": 33, "ss": 22, "used": False},
    ],
}


def _client(tmp_path, status=None):
    client = gpstak.GpsdClient("127.0.0.1", 2947)
    client.status = status if status is not None else _writer(tmp_path)
    return client


def _writer(tmp_path, name="gpstak-test"):
    return pytak.StatusWriter(name, path=str(tmp_path / "status.json"))


def _worker(tmp_path, client):
    """A GpsWorker without pytak.QueueWorker's constructor.

    __init__ needs a live queue and config; none of the status behaviour does.
    """
    worker = gpstak.GpsWorker.__new__(gpstak.GpsWorker)
    worker.gpsd = client
    worker.status = client.status
    return worker


def _doc(status):
    with open(status.path) as handle:
        return json.load(handle)


@needs_statuswriter
class TestGpsdObservation:
    """What the gpsd reader records, per report class."""

    def test_a_fix_counts_as_received_and_sets_the_fix_gauge(self, tmp_path):
        client = _client(tmp_path)
        client._observe(TPV_3D)
        client.status.write(force=True)

        doc = _doc(client.status)
        assert doc["counters"]["rx"] == 1
        assert doc["fix"] == "3D"
        assert doc["gpsd_connected"] is True
        assert client.tpv is TPV_3D

    def test_no_lock_is_counted_distinctly_and_does_not_become_a_position(
        self, tmp_path
    ):
        """"gpsd up, receiver searching" must not read the same as "gpsd down"."""
        client = _client(tmp_path)
        client._observe(TPV_NO_FIX)
        client.status.write(force=True)

        doc = _doc(client.status)
        assert doc["counters"]["rx"] == 1
        assert doc["counters"]["no_fix"] == 1
        assert doc["fix"] == "none"
        # The critical part: a no-fix TPV must never be adopted as a position.
        assert client.tpv is None

    def test_sky_supplies_satellites_used_and_hdop(self, tmp_path):
        """Neither number exists in a TPV, so missing SKY means missing both."""
        client = _client(tmp_path)
        client._observe(SKY)
        client.status.write(force=True)

        doc = _doc(client.status)
        assert doc["sats_used"] == 2  # three visible, two actually used
        assert doc["sats_seen"] == 3
        assert doc["hdop"] == 0.87
        assert doc["counters"]["sky"] == 1
        # A SKY report is not a fix and must not inflate the receive count.
        assert "rx" not in doc["counters"]

    def test_bookkeeping_reports_are_not_counted_as_fixes(self, tmp_path):
        """gpsd opens every session with VERSION/DEVICES/WATCH."""
        client = _client(tmp_path)
        client._observe({"class": "VERSION", "release": "3.22"})
        client._observe({"class": "DEVICES", "devices": []})
        client.status.write(force=True)

        assert _doc(client.status)["counters"] == {}


@needs_statuswriter
class TestUnreadableInputIsNotAFix:
    def test_garbage_from_gpsd_is_counted_but_not_received(self, tmp_path):
        """A line that is neither NMEA nor JSON is not a fix.

        Driven through run() against a fake gpsd so the counter is proved to be
        wired to the real read path, not just callable in isolation.
        """
        import asyncio

        status = _writer(tmp_path)
        lines = [b"{ this is not json\n", json.dumps(TPV_3D).encode() + b"\n"]

        class _Reader:
            async def readline(self):
                return lines.pop(0) if lines else b""

        class _Writer:
            def write(self, _data):
                return None

            async def drain(self):
                return None

        async def _fake_open(_host, _port):
            return _Reader(), _Writer()

        async def _drive():
            client = gpstak.GpsdClient("127.0.0.1", 2947, status=status)
            orig = asyncio.open_connection
            asyncio.open_connection = _fake_open
            try:
                # run() reconnects forever; stop it once the reader is drained
                # and it hits its 5s retry sleep.
                await asyncio.wait_for(client.run(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            finally:
                asyncio.open_connection = orig
            return client

        client = asyncio.run(_drive())
        status.write(force=True)

        doc = _doc(status)
        assert doc["counters"]["unparseable"] == 1
        assert doc["counters"]["rx"] == 1  # only the TPV, not the garbage
        assert doc["counters"]["gpsd_disconnect"] == 1  # reader ran dry
        assert client.tpv is None  # cleared on disconnect


@needs_statuswriter
class TestEmittedEvents:
    """What lands in the decode feed the Cockpit plugin renders."""

    def test_emitting_records_the_fix_detail(self, tmp_path):
        client = _client(tmp_path)
        client._observe(SKY)
        client._observe(TPV_3D)
        worker = _worker(tmp_path, client)

        event = worker.next_event("GPSTAK-t", "a-f-G", 10, "t")
        assert event is not None
        worker.status.write(force=True)

        doc = _doc(worker.status)
        assert doc["counters"]["emitted"] == 1
        entry = doc["recent"][0]
        assert entry["fix"] == "3D"
        assert entry["lat"] == 37.7601
        assert entry["lon"] == -122.4977
        # Carried across from SKY: the point of tracking it separately.
        assert entry["sats"] == 2
        assert entry["hdop"] == 0.87

    def test_no_fix_yet_is_counted_not_emitted(self, tmp_path):
        client = _client(tmp_path)
        worker = _worker(tmp_path, client)

        assert worker.next_event("GPSTAK-t", "a-f-G", 10, "t") is None
        worker.status.write(force=True)

        doc = _doc(worker.status)
        assert doc["counters"]["no_fix_to_emit"] == 1
        assert "emitted" not in doc["counters"]
        assert doc["recent"] == []

    def test_a_fix_with_no_coordinates_is_its_own_counter(self, tmp_path):
        """Passed the mode gate but carries no lat/lon: a receiver quirk."""
        client = _client(tmp_path)
        client.tpv = {"class": "TPV", "mode": 3}  # mode says 3D, no position
        worker = _worker(tmp_path, client)

        assert worker.next_event("GPSTAK-t", "a-f-G", 10, "t") is None
        worker.status.write(force=True)

        doc = _doc(worker.status)
        assert doc["counters"]["no_position"] == 1
        assert "no_fix_to_emit" not in doc["counters"]


class TestStatusDegradesVisibly:
    """A pytak without StatusWriter must not take the gateway down.

    Fleet boxes run pytak 7.3.13, which has no StatusWriter at all, so this is
    the path most installs take today -- not a hypothetical.
    """

    def test_no_op_status_when_pytak_is_too_old(self, monkeypatch):
        monkeypatch.setattr(gpstak, "_StatusWriter", None)
        status = gpstak.make_status("gpstak", "1.0.1")

        assert isinstance(status, gpstak._NoStatus)
        # Every call the gateway makes must be safe on the stand-in.
        status.count("rx")
        status.record(lat=1.0, lon=2.0)
        status.set(fix="3D")
        assert status.write() is False
        assert status.write(force=True) is False

    def test_the_read_path_survives_a_no_op_status(self, monkeypatch, tmp_path):
        """The counters are wired into _observe; prove they are not load-bearing."""
        monkeypatch.setattr(gpstak, "_StatusWriter", None)
        client = gpstak.GpsdClient("127.0.0.1", 2947)
        assert isinstance(client.status, gpstak._NoStatus)

        client._observe(SKY)
        client._observe(TPV_3D)
        client._observe({"class": "TPV", "mode": 1})

        # Still does its actual job: adopt the fix, drop the no-lock report.
        assert client.tpv is TPV_3D
        assert client.sats_used == 2

    def test_real_writer_used_when_available(self):
        if gpstak._StatusWriter is None:
            pytest.skip("installed pytak has no StatusWriter")
        assert not isinstance(gpstak.make_status("gpstak", "1.0.1"), gpstak._NoStatus)


class TestVersion:
    def test_version_matches_the_packaged_version_file(self):
        """The literal used to drift behind setup.cfg's VERSION file."""
        import os

        path = os.path.join(os.path.dirname(gpstak.__file__), "VERSION")
        with open(path, encoding="utf-8") as handle:
            assert gpstak.VERSION == handle.read().strip()
