#!/usr/bin/env python3
"""
GPSCOT: Network GPS for TAK — feed this device's GNSS position to ATAK/WinTAK.

ATAK's "External or Network GPS" listens on UDP 4349 for Cursor on Target XML
and adopts the received point as the device's own position (WinTAK also accepts
raw NMEA). GPSCOT reads gpsd and emits both:

  - CoT position events via PyTAK to COT_URL (default
    udp+broadcast://255.255.255.255:4349 — every ATAK device on the subnet),
  - optional raw NMEA ($GPGGA/$GPRMC passthrough from gpsd) to NMEA_TARGETS
    ("host:port host:port") for WinTAK.

Configuration is PyTAK-style via /etc/default/gpscot (systemd EnvironmentFile)
or the environment: COT_URL, GPSCOT_RATE, GPSCOT_UID, GPSCOT_COT_TYPE,
GPSCOT_STALE, GPSD_HOST, GPSD_PORT, NMEA_TARGETS.

See https://ampledata.org/network_gps.html

Copyright Sensors & Signals LLC https://www.snstac.com/
SPDX-License-Identifier: Apache-2.0
"""

import asyncio
import configparser
import json
import logging
import os
import socket
import time
import xml.etree.ElementTree as ET
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytak


def _read_version(default="1.0.0"):
    """Version from the packaged VERSION file, falling back to a literal.

    The literal used to be the only source and drifted behind setup.cfg's
    `version = file: src/gpscot/VERSION`, so the status surface would have
    advertised a version the package had not shipped for two releases. The
    fallback stays because the file is only present when installed as a
    package; a missing VERSION must not stop the gateway from starting.
    """
    try:
        with open(
            os.path.join(os.path.dirname(__file__), "VERSION"), encoding="utf-8"
        ) as handle:
            return handle.read().strip() or default
    except OSError:
        return default


VERSION = _read_version()
logger = logging.getLogger("gpscot")


class _NoStatus:
    """Stand-in for pytak.StatusWriter on a pytak too old to have one.

    AryaOS boxes are updated as packages, so this gateway can land on a host
    whose pytak predates StatusWriter (added in 7.4.0) -- fleet boxes are on
    7.3.13 today. Failing to import would take the gateway down over its
    telemetry helper, which is exactly backwards: feeding position to ATAK is
    the job, reporting on it is not.

    Degrading here is safe because it is VISIBLE. With nothing writing
    /run/gpscot/status.json, a management UI reports "no status from this
    gateway" rather than rendering an empty feed as though the sky were empty.
    """

    def count(self, *args, **kwargs) -> None:
        return None

    def record(self, *args, **kwargs) -> None:
        return None

    def set(self, *args, **kwargs) -> None:
        return None

    def set_health(self, *args, **kwargs) -> None:
        return None

    def set_input(self, *args, **kwargs) -> None:
        return None

    def set_output(self, *args, **kwargs) -> None:
        return None

    def write(self, *args, **kwargs) -> bool:
        return False


# Resolved at import so a missing StatusWriter is a startup-time decision
# rather than an AttributeError on the first fix.
_StatusWriter = getattr(pytak, "StatusWriter", None)


def make_status(app_name: str, version: str):
    """Return a status writer, or a no-op if this pytak has none."""
    if _StatusWriter is None:
        return _NoStatus()
    return _StatusWriter(app_name, version=version)


def conf(key, default):
    return os.environ.get(key, default)


def redact_cot_url(url):
    """Return a status-safe destination without credentials or tokens."""
    try:
        parts = urlsplit(str(url))
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        query = urlencode(
            [
                (
                    key,
                    "REDACTED" if key.lower() in ("token", "password") else value,
                )
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
            ]
        )
        return urlunsplit((parts.scheme, host, parts.path, query, parts.fragment))
    except (TypeError, ValueError):
        return "invalid://destination"


# gpsd TPV `mode`: 0 unknown, 1 no fix, 2 two-dimensional, 3 three-dimensional.
FIX_MODES = {0: "unknown", 1: "none", 2: "2D", 3: "3D"}


class GpsdClient:
    """Minimal asyncio gpsd watcher: keeps the latest TPV and raw NMEA lines."""

    def __init__(self, host, port, nmea_sink=None, status=None):
        self.host = host
        self.port = int(port)
        self.nmea_sink = nmea_sink
        self.tpv = None
        # SKY is a separate gpsd report from TPV, so satellite count and HDOP
        # have to be caught here or they are simply not available anywhere: a
        # TPV carries a position but says nothing about how well it was fixed,
        # which is the first thing an operator asks about a GPS gateway.
        self.sky = None
        self.sats_used = None
        self.sats_seen = None
        self.hdop = None
        # Defaults to a no-op so a GpsdClient built without a worker (tests,
        # a REPL) does not have to care about status at all.
        self.status = status if status is not None else _NoStatus()

    async def run(self):
        while True:
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
                watch = {"enable": True, "json": True}
                if self.nmea_sink:
                    watch["nmea"] = True
                writer.write(("?WATCH=" + json.dumps(watch) + "\n").encode())
                await writer.drain()
                logger.info("connected to gpsd at %s:%s", self.host, self.port)
                self.status.set_health("degraded", "gpsd connected; waiting for fix")
                self.status.set(input_state="connected")
                self.status.write(force=True)
                while True:
                    line = await reader.readline()
                    if not line:
                        raise ConnectionError("gpsd closed connection")
                    text = line.decode(errors="replace").strip()
                    if text.startswith("$"):
                        if self.nmea_sink and text[3:6] in ("GGA", "RMC"):
                            self.nmea_sink(text)
                            self.status.count("nmea_out")
                        continue
                    try:
                        msg = json.loads(text)
                    except ValueError:
                        # Not JSON and not NMEA: gpsd said something we cannot
                        # read. Counted rather than logged -- a chatty gpsd
                        # would otherwise flood the journal -- but counted, so
                        # a persistent protocol mismatch is visible.
                        self.status.count("unparseable")
                        continue
                    self._observe(msg)
            except (OSError, ConnectionError) as exc:
                logger.warning("gpsd: %s — retrying in 5s", exc)
                self.tpv = None
                self.status.count("gpsd_disconnect")
                self.status.set(gpsd_connected=False, fix="none")
                self.status.set_health("fault", "gpsd connection unavailable")
                self.status.set_input(connection="disconnected")
                self.status.write(force=True)
                await asyncio.sleep(5)

    def _observe(self, msg):
        """Fold one gpsd JSON report into our view of the receiver."""
        klass = msg.get("class")

        if klass == "SKY":
            self.sky = msg
            sats = msg.get("satellites") or []
            self.sats_seen = len(sats)
            self.sats_used = sum(1 for s in sats if s.get("used"))
            self.hdop = msg.get("hdop")
            self.status.count("sky")
            self.status.set(
                sats_used=self.sats_used, sats_seen=self.sats_seen, hdop=self.hdop
            )
            return

        if klass != "TPV":
            return

        # A TPV is the inbound "fix" for this gateway; everything else gpsd
        # emits (VERSION, DEVICES, WATCH) is bookkeeping, not data.
        self.status.count("rx")
        mode = msg.get("mode", 0) or 0
        self.status.set(fix=FIX_MODES.get(mode, str(mode)), gpsd_connected=True)
        self.status.set_input(
            last_observation=time.time(),
            connection="connected",
            fix=FIX_MODES.get(mode, str(mode)),
        )

        if mode < 2:
            # gpsd is talking to us but the receiver has no lock. This is the
            # single most common real fault -- antenna indoors, cold start --
            # and it is NOT the same as gpsd being down, so it gets its own
            # counter instead of being folded into silence.
            self.status.count("no_fix")
            self.status.set_health("degraded", "GNSS receiver has no position fix")
            return

        self.tpv = msg
        self.status.set_health("ok", "GNSS position fix active")


class NmeaFanout:
    """Raw NMEA passthrough over UDP for WinTAK network GPS."""

    def __init__(self, targets):
        self.addrs = []
        for t in targets.split():
            host, _, port = t.rpartition(":")
            if host and port.isdigit():
                self.addrs.append((host, int(port)))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    def send(self, sentence):
        data = (sentence + "\r\n").encode()
        for addr in self.addrs:
            try:
                self.sock.sendto(data, addr)
            except OSError as exc:
                logger.debug("nmea send %s: %s", addr, exc)


def cot_event(tpv, uid, cot_type, stale, source_name):
    """CoT position event from a gpsd TPV report."""
    lat = tpv.get("lat")
    lon = tpv.get("lon")
    if lat is None or lon is None:
        return None
    hae = tpv.get("altHAE", tpv.get("alt", 0.0)) or 0.0
    ce = max(float(tpv.get("epx", 0) or 0), float(tpv.get("epy", 0) or 0)) or 9999999.0
    le = float(tpv.get("epv", 0) or 0) or 9999999.0

    track = ET.Element("track")
    track.set("course", str(tpv.get("track", 0.0) or 0.0))
    track.set("speed", str(tpv.get("speed", 0.0) or 0.0))
    remarks = ET.Element("remarks")
    remarks.text = f"Network GPS from {source_name}"
    event = pytak.cot_event(
        uid=uid,
        cot_type=cot_type,
        stale=stale,
        point=pytak.cot_point(lat=lat, lon=lon, hae=hae, ce=ce, le=le),
        detail=pytak.cot_detail(track, remarks),
        how="m-g",
    )
    return pytak.serialize_cot(event, xml_declaration=False)


class GpsWorker(pytak.QueueWorker):
    """Emit the latest gpsd fix as CoT at a fixed rate."""

    def __init__(self, queue, config, gpsd):
        super().__init__(queue, config)
        self.gpsd = gpsd

        # Runtime status for Cockpit. systemd gives us /run/gpscot via
        # RuntimeDirectory=, so this lands where the plugin looks for it.
        self.status = make_status("gpscot", VERSION)
        # The gpsd client, not the worker, is where fixes actually arrive: the
        # worker only ever sees the LATEST TPV, so counting here would report
        # the emit rate and call it the receive rate. Share the one writer.
        gpsd.status = self.status

        # Seed the gauges HERE, not in run(). __init__ happens before either
        # loop is scheduled; doing it in run() raced the gpsd reader and could
        # stamp fix="none" over a lock that had already been reported.
        self.status.set(fix="none", gpsd_connected=False)
        self.status.set_health("degraded", "waiting for gpsd")
        self.status.set_input(connection="connecting")
        self.status.set_output(
            "connected",
            destination=redact_cot_url(config.get("COT_URL", "")),
        )

    async def run(self):
        rate = float(conf("GPSCOT_RATE", "1.0"))
        uid = conf("GPSCOT_UID", "GPSCOT-" + socket.gethostname())
        source_name = conf("GPSCOT_SOURCE_NAME", socket.gethostname())
        cot_type = conf("GPSCOT_COT_TYPE", "a-f-G")
        stale = int(conf("GPSCOT_STALE", "10"))
        logger.info(
            "emitting CoT every %ss as uid=%s source=%s", rate, uid, source_name
        )

        # Write once before any fix arrives. A cold GNSS start takes minutes and
        # an indoor antenna never locks at all; without this the UI would report
        # "no status from this gateway" for that whole time, which is
        # indistinguishable from a gateway that failed to start.
        self.status.set(
            uid=uid,
            source=source_name,
            rate_s=rate,
            gpsd=f"{self.gpsd.host}:{self.gpsd.port}",
        )
        self.status.write(force=True)

        last_beat = 0.0
        while True:
            event = self.next_event(uid, cot_type, stale, source_name)
            if event is not None:
                await self.put_queue(event)

            # The UI decides freshness from whether this file keeps changing,
            # so a locked-but-idle receiver MUST keep writing; otherwise a
            # perfectly healthy gateway parked on a rooftop reads as wedged.
            now = asyncio.get_running_loop().time()
            if now - last_beat >= 5:
                last_beat = now
                self.status.write(force=True)
            else:
                self.status.write()

            await asyncio.sleep(rate)

    def next_event(self, uid, cot_type, stale, source_name):
        """Turn the latest fix into CoT, updating status. None if there is none.

        Split out of run() so the status bookkeeping can be exercised without
        driving an infinite loop.
        """
        tpv = self.gpsd.tpv
        if not tpv:
            # No usable fix yet. Distinct from "gpsd is down" (counted in the
            # client) and from "we emitted nothing because we are idle".
            self.status.count("no_fix_to_emit")
            return None

        event = cot_event(tpv, uid, cot_type, stale, source_name)
        if event is None:
            # A TPV that passed the mode>=2 gate but carries no lat/lon. Rare,
            # and a receiver quirk rather than a normal state, so it is counted
            # separately from no_fix instead of looking like a quiet gateway.
            self.status.count("no_position")
            return None

        self.status.count("emitted")
        self.status.set_output(
            "connected",
            last_success=time.time(),
            destination=redact_cot_url(getattr(self, "config", {}).get("COT_URL", "")),
        )
        self.status.record(
            fix=FIX_MODES.get(tpv.get("mode", 0) or 0, "unknown"),
            sats=self.gpsd.sats_used,
            hdop=self.gpsd.hdop,
            lat=tpv.get("lat"),
            lon=tpv.get("lon"),
            alt=tpv.get("altHAE", tpv.get("alt")),
            speed=tpv.get("speed"),
        )
        return event


async def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s gpscot %(levelname)s %(message)s",
    )
    parser = configparser.ConfigParser()
    parser.read_dict(
        {
            "gpscot": {
                "COT_URL": conf("COT_URL", "udp+broadcast://255.255.255.255:4349"),
                "PYTAK_NO_HELLO": "1",
            }
        }
    )
    config = parser["gpscot"]
    # Pass through PYTAK_* (TLS etc.) from the environment.
    for key, val in os.environ.items():
        if key.startswith("PYTAK_"):
            config[key] = val

    nmea = None
    targets = conf("NMEA_TARGETS", "").strip()
    if targets:
        nmea = NmeaFanout(targets)
        logger.info("NMEA passthrough to: %s", targets)

    gpsd = GpsdClient(
        conf("GPSD_HOST", "127.0.0.1"),
        conf("GPSD_PORT", "2947"),
        nmea_sink=nmea.send if nmea else None,
    )

    clitool = pytak.CLITool(config)
    await clitool.setup()
    clitool.add_tasks({GpsWorker(clitool.tx_queue, config, gpsd)})
    await asyncio.gather(clitool.run(), gpsd.run())


def cli_main():
    """Console script entry point."""
    asyncio.run(main())


if __name__ == "__main__":
    cli_main()
