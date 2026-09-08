# 25Live -> BAS Schedule Sync — BACnet/IP driver
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Vendor-neutral BACnet/IP driver — writes ASHRAE 135 Schedule objects.

This is the driver to reach for first on a mixed campus. Tridium Niagara,
Automated Logic WebCTRL and Schneider EcoStruxure Building Operation are all
BTL-listed and all expose standard Schedule objects (Object_Type 17), so one
code path drives every one of them. No vendor SDK, no per-version REST
contract to chase.

What it writes
--------------
The `Exception_Schedule` property (BACnetARRAY of BACnetSpecialEvent) —
the standard place for "this date is different from the normal week". The
weekly schedule and the schedule's default value are left alone, so the
building keeps its normal operating profile and this sync only ever adds the
booking overlay.

Encoding: ONE special event per calendar date, holding a list of time/value
pairs that alternate ON at each window start and OFF at each window end:

    2026-06-10   09:00 -> active,  11:30 -> inactive,
                 13:00 -> active,  17:00 -> inactive

That matters for real controllers. A naive "one special event per booking"
encoding blows past the Exception_Schedule array limits that field controllers
actually enforce (commonly 10-25 entries); grouping by date caps the array at
one entry per day of lookahead no matter how heavily booked the rooms are.

Target syntax (the `target:` in space_mapping.yaml)
--------------------------------------------------
    "12001:5"               device instance 12001, Schedule object instance 5
    "12001:5@10.4.2.30"     same, but skip Who-Is and talk to that address
    "12001:5@10.4.2.30:47808"   ...with an explicit UDP port

The address form is worth using on a big campus: it removes a broadcast
round-trip per schedule and works even where Who-Is does not cross a subnet.

Priorities — two different things, often confused
-------------------------------------------------
`event_priority` (1-16, default 16) is the BACnetSpecialEvent's own priority:
which *exception* wins when two cover the same moment. Lower number wins. We
default to 16, the lowest, so ANY exception an operator adds by hand — a
holiday, a shutdown, a special event — overrides the booking overlay. That is
the safe default for an automated writer.

It is NOT the priority array (1-16) that commandable outputs use, and not the
Schedule object's own `Priority_For_Writing` (which decides at what priority
the schedule commands its listed points). This driver never changes
`Priority_For_Writing` — that is the controls engineer's setting.

Requirements
------------
`pip install bacpypes3` (or `pip install -r requirements-bacnet.txt`). The
import is lazy, so a Niagara-only or WebCTRL-only site never needs it.
"""

import asyncio
import logging
import re
from importlib import import_module
from datetime import date as _date, datetime
from typing import Optional

from .base import DriverError, ScheduleWriter

# Default BACnet/IP UDP port (0xBAC0).
DEFAULT_BACNET_PORT = 47808

# BACnetSpecialEvent priority. 16 is the LOWEST exception priority, so any
# hand-entered exception at the panel beats the booking overlay.
DEFAULT_EVENT_PRIORITY = 16

# Seconds to wait for a Who-Is reply before giving up on a device.
WHO_IS_TIMEOUT = 5.0

# Seconds to allow for the BACnet stack to bind and come up. BACpypes3 retries
# a failed socket bind indefinitely, so without a ceiling a wrong
# `local_address` makes a nightly run hang forever instead of failing — which
# is strictly worse, because a hung job never alerts.
CONNECT_TIMEOUT = 10.0

# target: "<device>:<schedule>" with an optional "@address[:port]" suffix.
_TARGET_RE = re.compile(
    r"^\s*(?P<device>\d+)\s*:\s*(?P<schedule>\d+)\s*"
    r"(?:@\s*(?P<address>[^\s]+?)\s*)?$"
)


class BacnetTarget:
    """A parsed `device:schedule[@address]` target."""

    __slots__ = ("device_id", "schedule_instance", "address")

    def __init__(self, device_id: int, schedule_instance: int,
                 address: Optional[str] = None):
        self.device_id = device_id
        self.schedule_instance = schedule_instance
        self.address = address

    def __str__(self) -> str:
        base = f"{self.device_id}:{self.schedule_instance}"
        return f"{base}@{self.address}" if self.address else base


def parse_target(target: str) -> BacnetTarget:
    """Parse a target string, raising DriverError with a usable message."""
    m = _TARGET_RE.match(target or "")
    if not m:
        raise DriverError(
            f"Invalid BACnet target {target!r}. Expected "
            "'<device-instance>:<schedule-instance>', optionally "
            "'@<ip>[:<port>]' — e.g. '12001:5' or '12001:5@10.4.2.30'.")
    address = m.group("address")
    if address and ":" not in address:
        address = f"{address}:{DEFAULT_BACNET_PORT}"
    return BacnetTarget(int(m.group("device")), int(m.group("schedule")), address)


def windows_to_daily(windows: list, tz) -> "dict[_date, list[tuple[datetime, bool]]]":
    """
    Turn occupancy windows into { calendar date: [(local time, value), ...] }.

    This is the whole encoding decision, kept pure so it can be tested without
    a BACnet stack. Windows are split at local midnight, sorted, and collapsed
    into one alternating ON/OFF list per date.

    A window that ends exactly at the next midnight contributes no OFF entry:
    a BACnet time cannot be 24:00, and at midnight the controller re-evaluates
    against the next day's exception (or falls back to the weekly schedule)
    anyway, so the trailing OFF would be both illegal and redundant.
    """
    by_date: dict = {}
    for w in ScheduleWriter.split_at_midnight(windows):
        start = w.start.astimezone(tz)
        end = w.end.astimezone(tz)
        if end <= start:
            continue
        day = start.date()
        entries = by_date.setdefault(day, [])
        entries.append((start, True))
        # end.date() != day means the piece runs to exactly midnight.
        if end.date() == day:
            entries.append((end, False))
    for day in by_date:
        by_date[day].sort(key=lambda tv: (tv[0], not tv[1]))
    return by_date


class BacnetScheduleWriter(ScheduleWriter):
    """Writes BACnet Schedule Exception_Schedule over BACnet/IP."""

    name = "bacnet"
    description = ("Standard BACnet/IP Schedule objects — works with Niagara, "
                   "WebCTRL, EcoStruxure and any BTL-listed controller.")

    def __init__(self, system_name: str, cfg: dict, tz, retry=None):
        super().__init__(system_name, cfg, tz, retry)
        # This machine's BACnet identity. `local_address` must be a real NIC
        # address on this host with its prefix length, e.g. "10.4.1.55/24".
        self.local_address = cfg.get("local_address") or ""
        self.device_id = int(cfg.get("device_id", 599001))
        self.device_name = cfg.get("device_name") or "25Live-BAS-Sync"
        self.vendor_id = int(cfg.get("vendor_identifier", 999))
        # Foreign-device registration: needed whenever this host is not on the
        # same subnet as the controllers (the usual case for a server in a
        # data centre writing to field panels).
        self.bbmd_address = cfg.get("bbmd_address") or ""
        self.bbmd_ttl = int(cfg.get("bbmd_ttl_seconds", 900))
        self.event_priority = int(cfg.get("event_priority", DEFAULT_EVENT_PRIORITY))
        # 0 = no cap. Set it to your controllers' documented Exception_Schedule
        # limit and the driver truncates to the nearest days instead of letting
        # the controller reject the whole write.
        self.max_special_events = int(cfg.get("max_special_events", 0))
        self.verify_writes = bool(cfg.get("verify_writes", True))
        self.who_is_timeout = float(cfg.get("who_is_timeout", WHO_IS_TIMEOUT))
        self.connect_timeout = float(cfg.get("connect_timeout", CONNECT_TIMEOUT))
        if not 1 <= self.event_priority <= 16:
            raise DriverError(
                f"System '{system_name}': event_priority must be 1-16, "
                f"got {self.event_priority}.")

        self._loop = None
        self._app = None
        self._address_cache: dict = {}

    # ── lifecycle ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """
        Stand up the BACnet stack on its own event loop.

        bacpypes3 is asyncio-native while the rest of the sync is plain
        synchronous code. Rather than colour the whole program async for one
        driver, the loop lives here and each public method drives it to
        completion — the stack stays up for the whole run, so we register with
        the BBMD once rather than per schedule.
        """
        if self._app is not None:
            return
        if not self.local_address:
            raise DriverError(
                f"System '{self.system_name}': `local_address` is required for "
                "the bacnet driver — set it to this host's NIC address and "
                "prefix, e.g. \"10.4.1.55/24\".")
        # Fail here, with a fixable message, rather than deep inside the
        # first write at 2 AM.
        try:
            import_module("bacpypes3")
        except ImportError as exc:
            raise DriverError(
                "The bacnet driver needs BACpypes3. Install it with "
                "`pip install bacpypes3` (or "
                "`pip install -r requirements-bacnet.txt`).") from exc

        # Check the address ourselves first. BACpypes3 would retry the bind in
        # a loop and never surface the reason; this names it immediately, and
        # lists what the host actually has so the fix is obvious.
        _check_local_address(self.local_address, self.system_name)

        self._loop = asyncio.new_event_loop()
        try:
            self._app = self._loop.run_until_complete(
                asyncio.wait_for(self._build_app(), timeout=self.connect_timeout))
        except asyncio.TimeoutError as exc:
            self._close_loop()
            raise DriverError(
                f"System '{self.system_name}': the BACnet stack did not come up "
                f"within {self.connect_timeout:g}s on {self.local_address}. "
                "Check that the address is free (nothing else is bound to UDP "
                f"{DEFAULT_BACNET_PORT}) and that the interface is up."
            ) from exc
        except Exception as exc:
            self._close_loop()
            raise DriverError(f"Could not start the BACnet stack: {exc}") from exc
        logging.info("BACnet stack up on %s as device %d (%s)",
                     self.local_address, self.device_id,
                     f"foreign device via BBMD {self.bbmd_address}"
                     if self.bbmd_address else "same-subnet broadcast")

    async def _build_app(self):
        from bacpypes3.app import Application
        from bacpypes3.basetypes import HostNPort
        from bacpypes3.local.device import DeviceObject
        from bacpypes3.local.networkport import NetworkPortObject

        address = self.local_address
        if ":" not in address.rsplit("/", 1)[-1]:
            address = f"{address}:{DEFAULT_BACNET_PORT}"

        device = DeviceObject(
            objectIdentifier=("device", self.device_id),
            objectName=self.device_name,
            vendorIdentifier=self.vendor_id,
        )
        port = NetworkPortObject(
            address,
            objectIdentifier=("network-port", 1),
            objectName="NetworkPort-1",
        )
        if self.bbmd_address:
            host, _, port_txt = self.bbmd_address.partition(":")
            port.bacnetIPMode = "foreign"
            port.fdBBMDAddress = HostNPort(
                host=dict(ipAddress=_ip_bytes(host)),
                port=int(port_txt or DEFAULT_BACNET_PORT),
            )
            port.fdSubscriptionLifetime = self.bbmd_ttl
        return Application.from_object_list([device, port])

    def close(self) -> None:
        if self._app is not None:
            try:
                self._app.close()
            except Exception as exc:                      # noqa: BLE001
                logging.debug("BACnet app close: %s", exc)
            self._app = None
        self._close_loop()

    def _close_loop(self) -> None:
        if self._loop is None:
            return
        try:
            # A timed-out bind leaves BACpypes3's retry task pending; cancel it
            # so closing the loop doesn't warn about a task that never finished.
            for task in asyncio.all_tasks(self._loop):
                task.cancel()
            self._loop.run_until_complete(asyncio.sleep(0))
        except Exception as exc:                          # noqa: BLE001
            logging.debug("BACnet loop drain: %s", exc)
        try:
            self._loop.close()
        except Exception as exc:                          # noqa: BLE001
            logging.debug("BACnet loop close: %s", exc)
        self._loop = None

    def _run(self, coro):
        if self._loop is None:
            self.connect()
        return self._loop.run_until_complete(coro)

    # ── addressing ───────────────────────────────────────────────────────────

    async def _resolve(self, tgt: BacnetTarget):
        """Address for a device: the pinned one, else a cached/fresh Who-Is."""
        from bacpypes3.pdu import Address
        if tgt.address:
            return Address(tgt.address)
        cached = self._address_cache.get(tgt.device_id)
        if cached is not None:
            return cached
        found = await self._app.who_is(tgt.device_id, tgt.device_id,
                                       timeout=self.who_is_timeout)
        if not found:
            raise DriverError(
                f"BACnet device {tgt.device_id} did not answer Who-Is. Pin its "
                f"address in the target (e.g. '{tgt.device_id}:"
                f"{tgt.schedule_instance}@10.4.2.30') or check BBMD/foreign-"
                "device registration.")
        address = found[0].pduSource
        self._address_cache[tgt.device_id] = address
        return address

    # ── pre-flight ───────────────────────────────────────────────────────────

    def health_check(self) -> tuple[bool, str]:
        try:
            self.connect()
        except DriverError as exc:
            return False, str(exc)
        return True, (f"BACnet device {self.device_id} bound to "
                      f"{self.local_address}"
                      + (f", foreign device on BBMD {self.bbmd_address}"
                         if self.bbmd_address else ""))

    def target_exists(self, target: str) -> tuple[bool, str]:
        """Read the Schedule object's name — proves it resolves, is really a
        Schedule, and that we can talk to the device."""
        try:
            tgt = parse_target(target)
            name = self._run(self._read_object_name(tgt))
        except DriverError as exc:
            return False, str(exc)
        except Exception as exc:                          # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        return True, f"schedule '{name}'"

    async def _read_object_name(self, tgt: BacnetTarget) -> str:
        address = await self._resolve(tgt)
        value = await self._app.read_property(
            address, f"schedule,{tgt.schedule_instance}", "object-name")
        return str(value)

    # ── writing ──────────────────────────────────────────────────────────────

    def write_schedule(self, target: str, windows: list) -> None:
        tgt = parse_target(target)
        try:
            self._run(self._write(tgt, windows))
        except DriverError:
            raise
        except Exception as exc:                          # noqa: BLE001
            raise DriverError(
                f"BACnet write to {tgt} failed: {type(exc).__name__}: {exc}") from exc

    async def _write(self, tgt: BacnetTarget, windows: list) -> None:
        from bacpypes3.basetypes import (CalendarEntry, SpecialEvent,
                                         SpecialEventPeriod, TimeValue)
        from bacpypes3.constructeddata import ArrayOf
        from bacpypes3.primitivedata import Boolean, Time

        address = await self._resolve(tgt)
        by_date = windows_to_daily(windows, self.tz)

        special_events = []
        for day in sorted(by_date):
            time_values = [
                TimeValue(time=Time((dt.hour, dt.minute, dt.second,
                                     dt.microsecond // 10000)),
                          value=Boolean(value))
                for dt, value in by_date[day]
            ]
            special_events.append(SpecialEvent(
                period=SpecialEventPeriod(
                    calendarEntry=CalendarEntry(date=_bacnet_date(day))),
                listOfTimeValues=time_values,
                eventPriority=self.event_priority,
            ))

        if self.max_special_events and len(special_events) > self.max_special_events:
            dropped = len(special_events) - self.max_special_events
            logging.warning(
                "%s: %d special events exceeds max_special_events=%d — keeping "
                "the %d nearest days and dropping the %d furthest out. Shorten "
                "lookahead_days or raise the cap once you have confirmed the "
                "controller's real limit.",
                tgt, len(special_events), self.max_special_events,
                self.max_special_events, dropped)
            special_events = special_events[:self.max_special_events]

        array_type = ArrayOf(SpecialEvent)
        # Writing the whole array in one WriteProperty replaces the previous
        # run's overlay atomically. An empty array is the clear — which is why
        # "no bookings" writes [] rather than skipping the target.
        await self._app.write_property(
            address, f"schedule,{tgt.schedule_instance}",
            "exception-schedule", array_type(special_events))

        if self.verify_writes:
            await self._verify(tgt, address, len(special_events))

        logging.info("Wrote %d special event(s) covering %d day(s) to %s",
                     len(special_events), len(by_date), tgt)

    async def _verify(self, tgt: BacnetTarget, address, expected: int) -> None:
        """Read Exception_Schedule back and confirm the array took.

        Some controllers accept a WriteProperty and quietly ignore it (schedule
        locked by the vendor tool, object out of service, insufficient
        privilege). Without a read-back the sync would report success while the
        building never changes.
        """
        try:
            readback = await self._app.read_property(
                address, f"schedule,{tgt.schedule_instance}", "exception-schedule")
        except Exception as exc:                          # noqa: BLE001
            logging.warning("%s: wrote but could not read back to verify (%s). "
                            "Set verify_writes: false to silence this.", tgt, exc)
            return
        actual = len(readback) if readback is not None else 0
        if actual != expected:
            raise DriverError(
                f"{tgt}: wrote {expected} special event(s) but read back "
                f"{actual}. The controller may be rejecting the write — check "
                "that the schedule is not locked by the vendor tool and that "
                "this device has write privilege.")

    def describe(self, target: str, windows: list) -> str:
        """Preview the actual per-date encoding, not just the windows."""
        try:
            tgt = parse_target(target)
        except DriverError as exc:
            return f"{target}: INVALID — {exc}"
        by_date = windows_to_daily(windows, self.tz)
        if not by_date:
            return f"{tgt}: CLEAR Exception_Schedule (no bookings)"
        lines = [f"{tgt}: {len(by_date)} special event(s), "
                 f"eventPriority={self.event_priority}"]
        for day in sorted(by_date):
            pairs = ", ".join(
                f"{dt.strftime('%H:%M')}->{'ON' if val else 'OFF'}"
                for dt, val in by_date[day])
            lines.append(f"      {day.isoformat()}  {pairs}")
        return "\n".join(lines)


def _bacnet_date(day: _date):
    """A python date as a BACnet Date (year offset from 1900, 1-based weekday)."""
    from bacpypes3.primitivedata import Date
    return Date((day.year - 1900, day.month, day.day, day.isoweekday()))


def _check_local_address(local_address: str, system_name: str) -> None:
    """
    Fail fast, and usefully, when `local_address` is not this host's.

    Binding a throwaway UDP socket is the portable way to ask "is this address
    mine?" — it does not depend on enumerating interfaces, which differs across
    platforms. The port is left at 0 so this never collides with a BACnet stack
    already running here.
    """
    import socket
    ip = local_address.split("/", 1)[0].split(":", 1)[0].strip()
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((ip, 0))
    except OSError as exc:
        raise DriverError(
            f"System '{system_name}': local_address {local_address!r} is not an "
            f"address on this host ({exc.strerror or exc}). It must be this "
            "machine's own NIC address with its prefix length, e.g. "
            f"\"10.4.1.55/24\". Addresses available here: "
            f"{', '.join(_host_addresses()) or 'none found'}.") from exc
    finally:
        probe.close()


def _host_addresses() -> list:
    """
    This host's IPv4 addresses, for the error message above.

    Two sources, because neither alone is reliable: resolving the hostname
    misses addresses on a box with no DNS entry for itself, and the
    connect-to-a-remote trick only ever reveals the primary outbound
    interface — which is usually, but not always, the one you want here.
    No packets are sent by the UDP connect.
    """
    import socket
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))     # TEST-NET-1: reserved, unroutable
        found.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    return sorted(a for a in found if not a.startswith("127."))


def _ip_bytes(host: str) -> bytes:
    """Dotted-quad (or resolvable hostname) to the 4 bytes a HostNPort wants."""
    import socket
    return socket.inet_aton(socket.gethostbyname(host))
