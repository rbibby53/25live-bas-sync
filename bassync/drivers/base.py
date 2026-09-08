# 25Live -> BAS Schedule Sync — driver interface
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
The contract every BAS integration implements.

A driver's whole job is: given a target and a list of occupancy windows, make
that schedule say "occupied exactly during these windows, unoccupied otherwise"
— idempotently, so running the sync twice is indistinguishable from running it
once.

Everything above this line (25Live, buffers, merging, building roll-ups) is
vendor-neutral. Everything vendor-specific lives in a driver.

Writing a new driver
--------------------
1. Subclass `ScheduleWriter`, set `name`, implement `write_schedule`.
2. Override `health_check` / `target_exists` so `--validate` can pre-flight it.
3. Register it in `bassync/drivers/__init__.py`.
4. Document the `target` string syntax in the module docstring — that string is
   what operators type into space_mapping.yaml.
"""

from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from ..model import OccupancyWindow


class DriverError(RuntimeError):
    """A driver could not complete an operation against its BAS."""


class ScheduleWriter(ABC):
    """Base class for BAS schedule integrations."""

    #: Value used in `systems: { <name>: { driver: <name> } }`.
    name: str = "base"

    #: One-line description shown by `--list-drivers`.
    description: str = ""

    def __init__(self, system_name: str, cfg: dict, tz: ZoneInfo,
                 retry: Optional[dict] = None):
        self.system_name = system_name
        self.cfg = cfg
        # Campus timezone. Drivers that speak in local wall-clock time (BACnet)
        # can override it per system for a building in another zone.
        self.tz = ZoneInfo(cfg["timezone"]) if cfg.get("timezone") else tz
        self.retry = retry

    # ── lifecycle ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Open any long-lived resource (a BACnet stack, a session token).
        Called once before the first write. Safe to call more than once."""

    def close(self) -> None:
        """Release whatever connect() opened. Always called, even after a
        failed run, so a driver must tolerate close() without connect()."""

    def __enter__(self) -> "ScheduleWriter":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ── pre-flight (used by --validate) ──────────────────────────────────────

    @abstractmethod
    def health_check(self) -> tuple[bool, str]:
        """(reachable, human-readable detail). Must not raise."""

    def target_exists(self, target: str) -> tuple[bool, str]:
        """
        (exists, detail) for one schedule target — catches a typo in the room
        map before a live run touches anything.

        The default is "unknown, assume present" so a driver that cannot
        cheaply check does not fail validation. Override where you can.
        """
        return True, "not checked (driver cannot verify targets)"

    # ── writing ──────────────────────────────────────────────────────────────

    @abstractmethod
    def write_schedule(self, target: str, windows: list) -> None:
        """
        Make `target` occupied exactly during `windows` and unoccupied
        otherwise. An empty list means "no bookings" and MUST clear the
        schedule rather than leave yesterday's occupancy in place.

        Idempotent: writing the same windows twice leaves the same result.
        Raises DriverError on failure.
        """

    def describe(self, target: str, windows: list) -> str:
        """
        What a live run would do to this target, for --dry-run output. The
        default lists the windows; drivers whose encoding differs materially
        from "one line per window" (BACnet groups by calendar date) override
        this so the preview shows what actually goes on the wire.
        """
        if not windows:
            return f"{target}: CLEAR (no bookings)"
        lines = [f"{target}: {len(windows)} window(s)"]
        lines.extend(f"      {w}" for w in windows)
        return "\n".join(lines)

    # ── optional extras ──────────────────────────────────────────────────────

    def write_heartbeat(self, stamp) -> None:
        """Stamp a 'last successful sync' point so the BAS itself can alarm if
        the nightly job stops running. Optional; the default does nothing."""

    @staticmethod
    def split_at_midnight(windows: list) -> list:
        """
        Split any window crossing local midnight into per-day pieces.

        BACnet exception schedules — and most vendor schedule editors — are
        keyed by calendar date, so an event running 22:00-01:00 has to become
        two entries. Drivers that are date-based call this first; ones that
        take absolute timestamps (the REST drivers) do not need it.
        """
        out: list = []
        for w in windows:
            start, end = w.start, w.end
            while start.date() < end.date():
                # Midnight at the START of the next local day. Built from the
                # date rather than by adding 24h so DST-shifted days (23h or
                # 25h) still land exactly on midnight.
                next_midnight = (start + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0)
                if next_midnight >= end:
                    break
                out.append(OccupancyWindow(start, next_midnight, list(w.source_event_ids)))
                start = next_midnight
            out.append(OccupancyWindow(start, end, list(w.source_event_ids)))
        return out
