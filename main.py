#!/usr/bin/env python3
# 25Live -> Niagara Schedule Sync
# Copyright (C) 2026 Ryan Bibby and contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details <https://www.gnu.org/licenses/>.
"""
25Live -> Niagara Schedule Sync
===============================
Pulls room bookings from CollegeNET 25Live (Series25 WebServices, XML) and writes
them into a Tridium Niagara station as BooleanSchedule SpecialEvents, so HVAC/
lighting pre-conditions for booked rooms and stands down when they're empty.

Runs ONCE per invocation — schedule it nightly (e.g. 2 AM) via Windows Task
Scheduler or cron. See README.md for deployment details.

On each run it:
  1. Loads settings from config.yaml and the room map from space_mapping.yaml
  2. Pulls the next N days of confirmed events from 25Live
  3. Applies per-space pre-conditioning / post-buffer offsets
  4. Merges overlapping/adjacent events into clean occupancy windows
  5. Rolls room events up into building-level schedules
  6. Writes them to Niagara as BooleanSchedule SpecialEvents
  7. Writes a heartbeat timestamp to Niagara for monitoring
  8. Exits 0 on success, non-zero on failure (so monitoring can alert)

Quick start:
    pip install -r requirements.txt
    cp config.example.yaml config.yaml         # then edit it for your site
    export BAS_25LIVE_PASSWORD=...             # (set ... on Windows)
    export BAS_NIAGARA_PASSWORD=...
    python main.py --dry-run                    # fetch + build, no writes

Institution-specific settings live in config.yaml (gitignored). The room/building
map lives in space_mapping.yaml (edit by hand or via editor.py).
"""

import os
import sys
import copy
import smtplib
import logging
import argparse
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from collections import defaultdict
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Optional
from urllib.parse import quote

import requests
import yaml
from dateutil import parser as dateparser
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


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

# Safety cap on how many pages we'll pull per batch, in case an API that ignores
# paging would otherwise loop forever. 1000 pages * PAGE_SIZE = 100k events.
MAX_PAGES = 1000

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
# CONFIGURATION
#
# Institution-specific settings live in config.yaml (copy config.example.yaml).
# load_config() merges that file over these built-in DEFAULTS at startup, so the
# code stays generic and each site only edits YAML. config.yaml is gitignored.
#
# Secrets are NOT stored in either file — set them as environment variables:
#     BAS_25LIVE_PASSWORD   -> collegenet.password
#     BAS_NIAGARA_PASSWORD  -> niagara.password
# ─────────────────────────────────────────────────────────────────────────────

# For CollegeNET-hosted 25Live, the WebServices base URL is built from the
# instance name. Self-hosted sites set collegenet.base_url directly instead.
COLLEGENET_URL_TEMPLATE = "https://webservices.collegenet.com/r25ws/wrd/{instance}/run"

DEFAULTS = {
    # ── CollegeNET 25Live Series25 WebServices (XML API) ──
    "collegenet": {
        "instance": "",                       # your 25Live instance name
        "base_url": "",                       # blank -> built from `instance`
        "username": "",                       # a LOCAL 25Live account (not SSO)
        "password": PLACEHOLDER_PASSWORD,     # set env var BAS_25LIVE_PASSWORD
        "lookahead_days": 7,
        "include_states": [2],                # 2=confirmed (add 4 for tentative)
        "default_pre_condition_minutes": 30,
        "default_post_buffer_minutes": 15,
        "merge_gap_minutes": 5,               # default gap for collapsing a
                                              #   space's windows; rooms/buildings
                                              #   may override per entry. Building
                                              #   roll-ups use this default.
    },

    # ── Niagara station (REST/HTTP API) ──
    "niagara": {
        "host": "localhost",                  # station host (localhost if this
                                              #   runs on the station server)
        "port": 443,                          # confirm — often 443 or 8443
        "https": True,
        "username": "",
        "password": PLACEHOLDER_PASSWORD,     # set env var BAS_NIAGARA_PASSWORD
        "verify_tls": False,                  # or a CA-bundle path in production
        "schedule_base_path": "slot:/Schedules",
        "heartbeat_path": "slot:/Schedules/_LastSync",
    },

    "timezone": "America/New_York",           # your campus timezone (IANA name)

    # ── Resilience: retry transient 25Live/Niagara errors (timeouts, 5xx) ──
    # Applied to reads and the idempotent clear; writes (POST) are not auto-
    # retried, to avoid duplicate special events.
    "retry": {
        "attempts": 3,                        # retries per request
        "backoff_seconds": 2.0,               # exponential backoff base
    },

    # ── Alerting: notify on a failed (or optionally successful) sync run ──
    # Off by default. Email SMTP password comes from env var BAS_SMTP_PASSWORD.
    "alerts": {
        "enabled": False,
        "notify_on_success": False,
        "webhook_url": "",                    # Slack/Teams/generic incoming webhook
        "email": {
            "enabled": False,
            "smtp_host": "",
            "smtp_port": 587,
            "use_tls": True,
            "username": "",                   # SMTP user (password: BAS_SMTP_PASSWORD)
            "from_addr": "",
            "to_addrs": [],
        },
    },

    # Room map YAML (same folder as this script by default).
    "space_map_file": str(Path(__file__).parent / "space_mapping.yaml"),

    # Log file. None -> default_log_file(); override in config.yaml if desired.
    "log_file": None,
}

# Working config: DEFAULTS until main() merges config.yaml over a copy of it.
# Importers and tests can use this directly (it holds the defaults).
CONFIG = copy.deepcopy(DEFAULTS)


def default_log_file() -> str:
    """
    Default log path: a logs/ folder next to this script (portable across OSes).
    Override with `log_file` in config.yaml. setup_logging() falls back to
    stdout-only if the directory isn't writable, so this never blocks a run.
    """
    return str(Path(__file__).parent / "logs" / "25live_sync.log")


def _deep_merge(base: dict, override: dict) -> None:
    """Recursively merge `override` into `base` in place (nested dicts merged,
    scalars/lists replaced)."""
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val


class ConfigError(Exception):
    """A YAML file (config / defaults / room map) is unreadable, malformed, or
    not a mapping. Carries a human-readable, file-named message so callers can
    report one clean line instead of a raw traceback."""


def _read_yaml(path) -> dict:
    """
    Load a YAML file into a dict. A missing or empty file yields {}. A parse
    error, an unreadable file, or a top level that isn't a mapping raises
    ConfigError naming the file — so a stray tab in config.yaml fails with one
    clear line instead of a stack trace.
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (yaml.YAMLError, OSError) as exc:
        raise ConfigError(f"{p}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"{p}: expected a YAML mapping at the top level, got "
            f"{type(data).__name__}.")
    return data


# Global scheduling defaults live in defaults.yaml — a small, GUI-editable file
# (run-up / run-down / merge-gap / lookahead). These flat keys map onto the
# internal config so the rest of the code is unchanged.
DEFAULTS_FILE_MAP = {
    "pre_condition_minutes": "default_pre_condition_minutes",
    "post_buffer_minutes":   "default_post_buffer_minutes",
    "merge_gap_minutes":     "merge_gap_minutes",
    "lookahead_days":        "lookahead_days",
}


def load_config(path: str, defaults_path: Optional[str] = None) -> dict:
    """
    Build the runtime config: a deep copy of DEFAULTS, with config.yaml merged
    over it, then the global scheduling defaults from defaults.yaml applied on
    top. defaults.yaml is the single, GUI-editable home for the run-up/run-down/
    merge-gap/lookahead defaults; connection/auth settings stay in config.yaml.
    A missing file just leaves the built-ins in place. If collegenet.base_url is
    blank, it's derived from collegenet.instance. Secrets are applied separately
    by load_credentials().
    """
    cfg = copy.deepcopy(DEFAULTS)
    user = _read_yaml(path)
    if user:
        _deep_merge(cfg, user)

    if defaults_path:
        gd = _read_yaml(defaults_path)
        for file_key, cfg_key in DEFAULTS_FILE_MAP.items():
            if gd.get(file_key) is not None:
                cfg["collegenet"][cfg_key] = gd[file_key]

    cn = cfg["collegenet"]
    if not cn.get("base_url"):
        instance = (cn.get("instance") or "").strip()
        if instance:
            cn["base_url"] = COLLEGENET_URL_TEMPLATE.format(instance=instance)
    return cfg


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
# HTTP retry + alerting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mount_retries(session: requests.Session, retry: Optional[dict],
                   allowed_methods) -> None:
    """
    Mount a urllib3 Retry adapter so transient errors (connection failures,
    timeouts, 429/5xx) are retried with exponential backoff. `allowed_methods`
    limits which HTTP verbs auto-retry — we pass only safe/idempotent ones
    (GET, DELETE) so writes (POST) never replay and create duplicate events.
    """
    if not retry:
        return
    policy = Retry(
        total=retry.get("attempts", 3),
        connect=retry.get("attempts", 3),
        read=retry.get("attempts", 3),
        status=retry.get("attempts", 3),
        backoff_factor=retry.get("backoff_seconds", 2.0),
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(allowed_methods),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=policy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)


def send_alert(alerts_cfg: dict, subject: str, body: str) -> None:
    """
    Best-effort notification on a failed (or optionally successful) run. Sends
    to a webhook (Slack/Teams/generic) and/or email if configured. Never raises
    — an alerting problem must not change the run's outcome.
    """
    if not alerts_cfg or not alerts_cfg.get("enabled"):
        return

    url = alerts_cfg.get("webhook_url")
    if url:
        try:
            requests.post(url, json={"text": f"{subject}\n\n{body}"}, timeout=15)
        except requests.RequestException as exc:
            logging.warning("Alert webhook failed: %s", exc)

    email = alerts_cfg.get("email") or {}
    if email.get("enabled"):
        try:
            _send_email(email, subject, body)
        except Exception as exc:
            logging.warning("Alert email failed: %s", exc)


def _send_email(email_cfg: dict, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_cfg.get("from_addr", "")
    msg["To"] = ", ".join(email_cfg.get("to_addrs", []))
    msg.set_content(body)
    with smtplib.SMTP(email_cfg.get("smtp_host"),
                      email_cfg.get("smtp_port", 587), timeout=20) as smtp:
        if email_cfg.get("use_tls", True):
            smtp.starttls()
        user = email_cfg.get("username")
        password = os.environ.get("BAS_SMTP_PASSWORD")
        if user and password:
            smtp.login(user, password)
        smtp.send_message(msg)


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
    floor: Optional[int] = None           # which floor the room is on (if any)
    floor_schedule_path: Optional[str] = None  # the floor's hallway schedule the
                                          # room also feeds (floor -> building)


# ─────────────────────────────────────────────────────────────────────────────
# Space map loader
# ─────────────────────────────────────────────────────────────────────────────

def _int_or_default(value, default: int) -> int:
    """
    Return the configured value, falling back to `default` only when it is truly
    absent (None). A plain `value or default` would wrongly replace an explicit
    0 — e.g. a room that intentionally sets pre_condition_minutes: 0 to disable
    pre-conditioning would silently get the 30-minute default instead.
    """
    return default if value is None else int(value)


def _resolve_minutes(room_value, building_value, default: int) -> int:
    """
    Resolve a per-room minutes setting (run-up / run-down) with precedence:
        room override > building override > global default.
    A value counts as "set" only when not None, so an explicit 0 is honored at
    any level (same rationale as _int_or_default).
    """
    if room_value is not None:
        return int(room_value)
    if building_value is not None:
        return int(building_value)
    return default


def load_space_map(path: str, cfg: dict) -> dict[str, SpaceConfig]:
    """
    Parse space_mapping.yaml into { space_id: SpaceConfig }.

    The YAML has up to three sections:
        buildings:  each building's roll-up schedule, defined ONCE.
        floors:     (optional) per-floor hallway schedules — each names its
                    building, a numeric level, and a niagara_path.
        spaces:     the rooms; each names its building and (optionally) floor.

    Every room that names a building is automatically unioned into that
    building's occupancy schedule by ScheduleBuilder — so if ANY room in the
    building is occupied, the building schedule (lobbies, common AHUs) is
    occupied too. If the room also names a floor (and that floor is defined for
    its building), the room additionally feeds that floor's hallway schedule —
    so floors roll up into the building (room -> floor -> building).

    Run-up (pre_condition_minutes) and run-down (post_buffer_minutes) resolve
    with precedence: room override > building override > global default.
    """
    default_pre = cfg["collegenet"]["default_pre_condition_minutes"]
    default_post = cfg["collegenet"]["default_post_buffer_minutes"]
    default_gap = cfg["collegenet"]["merge_gap_minutes"]

    if not Path(path).exists():
        logging.error(
            "Room map not found: %s — copy space_mapping.example.yaml to "
            "space_mapping.yaml (or run editor.py) and add your rooms.", path)
        return {}

    try:
        data = _read_yaml(path)
    except ConfigError as exc:
        logging.error("Could not load room map — %s", exc)
        return {}

    # 1) Index the building definitions by their id. A building needs both an id
    #    and a niagara_path; skip (with a warning) any entry missing one rather
    #    than letting one bad row abort the whole load.
    buildings: dict[str, dict] = {}
    for b in data.get("buildings", []):
        try:
            bid = str(b["id"])
            if not b.get("niagara_path"):
                raise KeyError("niagara_path")
        except (KeyError, TypeError) as exc:
            logging.warning("Skipping building entry missing %s: %r", exc, b)
            continue
        buildings[bid] = b

    # 1b) Index floor hallway schedules by (building_id, level).
    floors_by_key: dict[tuple, str] = {}
    for f in data.get("floors", []):
        try:
            floor_key = (str(f["building"]), int(f["level"]))
            floor_path = f["niagara_path"]
        except (KeyError, TypeError, ValueError) as exc:
            logging.warning("Skipping malformed floors entry (%s): %r", exc, f)
            continue
        floors_by_key[floor_key] = floor_path

    space_map: dict[str, SpaceConfig] = {}

    # 2) Rooms — resolve each room's building id to that building's Niagara path
    #    so the existing roll-up logic unions every room in the building.
    for row in data.get("spaces", []):
        try:
            space_id = str(row["space_id"])
            room_path = row["niagara_path"]
        except (KeyError, TypeError) as exc:
            logging.warning("Skipping room entry missing %s: %r", exc, row)
            continue
        building_path: Optional[str] = None
        building: Optional[dict] = None
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

        # Optional per-floor hallway schedule (room -> floor -> building).
        floor: Optional[int] = None
        floor_path: Optional[str] = None
        if row.get("floor") is not None and building_id is not None:
            try:
                floor = int(row["floor"])
            except (TypeError, ValueError):
                logging.warning("Room %s has a non-numeric floor %r — ignoring it.",
                                space_id, row["floor"])
            if floor is not None:
                floor_path = floors_by_key.get((building_id, floor))
                if floor_path is None:
                    logging.warning(
                        "Room %s references floor %s of building '%s' with no "
                        "matching floors: entry — it will NOT drive a floor "
                        "schedule.", space_id, floor, building_id)

        bld = building or {}
        space_map[space_id] = SpaceConfig(
            space_id=space_id,
            space_name=row.get("space_name", space_id),
            space_type="room",
            niagara_path=room_path,
            building_schedule_path=building_path,
            floor=floor,
            floor_schedule_path=floor_path,
            # Run-up / run-down: room override > building override > global default.
            pre_condition_minutes=_resolve_minutes(
                row.get("pre_condition_minutes"),
                bld.get("pre_condition_minutes"), default_pre),
            post_buffer_minutes=_resolve_minutes(
                row.get("post_buffer_minutes"),
                bld.get("post_buffer_minutes"), default_post),
            merge_gap_minutes=_int_or_default(row.get("merge_gap_minutes"), default_gap),
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
            pre_condition_minutes=_int_or_default(b.get("pre_condition_minutes"), default_pre),
            post_buffer_minutes=_int_or_default(b.get("post_buffer_minutes"), default_post),
            merge_gap_minutes=_int_or_default(b.get("merge_gap_minutes"), default_gap),
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

    def __init__(self, cfg: dict, tz: ZoneInfo, retry: Optional[dict] = None):
        self.base_url = cfg["base_url"].rstrip("/")
        self.lookahead_days = cfg["lookahead_days"]
        self.include_states = set(cfg["include_states"])
        self.merge_gap = cfg["merge_gap_minutes"]
        self.tz = tz
        self.session = requests.Session()
        self.session.auth = (cfg["username"], cfg["password"])
        self.session.headers.update({"Accept": "application/xml"})
        _mount_retries(self.session, retry, allowed_methods=["GET"])

    def _state_param(self) -> str:
        return "+".join(str(s) for s in sorted(self.include_states))

    @staticmethod
    def _local(tag: str) -> str:
        """Strip the namespace prefix from an XML tag."""
        return tag.rsplit("}", 1)[-1]

    def _child_text(self, elem: ET.Element, local_name: str) -> Optional[str]:
        """Text of the first direct child whose local tag name matches."""
        for child in elem:
            if self._local(child.tag) == local_name and child.text:
                return child.text.strip()
        return None

    def check_connection(self) -> tuple[bool, str]:
        """
        Lightweight authenticated request to confirm reachability + credentials,
        used by --validate. Returns (ok, detail). A 401/403 means the account or
        password is wrong; anything else that responds means we reached and
        authenticated against the service.
        """
        now = datetime.now(self.tz)
        params = {
            "start_dt": now.strftime("%Y-%m-%dT00:00:00"),
            "end_dt":   now.strftime("%Y-%m-%dT23:59:59"),
            "state":    self._state_param(),
            "page_size": 1,
        }
        try:
            r = self.session.get(f"{self.base_url}/events.xml", params=params,
                                 timeout=HTTP_TIMEOUT_HEALTH)
        except requests.RequestException as exc:
            return False, f"connection error: {exc}"
        if r.status_code in (401, 403, 407):
            return False, (f"auth failed (HTTP {r.status_code}) — check username "
                           "and BAS_25LIVE_PASSWORD")
        return True, f"HTTP {r.status_code}"

    def discover_spaces(self, days: int) -> list[dict]:
        """
        Return distinct {space_id, space_name} seen in events over the next
        `days` days. Uses the SAME events endpoint as the sync (no extra API
        surface to validate), so it finds spaces that have bookings in the
        window — handy for first-time mapping. Spaces with no upcoming events
        won't appear; widen `days` to surface more.
        """
        now = datetime.now(self.tz)
        end = now + timedelta(days=days)
        seen: dict[str, str] = {}
        offset = 0
        for _page in range(MAX_PAGES):
            params = {
                "start_dt": now.strftime("%Y-%m-%dT00:00:00"),
                "end_dt":   end.strftime("%Y-%m-%dT23:59:59"),
                "scope":    "extended",
                "state":    self._state_param(),
                "page_size": PAGE_SIZE,
                "page_offset": offset,
            }
            r = self.session.get(f"{self.base_url}/events.xml", params=params,
                                 timeout=HTTP_TIMEOUT_FETCH)
            r.raise_for_status()
            root = ET.fromstring(r.text)
            events = root.findall("r25:event", R25_NS)
            self._collect_spaces_from(root, seen)
            if len(events) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return [{"space_id": sid, "space_name": name}
                for sid, name in sorted(seen.items(), key=lambda kv: kv[1].lower())]

    def _collect_spaces_from(self, root: ET.Element, into: dict) -> None:
        """Collect {space_id: space_name} from any element that has a space_id
        child (the space_reservation), preferring space_name then formal_name."""
        for elem in root.iter():
            sid = self._child_text(elem, "space_id")
            if sid:
                name = (self._child_text(elem, "space_name")
                        or self._child_text(elem, "formal_name") or sid)
                into.setdefault(sid, name)

    @staticmethod
    def _chunk(items: list[str], size: int) -> list[list[str]]:
        """Split a list into chunks of at most `size`."""
        return [items[i:i + size] for i in range(0, len(items), size)]

    def _to_tz(self, dt_text: str) -> datetime:
        """
        Parse a 25Live datetime string into the configured timezone.

        25Live returns instance-local timestamps. If the string is naive (no
        offset), attach the configured tz — do NOT use .astimezone(), which
        would assume the *server's* local tz and shift the time if the server
        isn't in America/New_York. If it's already tz-aware, convert it.
        """
        dt = dateparser.parse(dt_text)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=self.tz)
        return dt.astimezone(self.tz)

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

        for _page in range(MAX_PAGES):
            params = {
                "space_id": " ".join(space_ids),     # space-separated IDs
                "start_dt": start.strftime("%Y-%m-%dT00:00:00"),
                "end_dt":   end.strftime("%Y-%m-%dT23:59:59"),
                "scope":    "extended",               # include setup/pre-event times
                # NOTE: Series25 filters confirmed events by the numeric `state`
                # query param, not include=confirmed. We derive it from
                # include_states so CONFIG actually drives the request. If your
                # 25Live instance expects a different format (e.g. comma- vs
                # plus-separated), change _state_param().
                "state":    self._state_param(),
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

            # Stop on the final (short) page. We deliberately do NOT rely on a
            # total-count element being present: if the response omits it (or
            # names it differently across API versions), stopping early would
            # silently drop every event past the first page.
            if len(events) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        else:
            logging.warning(
                "Reached MAX_PAGES (%d) while paging 25Live events for a batch; "
                "results may be truncated.", MAX_PAGES)

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

            event_start = self._to_tz(start_txt)
            event_end   = self._to_tz(end_txt)

            # 25Live's own setup/teardown times (present with scope=extended)
            native_pre_txt  = (res.findtext("r25:setup_dt", namespaces=R25_NS)
                               or res.findtext("r25:pre_event_dt", namespaces=R25_NS))
            native_post_txt = (res.findtext("r25:takedown_dt", namespaces=R25_NS)
                               or res.findtext("r25:post_event_dt", namespaces=R25_NS))
            native_pre  = self._to_tz(native_pre_txt) if native_pre_txt else event_start
            native_post = self._to_tz(native_post_txt) if native_post_txt else event_end

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
        # Accumulated roll-up windows keyed by target schedule path. A room feeds
        # its floor schedule (if any) AND its building schedule, so floors roll
        # up into the building (room -> floor -> building).
        rollup_windows: dict[str, list[OccupancyWindow]] = defaultdict(list)

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
                if sc.floor_schedule_path:
                    rollup_windows[sc.floor_schedule_path].extend(windows)
                if sc.building_schedule_path:
                    rollup_windows[sc.building_schedule_path].extend(windows)
            elif sc.space_type == "building":
                # Direct building booking (e.g. a lobby event)
                rollup_windows[sc.niagara_path].extend(windows)

        # Merge the roll-ups (floor + building, plus direct building bookings).
        # The inputs are each space's already gap-merged windows; here we merge
        # ACROSS spaces using the global default gap. A room's own gap override
        # governs only its own windows — but those windows still carry into the
        # roll-up, so a room kept "occupied" across a gap keeps its floor and
        # building occupied too.
        for bpath, windows in rollup_windows.items():
            merged = self._merge(sorted(windows, key=lambda w: w.start),
                                 self.default_merge_gap)
            # If the roll-up path was also a room result, union them
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

    def __init__(self, cfg: dict, tz: ZoneInfo, retry: Optional[dict] = None):
        proto = "https" if cfg["https"] else "http"
        self.base = f"{proto}://{cfg['host']}:{cfg['port']}{NIAGARA_REST_BASE}"
        self.schedule_base = cfg["schedule_base_path"]
        self.heartbeat_path = cfg.get("heartbeat_path")
        self.tz = tz
        self.session = requests.Session()
        self.session.auth = (cfg["username"], cfg["password"])
        # Retry only safe/idempotent verbs — GET (reads) and DELETE (the clear
        # step). POST writes are intentionally excluded so a retry can't create
        # duplicate special events.
        _mount_retries(self.session, retry, allowed_methods=["GET", "DELETE"])
        self.session.verify = cfg.get("verify_tls", False)
        if not self.session.verify:
            logging.warning(
                "Niagara TLS verification is DISABLED (verify_tls=False). "
                "Acceptable for a self-signed station cert on a trusted network; "
                "for production, set verify_tls to a CA-bundle path.")
            # Suppress the per-request InsecureRequestWarning so a nightly run
            # with many writes doesn't flood the log. The warning above is the
            # single, intentional notice.
            from urllib3.exceptions import InsecureRequestWarning
            requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    @staticmethod
    def _encode_ord(path: str) -> str:
        """
        Percent-encode a Niagara ORD for use in a REST URL path, preserving the
        ORD structure characters ('/', ':', '$') while encoding spaces and other
        unsafe characters (schedule/component names often contain spaces). Part
        of the N4.15 REST surface to confirm against your station — see the
        class docstring.
        """
        return quote(path, safe="/:$")

    def health_check(self) -> bool:
        try:
            r = self.session.get(f"{self.base}/about", timeout=HTTP_TIMEOUT_HEALTH)
            return r.status_code == 200
        except requests.RequestException as exc:
            logging.error("Niagara health check failed: %s", exc)
            return False

    def schedule_exists(self, niagara_path: str) -> bool:
        """
        True if the schedule component at this path resolves (HTTP 200), used by
        --validate to catch typos before a live run. Subject to the same REST
        contract caveats as the writes (see class docstring).
        """
        full = f"{self.schedule_base}/{niagara_path}"
        endpoint = f"{self.base}/{self._encode_ord(full)}"
        try:
            r = self.session.get(endpoint, timeout=HTTP_TIMEOUT_HEALTH)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def write_schedule(self, niagara_path: str, windows: list[OccupancyWindow]) -> None:
        full = f"{self.schedule_base}/{niagara_path}"
        self._clear_special_events(full)
        for win in windows:
            self._write_special_event(full, win)
        logging.info("Wrote %d windows to %s", len(windows), niagara_path)

    def _clear_special_events(self, path: str) -> None:
        endpoint = f"{self.base}/{self._encode_ord(path)}/specialEvents"
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
        endpoint = f"{self.base}/{self._encode_ord(path)}/specialEvents"
        r = self.session.post(endpoint, json=payload, timeout=HTTP_TIMEOUT_WRITE)
        if r.status_code not in (200, 201):
            raise RuntimeError(
                f"Write failed {path}: {r.status_code} {r.text[:200]}")

    def write_heartbeat(self) -> None:
        """Stamp _LastSync so monitoring can alert if the cron stops running."""
        if not self.heartbeat_path:
            return
        stamp = datetime.now(self.tz).isoformat()
        endpoint = f"{self.base}/{self._encode_ord(self.heartbeat_path)}/out"
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

    if not cfg["collegenet"].get("base_url"):
        logging.error(
            "25Live base_url is not set — set collegenet.instance (for "
            "CollegeNET-hosted sites) or collegenet.base_url in config.yaml. "
            "Did you copy config.example.yaml to config.yaml?")
        return 4

    cn = CollegeNetClient(cfg["collegenet"], tz, cfg.get("retry"))
    builder = ScheduleBuilder(cfg["collegenet"]["merge_gap_minutes"])

    try:
        events = cn.fetch_events(space_map)
    except requests.RequestException as exc:
        logging.error("Failed to fetch from 25Live: %s", exc)
        return 4
    except ET.ParseError as exc:
        logging.error("25Live returned a response that isn't valid XML "
                      "(check the instance/base_url and account): %s", exc)
        return 4

    schedule = builder.build(events, space_map)

    if dry_run:
        logging.info("DRY RUN — schedules that WOULD be written:")
        for path, windows in sorted(schedule.items()):
            logging.info("  %s:", path)
            for w in windows:
                logging.info("      %s", w)
        return 0

    n4 = NiagaraClient(cfg["niagara"], tz, cfg.get("retry"))
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

    # Also clear schedules that had events before but have none now: every
    # managed path (room, floor, building) with zero events this week gets an
    # explicit empty write so it goes Unoccupied.
    for path in _schedule_paths(space_map):
        if path not in schedule:
            try:
                n4.write_schedule(path, [])
            except Exception as exc:
                logging.error("Error clearing %s: %s", path, exc)
                failures += 1

    n4.write_heartbeat()

    if failures:
        logging.error("Sync finished with %d write failures", failures)
        return 5

    logging.info("Sync completed successfully — %d schedules written",
                 len(schedule))
    return 0


def _schedule_paths(space_map: dict[str, SpaceConfig]) -> set:
    """All distinct Niagara schedule paths the sync manages (rooms + floor +
    building roll-ups)."""
    paths = set()
    for sc in space_map.values():
        paths.add(sc.niagara_path)
        if sc.building_schedule_path:
            paths.add(sc.building_schedule_path)
        if sc.floor_schedule_path:
            paths.add(sc.floor_schedule_path)
    return paths


def run_validate(cfg: dict) -> int:
    """
    Pre-flight check (no writes): config, 25Live auth, Niagara reachability, and
    that every schedule ORD exists. Logs a PASS/FAIL summary. Returns 0 if all
    checks pass, else 6.
    """
    tz = ZoneInfo(cfg["timezone"])
    checks: list[tuple[str, bool, str]] = []

    space_map = load_space_map(cfg["space_map_file"], cfg)
    checks.append(("Room map loads",
                   bool(space_map),
                   f"{len(space_map)} spaces" if space_map else "empty or missing"))

    base_url = cfg["collegenet"].get("base_url")
    checks.append(("25Live base_url configured",
                   bool(base_url),
                   base_url or "set collegenet.instance or base_url"))

    if base_url:
        cn = CollegeNetClient(cfg["collegenet"], tz, cfg.get("retry"))
        ok, detail = cn.check_connection()
        checks.append(("25Live reachable + authenticated", ok, detail))

    n4 = NiagaraClient(cfg["niagara"], tz, cfg.get("retry"))
    reachable = n4.health_check()
    checks.append(("Niagara reachable",
                   reachable,
                   f"{n4.base}/about" if reachable else "unreachable (check host/port/TLS)"))

    if reachable and space_map:
        missing = sorted(p for p in _schedule_paths(space_map)
                         if not n4.schedule_exists(p))
        checks.append(("Niagara schedules exist",
                       not missing,
                       "all present" if not missing
                       else f"{len(missing)} missing: {', '.join(missing)}"))

    logging.info("=== Validation results ===")
    for name, ok, detail in checks:
        logging.info("  [%-4s] %s — %s", "PASS" if ok else "FAIL", name, detail)

    all_ok = all(ok for _, ok, _ in checks)
    logging.info("=== Validation %s ===", "PASSED" if all_ok else "FAILED")
    return 0 if all_ok else 6


def run_discover(cfg: dict, days: int) -> int:
    """
    List 25Live spaces that have events in the next `days` days, as a starter
    for space_mapping.yaml. Read-only; never writes anything.
    """
    tz = ZoneInfo(cfg["timezone"])
    if not cfg["collegenet"].get("base_url"):
        logging.error("25Live base_url is not set — configure collegenet.instance "
                      "or base_url in config.yaml.")
        return 4

    cn = CollegeNetClient(cfg["collegenet"], tz, cfg.get("retry"))
    try:
        spaces = cn.discover_spaces(days)
    except (requests.RequestException, ET.ParseError) as exc:
        logging.error("Discovery failed: %s", exc)
        return 4

    logging.info("Discovered %d space(s) with events in the next %d days:",
                 len(spaces), days)
    # Print a YAML-ish starter block operators can paste into space_mapping.yaml.
    lines = ["", "# --- discovered spaces (add building + niagara_path) ---", "spaces:"]
    for s in spaces:
        lines.append(f"  - space_id: {s['space_id']}")
        lines.append(f"    space_name: \"{s['space_name']}\"")
        lines.append(f"    niagara_path: \"\"   # TODO: set the Niagara schedule slot")
    print("\n".join(lines))
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


# Human-readable hints for the sync exit codes (used in failure alerts).
EXIT_CODE_HELP = {
    1: "Unhandled error (see the log).",
    2: "Room map empty or missing.",
    3: "Niagara station unreachable.",
    4: "25Live fetch failed (auth, network, or config).",
    5: "One or more Niagara writes failed.",
    6: "Validation failed.",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="25Live -> Niagara schedule sync (single run).")
    parser.add_argument("--config",
                        help="Path to config.yaml (default: ./config.yaml, or $BAS_CONFIG)")
    parser.add_argument("--defaults",
                        help="Path to defaults.yaml (default: ./defaults.yaml, or $BAS_DEFAULTS)")
    parser.add_argument("--space-map",
                        help="Path to space_mapping.yaml (default: ./space_mapping.yaml, "
                             "or $BAS_SPACE_MAP)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and build schedules but do not write to Niagara")
    parser.add_argument("--validate", action="store_true",
                        help="Pre-flight checks only (config, auth, reachability, "
                             "schedule ORDs); no writes")
    parser.add_argument("--discover", action="store_true",
                        help="List 25Live spaces with upcoming events (read-only)")
    parser.add_argument("--discover-days", type=int, default=30,
                        help="Window for --discover, in days (default 30)")
    args = parser.parse_args()

    cfg_path = (args.config or os.environ.get("BAS_CONFIG")
                or str(Path(__file__).parent / "config.yaml"))
    defaults_path = (args.defaults or os.environ.get("BAS_DEFAULTS")
                     or str(Path(__file__).parent / "defaults.yaml"))
    try:
        cfg = load_config(cfg_path, defaults_path)
    except ConfigError as exc:
        # Logging isn't configured yet; bring it up on the default path so this
        # fatal startup error is recorded, then exit cleanly (no traceback).
        setup_logging(default_log_file())
        logging.error("Configuration error — %s", exc)
        return 1

    space_map = args.space_map or os.environ.get("BAS_SPACE_MAP")
    if space_map:
        cfg["space_map_file"] = space_map
    if cfg["log_file"] is None:
        cfg["log_file"] = default_log_file()

    setup_logging(cfg["log_file"])
    if not Path(cfg_path).exists():
        logging.warning(
            "No config file at %s — using built-in defaults. Copy "
            "config.example.yaml to config.yaml and edit it for your site.",
            cfg_path)
    load_credentials(cfg)   # after logging is up, so warnings are captured

    # Validate the timezone once, up front, so every mode gets the same clear
    # message instead of a ZoneInfo traceback deep in a run.
    try:
        ZoneInfo(cfg["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        logging.error(
            "Invalid timezone %r — use an IANA name like 'America/New_York'. "
            "On Windows, make sure the 'tzdata' package is installed. (%s)",
            cfg["timezone"], exc)
        return 1

    # A "live sync" is the only mode that alerts; --validate/--discover/--dry-run
    # are interactive and just return a code.
    is_live_sync = not (args.validate or args.discover or args.dry_run)

    mode = ("VALIDATE" if args.validate else "DISCOVER" if args.discover
            else "DRY RUN" if args.dry_run else "SYNC")
    logging.info("=== 25Live -> Niagara starting (%s, lookahead %d days) ===",
                 mode, cfg["collegenet"]["lookahead_days"])

    try:
        if args.validate:
            code = run_validate(cfg)
        elif args.discover:
            code = run_discover(cfg, args.discover_days)
        else:
            code = run_sync(cfg, dry_run=args.dry_run)
    except Exception as exc:                       # noqa: BLE001 — last-resort guard
        logging.exception("Unhandled error during run")
        code = 1
        if is_live_sync:
            send_alert(cfg["alerts"], "25Live -> Niagara sync CRASHED",
                       f"Unhandled error: {exc}")

    if is_live_sync:
        if code != 0:
            send_alert(cfg["alerts"],
                       f"25Live -> Niagara sync FAILED (exit {code})",
                       EXIT_CODE_HELP.get(code, "See the log for details."))
        elif cfg["alerts"].get("notify_on_success"):
            send_alert(cfg["alerts"], "25Live -> Niagara sync OK",
                       "Sync completed successfully.")

    logging.info("=== Exited with code %d ===", code)
    return code


if __name__ == "__main__":
    sys.exit(main())
