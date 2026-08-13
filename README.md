# GPSCOT

GPSCOT feeds a Linux device's gpsd position to TAK clients as network GPS.
It emits Cursor on Target position events to `COT_URL` and can optionally
fan out raw NMEA sentences for WinTAK.

GPSCOT 2.0.1 and later rebuild the PyTAK client in-process with bounded
backoff when the destination is unavailable or local network policy is being
replaced. GNSS input remains live while the output transport reconnects.

Typical AryaOS use:

```sh
sudo apt install gpscot cockpit-gpscot
sudo systemctl enable --now gpscot
```

Configuration lives in `/etc/default/gpscot`:

- `COT_URL`: PyTAK destination, default `udp+broadcast://255.255.255.255:4349`
- `NMEA_TARGETS`: optional space-separated `host:port` targets for raw NMEA
- `GPSCOT_RATE`: update interval in seconds
- `GPSCOT_UID`: CoT UID, defaults to `GPSCOT-<hostname>`
- `GPSCOT_SOURCE_NAME`: source name in CoT remarks, defaults to hostname
- `GPSD_HOST` / `GPSD_PORT`: gpsd endpoint

Build packages:

```sh
make package
```

The Debian package installs:

- `/usr/bin/gpscot`
- `/etc/default/gpscot`
- `/lib/systemd/system/gpscot.service`

## License

Apache-2.0.
