#!/usr/bin/env python3
"""
25Live -> Niagara N4 Schedule Sync  (daily cron edition)
=========================================================
Runs ONCE per invocation. Designed to be triggered on a schedule (2am daily):

    Windows (Task Scheduler — this script runs on the Niagara 4.15 server, D:\\BAS):
        Program:   python.exe
        Arguments: D:\\BAS\\main.py
        Start in:  D:\\BAS
        Trigger:   Daily, 02:00

    Linux/macOS (cron), if deployed off-host:
        0 2 * * *  /usr/bin/python3 /opt/bas/main.py

On each run it:
  1. Reads the space map from space_mapping.yaml
  2. Pulls the next 7 days of confirmed events from the 25Live
     Series25 WebServices API (XML)
  3. Applies per-space pre-conditioning / post-buffer offsets
  4. Merges overlapping/adjacent events into clean occupancy windows
  5. Rolls room events up into building-level schedules
  6. Writes them to Niagara N4 as SpecialEvents at BACnet priority 14
  7. Writes a heartbeat timestamp to Niagara for monitoring
  8. Exits 0 on success, non-zero on failure (so cron/monitoring can alert)

Quick start:
    pip install -r requirements.txt          # requests, python-dateutil, PyYAML
    set BAS_25LIVE_PASSWORD=...               # (export ... on Linux/macOS)
    set BAS_NIAGARA_PASSWORD=...
    python main.py --dry-run                  # fetch + build, no writes

Files expected alongside this script:
    space_mapping.yaml      (the 25Live space_id -> Niagara path cross-reference)

Author: Facilities / BAS Integration
"""

import os
import sys
import logging
import argparse
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import requests
import yaml
from dateutil import parser as dateparser


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS  — named values used throughout, gathered here so behavioural
# tweaks are a one-line change instead of a hunt-and-replace.
# ─────────────────────────────────────────────────────────────────────────────

# BACnet scheduling priority for our writes. Priority 14 leaves 8 free for
# operator overrides at the panel. Lower number = higher precedence.
BACNET_SCHEDULE_PRIORITY = 14

# The value we push for an occupied window (True = Occupied on a BooleanSchedule).
OCCUPIED_VALUE = True

# Niagara REST API base path under the station host. Targeting N4.15; the N4
# web service is commonly "/rest/v1". Confirm for your station and change here
# if it differs (e.g. a different version or a custom servlet mount).
NIAGARA_REST_BASE = "/rest/v1"

# 25Live pagination: events returned per API page.
PAGE_SIZE = 100

# 25Live: how many space IDs to request per call. Keeps the query string under
# typical URL-length limits when the space map is large.
SPACE_IDS_PER_REQUEST = 50

# Sentinel left in CONFIG so a forgotten password is obvious (and warned about)
# rather than silently sending "CHANGE_ME" as a credential.
PLACEHOLDER_PASSWORD = "CHANGE_ME"

# HTTP timeouts (seconds).
HTTP_TIMEOUT_FETCH = 60      # 25Live event pulls can be large
HTTP_TIMEOUT_WRITE = 20      # individual Niagara writes
HTTP_TIMEOUT_HEALTH = 15     # health check / heartbeat

# 25Live XML namespace — every element lives under this.
R25_NS = {"r25": "http://www.collegenet.com/r25"}


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION  — edit these for your environment.
#
# Secrets: leave passwords as PLACEHOLDER_PASSWORD here and set them via
# environment variables instead (see load_credentials()):
#     BAS_25LIVE_PASSWORD     -> collegenet.password
#     BAS_NIAGARA_PASSWORD    -> niagara.password
# ─────────────────────────────────────────────────────────────────────────────

CONFIG = {
    # ── CollegeNet 25Live Series25 WebServices (XML API) ──
    "collegenet": {
        # Kennesaw is CollegeNet-hosted, so the base URL is:
        "base_url": "https://webservices.collegenet.com/r25ws/wrd/kennesaw/run",
        "username": "svc-bas-scheduler",      # local 25Live account (not SSO)
        "password": PLACEHOLDER_PASSWORD,     # set env var BAS_25LIVE_PASSWORD
        "lookahead_days": 7,                  # pull the next 7 days
        "include_states": [2],                # event states to sync: 2=confirmed
                                              #   (add 4 for tentative)
        "default_pre_condition_minutes": 30,  # fallback if space has no override
        "default_post_buffer_minutes": 15,    # fallback if space has no override
        "merge_gap_minutes": 5,               # default gap for collapsing a
                                              #   space's windows; a room/building
                                              #   may override per entry. Merges
                                              #   ACROSS rooms (building roll-ups)
                                              #   use this default.
    },

    # ── Niagara N4 station (REST/HTTP API) ──
    "niagara": {
        "host": "niagaraprdweb01.win.kennesaw.edu",  # same VM as script; cert matches this FQDN. Fallbacks: 10.54.40.79 / localhost
        "port": 443,                          # CONFIRM — may be 8443
        "https": True,
        "username": "svc-scheduler",          # local Niagara operator account
        "password": PLACEHOLDER_PASSWORD,     # set env var BAS_NIAGARA_PASSWORD
        "verify_tls": False,                  # set True + provide CA bundle in prod
        "schedule_base_path": "slot:/Schedules",
        "heartbeat_path": "slot:/Schedules/_LastSync",  # for monitoring
    },

    "timezone": "America/New_York",

    # Path to the space map YAML (same folder as this script by default).
    "space_map_file": str(Path(__file__).parent / "space_mapping.yaml"),

    # Log file. Defaults are OS-aware (see default_log_file); override here if
    # you want a specific location.
    "log_file": None,   # None -> default_log_file() picks a sensible path
}


def default_log_file() -> str:
    """
    Pick a sensible log path for the host OS:
      Windows -> D:\\BAS\\logs\\25live_sync.log   (deployed alongside the script)
      else    -> /var/log/bas/25live_sync.log
    setup_logging() falls back to stdout-only if the directory isn't writable,
    so this never blocks a run.
    """
    if os.name == "nt":
        base = Path(r"D:\BAS\logs")
    else:
        base = Path("/var/log/bas")
    return str(base / "25live_sync.log")


def load_credentials(config: dict) -> None:
    """
    Override passwords from environment variables if present, and warn loudly if
    a password is still the placeholder. Keeps secrets out of the source file.

        export BAS_25LIVE_PASSWORD=...
        export BAS_NIAGARA_PASSWORD=...
    """
    env_overrides = {
        "collegenet": "BAS_25LIVE_PASSWORD",
        "niagara": "BAS_NIAGARA_PASSWORD",
    }
    for section, env_var in env_overrides.items():
        value = os.environ.get(env_var)
        if value:
            config[section]["password"] = value
        if config[section]["password"] == PLACEHOLDER_PASSWORD:
            logging.warning(
                "%s password is still the placeholder — set %s before a live run.",
                section, env_var,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OccupancyWindow:
    """A single continuous occupied window after merging overlapping events."""
    start: datetime
    end: datetime
    source_event_ids: list[str] = field(default_factory=list)

    def overlaps_or_adjacent(self, other: "OccupancyWindow", gap_minutes: int) -> bool:
        gap = timedelta(minutes=gap_minutes)
        return self.start <= other.end + gap and other.start <= self.end + gap

    def merge(self, other: "OccupancyWindow") -> "OccupancyWindow":
        return OccupancyWindow(
            start=min(self.start, other.start),
            end=max(self.end, other.end),
            source_event_ids=self.source_event_ids + other.source_event_ids,
        )

    def __repr__(self) -> str:
        return (f"OccupancyWindow({self.start.strftime('%a %m/%d %H:%M')}"
                f"-{self.end.strftime('%H:%M')})")


@dataclass
class RawEvent:
    """One space assignment parsed from a 25Live reservation, buffers applied."""
    event_id: str
    title: str
    space_id: str
    start: datetime          # effective start (after pre-conditioning)
    end: datetime            # effective end (after post-buffer)


@dataclass
class SpaceConfig:
    """One row from space_mapping.yaml."""
    space_id: str
    space_name: str
    space_type: str                       # "room" or "building"
    niagara_path: str
    building_schedule_path: Optional[str]
    pre_condition_minutes: int
    post_buffer_minutes: int
    merge_gap_minutes: int                # collapse this space's windows within
                                          # this gap (per-space; falls back to
                                          # the global default)


# ─────────────────────────────────────────────────────────────────────────────
# Space map loader
# ─────────────────────────────────────────────────────────────────────────────

def load_space_map(path: str, cfg: dict) -> dict[str, SpaceConfig]:
    """
    Parse space_mapping.yaml into { space_id: SpaceConfig }.

    The YAML has two sections:
        buildings:  each building's roll-up schedule, defined ONCE.
        spaces:     the rooms; each room names the building it belongs to.

    Every room that names a building is automatically unioned into that
    building's occupancy schedule by ScheduleBuilder — so if ANY room in the
    building is occupied, the building schedule (hallways, lobbies, common AHUs)
    is occupied too. You never repeat the building's Niagara path on a room;
    you just give the building id, which makes "all rooms in the building" the
    default and removes the chance of forgetting one.

    Per-room pre/post overrides fall back to the global defaults.
    """
    default_pre = cfg["collegenet"]["default_pre_condition_minutes"]
    default_post = cfg["collegenet"]["default_post_buffer_minutes"]
    default_gap = cfg["collegenet"]["merge_gap_minutes"]

    with open(path, "r") as fh:
        data = yaml.safe_load(fh) or {}

    # 1) Index the building definitions by their id.
    buildings: dict[str, dict] = {str(b["id"]): b for b in data.get("buildings", [])}

    space_map: dict[str, SpaceConfig] = {}

    # 2) Rooms — resolve each room's building id to that building's Niagara path
    #    so the existing roll-up logic unions every room in the building.
    for row in data.get("spaces", []):
        space_id = str(row["space_id"])
        building_path: Optional[str] = None
        building_id = row.get("building")
        if building_id is not None:
            building_id = str(building_id)
            building = buildings.get(building_id)
            if building is None:
                logging.warning(
                    "Room %s references unknown building '%s' — it will NOT roll "
                    "up. Add it under buildings: or fix the name.",
                    space_id, building_id)
            else:
                building_path = building["niagara_path"]

        space_map[space_id] = SpaceConfig(
            space_id=space_id,
            space_name=row.get("space_name", space_id),
            space_type="room",
            niagara_path=row["niagara_path"],
            building_schedule_path=building_path,
            pre_condition_minutes=row.get("pre_condition_minutes") or default_pre,
            post_buffer_minutes=row.get("post_buffer_minutes") or default_post,
            merge_gap_minutes=row.get("merge_gap_minutes") or default_gap,
        )

    # 3) Buildings that are themselves bookable in 25Live (e.g. an atrium):
    #    add them as a directly-booked space so their own events also count
    #    toward the building schedule, alongside the room roll-up.
    for building_id, b in buildings.items():
        if b.get("space_id") is None:
            continue
        space_id = str(b["space_id"])
        space_map[space_id] = SpaceConfig(
            space_id=space_id,
            space_name=b.get("name", building_id),
            space_type="building",
            niagara_path=b["niagara_path"],
            building_schedule_path=None,
            pre_condition_minutes=b.get("pre_condition_minutes") or default_pre,
            post_buffer_minutes=b.get("post_buffer_minutes") or default_post,
            merge_gap_minutes=b.get("merge_gap_minutes") or default_gap,
        )

    n_rooms = sum(1 for s in space_map.values() if s.space_type == "room")
    logging.info("Loaded %d rooms across %d buildings from %s",
                 n_rooms, len(buildings), path)
    return space_map


# ─────────────────────────────────────────────────────────────────────────────
# 25Live Series25 WebServices client (XML)
# ─────────────────────────────────────────────────────────────────────────────

class CollegeNetClient:
    """
    Reads events from the Series25 WebServices XML API.

    Endpoint:  GET {base_url}/events.xml
    Auth:      HTTP Basic over HTTPS (local service account)
    Response:  XML in the http://www.collegenet.com/r25 namespace
    """

    def __init__(self, cfg: dict, tz: ZoneInfo):
        self.base_url = cfg["base_url"].rstrip("/")
        self.lookahead_days = cfg["lookahead_days"]
        self.include_states = set(cfg["include_states"])
        self.merge_gap = cfg["merge_gap_minutes"]
        self.tz = tz
        self.session = requests.Session()
        self.session.auth = (cfg["username"], cfg["password"])
        self.session.headers.update({"Accept": "application/xml"})

    @staticmethod
    def _local(tag: str) -> str:
        """Strip the namespace prefix from an XML tag."""
        return tag.rsplit("}", 1)[-1]

    @staticmethod
    def _chunk(items: list[str], size: int) -> list[list[str]]:
        """Split a list into chunks of at most `size`."""
        return [items[i:i + size] for i in range(0, len(items), size)]

    def _find_space_ids(self, reservation: ET.Element) -> list[str]:
        """
        Robustly collect every space_id found anywhere under a reservation.
        Handles API-version differences in how deeply space_reservation is
        nested. An event using multiple rooms yields multiple IDs.
        """
        ids: list[str] = []
        for elem in reservation.iter():
            if self._local(elem.tag) == "space_id" and elem.text:
                ids.append(elem.text.strip())
        return ids

    def fetch_events(self, space_map: dict[str, SpaceConfig]) -> list[RawEvent]:
        """
        Fetch all configured-state events for mapped spaces over the lookahead
        window. Returns a list of RawEvent with per-space buffers already
        applied. Space IDs are requested in batches (SPACE_IDS_PER_REQUEST) to
        keep the query string within URL-length limits.
        """
        now = datetime.now(self.tz)
        end = now + timedelta(days=self.lookahead_days)
        all_space_ids = list(space_map.keys())

        raw_events: list[RawEvent] = []
        for batch in self._chunk(all_space_ids, SPACE_IDS_PER_REQUEST):
            raw_events.extend(self._fetch_batch(batch, now, end, space_map))

        logging.info("Fetched %d space-assignments from 25Live", len(raw_events))
        return raw_events

    def _fetch_batch(self, space_ids: list[str], start: datetime, end: datetime,
                     space_map: dict[str, SpaceConfig]) -> list[RawEvent]:
        """Fetch one batch of space IDs, paging until exhausted."""
        raw_events: list[RawEvent] = []
        offset = 0

        while True:
            params = {
                "space_id": " ".join(space_ids),     # space-separated IDs
                "start_dt": start.strftime("%Y-%m-%dT00:00:00"),
                "end_dt":   end.strftime("%Y-%m-%dT23:59:59"),
                "scope":    "extended",               # include setup/pre-event times
                # NOTE: Series25 filters confirmed events by the numeric `state`
                # query param, not include=confirmed. We derive it from
                # include_states so CONFIG actually drives the request. If your
                # 25Live instance expects a different format (e.g. comma- vs
                # plus-separated), this is the one line to adjust.
                "state":    "+".join(str(s) for s in sorted(self.include_states)),
                "page_size": PAGE_SIZE,
                "page_offset": offset,
            }

            url = f"{self.base_url}/events.xml"
            resp = self.session.get(url, params=params, timeout=HTTP_TIMEOUT_FETCH)
            resp.raise_for_status()

            root = ET.fromstring(resp.text)
            events = root.findall("r25:event", R25_NS)

            for ev in events:
                try:
                    raw_events.extend(self._parse_event(ev, space_map))
                except Exception as exc:
                    eid = ev.findtext("r25:event_id", "?", R25_NS)
                    logging.warning("Skipping malformed event %s: %s", eid, exc)

            total = int(root.findtext("r25:total_count", "0", R25_NS) or 0)
            returned = int(root.findtext("r25:return_count",
                                         str(len(events)), R25_NS) or len(events))

            if offset + returned >= total or returned == 0:
                break
            offset += PAGE_SIZE

        return raw_events

    def _parse_event(self, ev: ET.Element,
                     space_map: dict[str, SpaceConfig]) -> list[RawEvent]:
        """Turn one <r25:event> into RawEvents (one per mapped space)."""
        event_id = ev.findtext("r25:event_id", namespaces=R25_NS) or "?"
        title = ev.findtext("r25:event_name", namespaces=R25_NS) or "Unnamed"

        # Optional state filter (defensive — we already request our states only)
        state_txt = ev.findtext("r25:state", namespaces=R25_NS)
        if state_txt and self.include_states and int(state_txt) not in self.include_states:
            return []

        results: list[RawEvent] = []

        reservations = ev.find("r25:reservations", R25_NS)
        if reservations is None:
            return []

        for res in reservations.findall("r25:reservation", R25_NS):
            start_txt = res.findtext("r25:event_start_dt", namespaces=R25_NS)
            end_txt   = res.findtext("r25:event_end_dt",   namespaces=R25_NS)
            if not (start_txt and end_txt):
                continue

            event_start = dateparser.parse(start_txt).astimezone(self.tz)
            event_end   = dateparser.parse(end_txt).astimezone(self.tz)

            # 25Live's own setup/teardown times (present with scope=extended)
            native_pre_txt  = (res.findtext("r25:setup_dt", namespaces=R25_NS)
                               or res.findtext("r25:pre_event_dt", namespaces=R25_NS))
            native_post_txt = (res.findtext("r25:takedown_dt", namespaces=R25_NS)
                               or res.findtext("r25:post_event_dt", namespaces=R25_NS))
            native_pre  = (dateparser.parse(native_pre_txt).astimezone(self.tz)
                           if native_pre_txt else event_start)
            native_post = (dateparser.parse(native_post_txt).astimezone(self.tz)
                           if native_post_txt else event_end)

            # Which mapped spaces does this reservation touch?
            for space_id in self._find_space_ids(res):
                if space_id not in space_map:
                    continue
                sc = space_map[space_id]

                # Effective window = earliest of (25Live setup, event - precond)
                #                    to latest of (25Live takedown, event + postbuf)
                configured_start = event_start - timedelta(minutes=sc.pre_condition_minutes)
                configured_end   = event_end + timedelta(minutes=sc.post_buffer_minutes)
                effective_start = min(native_pre, configured_start)
                effective_end   = max(native_post, configured_end)

                results.append(RawEvent(
                    event_id=event_id,
                    title=title,
                    space_id=space_id,
                    start=effective_start,
                    end=effective_end,
                ))

        return results


# ─────────────────────────────────────────────────────────────────────────────
# Schedule builder (merge + building roll-up)
# ─────────────────────────────────────────────────────────────────────────────

class ScheduleBuilder:
    def __init__(self, default_merge_gap_minutes: int):
        # Used for building roll-ups (which span multiple rooms) and as the
        # fallback when a space doesn't override merge_gap_minutes.
        self.default_merge_gap = default_merge_gap_minutes

    def build(self, events: list[RawEvent],
              space_map: dict[str, SpaceConfig]) -> dict[str, list[OccupancyWindow]]:
        """Returns { niagara_path: [OccupancyWindow, ...] }."""
        by_space: dict[str, list[RawEvent]] = defaultdict(list)
        for ev in events:
            by_space[ev.space_id].append(ev)

        result: dict[str, list[OccupancyWindow]] = {}
        building_windows: dict[str, list[OccupancyWindow]] = defaultdict(list)

        for space_id, evs in by_space.items():
            sc = space_map[space_id]
            # Each space merges its own windows with its own (possibly
            # overridden) gap.
            windows = self._merge([
                OccupancyWindow(e.start, e.end, [e.event_id])
                for e in sorted(evs, key=lambda e: e.start)
            ], sc.merge_gap_minutes)

            if sc.space_type == "room":
                result[sc.niagara_path] = windows
                if sc.building_schedule_path:
                    building_windows[sc.building_schedule_path].extend(windows)
            elif sc.space_type == "building":
                # Direct building booking (e.g. a lobby event)
                building_windows[sc.niagara_path].extend(windows)

        # Merge the building roll-ups (rooms + any direct building bookings).
        # The inputs are each space's already gap-merged windows; here we merge
        # ACROSS spaces using the global default gap. A room's own gap override
        # governs only its own windows — but those windows still carry into the
        # building contribution, so a room kept "occupied" across a gap keeps
        # the building occupied too.
        for bpath, windows in building_windows.items():
            merged = self._merge(sorted(windows, key=lambda w: w.start),
                                 self.default_merge_gap)
            # If the building path was also a room result, union them
            if bpath in result:
                merged = self._merge(sorted(result[bpath] + merged,
                                            key=lambda w: w.start),
                                     self.default_merge_gap)
            result[bpath] = merged

        total = sum(len(v) for v in result.values())
        logging.info("Built %d windows across %d schedules", total, len(result))
        return result

    def _merge(self, windows: list[OccupancyWindow],
               gap_minutes: int) -> list[OccupancyWindow]:
        if not windows:
            return []
        merged = [windows[0]]
        for w in windows[1:]:
            if merged[-1].overlaps_or_adjacent(w, gap_minutes):
                merged[-1] = merged[-1].merge(w)
            else:
                merged.append(w)
        return merged


# ─────────────────────────────────────────────────────────────────────────────
# Niagara N4 client (REST)
# ─────────────────────────────────────────────────────────────────────────────

class NiagaraClient:
    """
    Writes SpecialEvents to Niagara N4 BooleanSchedules via the REST API.
    Each write carries BACNET_SCHEDULE_PRIORITY (14), leaving 8 free for
    operator overrides. Uses a clear-then-write pattern so each run is
    idempotent.

    TARGET: Niagara 4.15. The exact REST surface depends on which web service
    the station runs and its version:
      - base path (NIAGARA_REST_BASE, default "/rest/v1"),
      - the SpecialEvent JSON shape in _write_special_event,
      - the schedule slot ORD style (slot:/Schedules/...).
    These are gathered/flagged so they're a one-line change if your 4.15
    station's API differs. Confirm against the station's REST docs (or the
    Workbench "rest" service) and run --dry-run first — it never calls these.
    """

    def __init__(self, cfg: dict, tz: ZoneInfo):
        proto = "https" if cfg["https"] else "http"
        self.base = f"{proto}://{cfg['host']}:{cfg['port']}{NIAGARA_REST_BASE}"
        self.schedule_base = cfg["schedule_base_path"]
        self.heartbeat_path = cfg.get("heartbeat_path")
        self.tz = tz
        self.session = requests.Session()
        self.session.auth = (cfg["username"], cfg["password"])
        self.session.verify = cfg.get("verify_tls", False)
        if not self.session.verify:
            logging.warning(
                "Niagara TLS verification is DISABLED (verify_tls=False). "
                "Acceptable for a self-signed station cert on a trusted network; "
                "for production, set verify_tls to a CA-bundle path.")
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def health_check(self) -> bool:
        try:
            r = self.session.get(f"{self.base}/about", timeout=HTTP_TIMEOUT_HEALTH)
            return r.status_code == 200
        except requests.RequestException as exc:
            logging.error("Niagara health check failed: %s", exc)
            return False

    def write_schedule(self, niagara_path: str, windows: list[OccupancyWindow]) -> None:
        full = f"{self.schedule_base}/{niagara_path}"
        self._clear_special_events(full)
        for win in windows:
            self._write_special_event(full, win)
        logging.info("Wrote %d windows to %s", len(windows), niagara_path)

    def _clear_special_events(self, path: str) -> None:
        endpoint = f"{self.base}/{path}/specialEvents"
        try:
            r = self.session.delete(endpoint, timeout=HTTP_TIMEOUT_WRITE)
            if r.status_code not in (200, 204, 404):
                logging.warning("Unexpected status clearing %s: %s",
                                path, r.status_code)
        except requests.RequestException as exc:
            logging.error("Failed clearing %s: %s", path, exc)
            raise

    def _write_special_event(self, path: str, win: OccupancyWindow) -> None:
        payload = {
            "type": "baja:BooleanSchedule$SpecialEvent",
            "start": win.start.isoformat(),
            "end":   win.end.isoformat(),
            "value": {"value": OCCUPIED_VALUE},        # True = Occupied
            "priority": BACNET_SCHEDULE_PRIORITY,
        }
        endpoint = f"{self.base}/{path}/specialEvents"
        r = self.session.post(endpoint, json=payload, timeout=HTTP_TIMEOUT_WRITE)
        if r.status_code not in (200, 201):
            raise RuntimeError(
                f"Write failed {path}: {r.status_code} {r.text[:200]}")

    def write_heartbeat(self) -> None:
        """Stamp _LastSync so monitoring can alert if the cron stops running."""
        if not self.heartbeat_path:
            return
        stamp = datetime.now(self.tz).isoformat()
        endpoint = f"{self.base}/{self.heartbeat_path}/out"
        try:
            self.session.post(endpoint, json={"value": stamp},
                              timeout=HTTP_TIMEOUT_HEALTH)
        except requests.RequestException as exc:
            logging.warning("Could not write heartbeat: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Main sync routine
# ─────────────────────────────────────────────────────────────────────────────

def run_sync(cfg: dict, dry_run: bool = False) -> int:
    """
    One full sync pass. Returns a process exit code (0=ok).

    When dry_run is True, it fetches from 25Live and builds the schedule, logs
    what *would* be written, and returns 0 without contacting Niagara for any
    writes. This is the single source of truth for both modes — no duplicated
    fetch/build logic elsewhere.
    """
    tz = ZoneInfo(cfg["timezone"])

    space_map = load_space_map(cfg["space_map_file"], cfg)
    if not space_map:
        logging.error("Space map is empty — nothing to sync. Aborting.")
        return 2

    cn = CollegeNetClient(cfg["collegenet"], tz)
    builder = ScheduleBuilder(cfg["collegenet"]["merge_gap_minutes"])

    try:
        events = cn.fetch_events(space_map)
    except requests.RequestException as exc:
        logging.error("Failed to fetch from 25Live: %s", exc)
        return 4

    schedule = builder.build(events, space_map)

    if dry_run:
        logging.info("DRY RUN — schedules that WOULD be written:")
        for path, windows in sorted(schedule.items()):
            logging.info("  %s:", path)
            for w in windows:
                logging.info("      %s", w)
        return 0

    n4 = NiagaraClient(cfg["niagara"], tz)
    if not n4.health_check():
        logging.error("Niagara station unreachable — aborting this run.")
        return 3

    failures = 0
    for niagara_path, windows in schedule.items():
        try:
            n4.write_schedule(niagara_path, windows)
        except Exception as exc:
            logging.error("Error writing %s: %s", niagara_path, exc)
            failures += 1

    # Also clear schedules that had events before but have none now
    # (mapped spaces with zero events this week get an explicit empty write)
    for space_id, sc in space_map.items():
        if sc.niagara_path not in schedule:
            try:
                n4.write_schedule(sc.niagara_path, [])
            except Exception as exc:
                logging.error("Error clearing %s: %s", sc.niagara_path, exc)
                failures += 1

    n4.write_heartbeat()

    if failures:
        logging.error("Sync finished with %d write failures", failures)
        return 5

    logging.info("Sync completed successfully — %d schedules written",
                 len(schedule))
    return 0


def setup_logging(log_file: str) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    except OSError:
        # If the log dir isn't writable, fall back to stdout only
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="25Live -> Niagara N4 daily schedule sync (single run).")
    parser.add_argument("--space-map", help="Override path to space_mapping.yaml")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and build schedules but do not write to Niagara")
    args = parser.parse_args()

    if args.space_map:
        CONFIG["space_map_file"] = args.space_map
    if CONFIG["log_file"] is None:
        CONFIG["log_file"] = default_log_file()

    setup_logging(CONFIG["log_file"])
    load_credentials(CONFIG)   # after logging is up, so warnings are captured

    logging.info("=== 25Live -> Niagara sync starting (lookahead %d days%s) ===",
                 CONFIG["collegenet"]["lookahead_days"],
                 ", DRY RUN" if args.dry_run else "")

    code = run_sync(CONFIG, dry_run=args.dry_run)
    logging.info("=== Sync exited with code %d ===", code)
    return code


if __name__ == "__main__":
    sys.exit(main())
