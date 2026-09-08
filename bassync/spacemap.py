# 25Live -> BAS Schedule Sync — room map loader
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Parses space_mapping.yaml — the cross-reference between 25Live spaces and BAS
schedules.

    buildings:  each building's roll-up schedule, defined ONCE.
    spaces:     the rooms; each room names the building it belongs to.

Every room that names a building is automatically unioned into that building's
occupancy schedule, so if ANY room in the building is occupied the building
schedule (hallways, lobbies, common AHUs) runs too. A room never repeats its
building's target — it just names the building — which makes "all rooms in the
building" the default and removes the chance of forgetting to wire one up.

Inheritance, all with the same precedence — room > building > global:
    pre_condition_minutes   HVAC run-up before a booking
    post_buffer_minutes     run-down after it
    system                  which BAS this schedule lives on
"""

import logging
from pathlib import Path
from typing import Optional

import yaml

from .model import Destination, SpaceConfig


def _int_or_default(value, default: int) -> int:
    """
    Return the configured value, falling back to `default` only when it is
    truly absent (None).

    A plain `value or default` would wrongly replace an explicit 0 — a room
    that intentionally sets pre_condition_minutes: 0 to disable pre-
    conditioning would silently get the 30-minute default instead, and start
    conditioning half an hour before every booking.
    """
    return default if value is None else int(value)


def _resolve(room_value, building_value, default):
    """room override > building override > global default.

    A value counts as "set" only when not None, so an explicit 0 (or an empty
    string for a system name) is honored at any level."""
    if room_value is not None:
        return room_value
    if building_value is not None:
        return building_value
    return default


def _target_of(row: dict, what: str, where: str,
               errors: list) -> Optional[str]:
    """
    The schedule address for a row.

    `target:` is the current key. `niagara_path:` is the pre-1.0 name and is
    still accepted verbatim, so an existing campus map keeps working after the
    upgrade without a mass edit.
    """
    target = row.get("target")
    if target in (None, ""):
        target = row.get("niagara_path")
    if target in (None, ""):
        errors.append(f"{what} {where}: no `target:` (or legacy `niagara_path:`).")
        return None
    return str(target)


class SpaceMap:
    """The loaded room map, plus whatever was wrong with it."""

    def __init__(self, spaces: dict, errors: list, warnings: list,
                 building_count: int = 0):
        self.spaces = spaces              # { space_id: SpaceConfig }
        self.errors = errors              # fatal — the run should not proceed
        self.warnings = warnings          # worth saying, not worth stopping for
        self.building_count = building_count

    def __bool__(self) -> bool:
        return bool(self.spaces)

    def __len__(self) -> int:
        return len(self.spaces)

    def destinations(self) -> set:
        """Every distinct schedule the sync would write — rooms and roll-ups."""
        out = set()
        for sc in self.spaces.values():
            out.add(sc.destination)
            if sc.building_destination:
                out.add(sc.building_destination)
        return out

    def systems_used(self) -> set:
        return {d.system for d in self.destinations()}


def load_space_map(path: str, cfg: dict) -> SpaceMap:
    """Parse space_mapping.yaml into a SpaceMap. Never raises on bad content —
    problems come back as `errors` so --validate can report them all at once
    instead of failing on the first one."""
    from .config import resolve_default_system

    cn = cfg["collegenet"]
    default_pre = cn["default_pre_condition_minutes"]
    default_post = cn["default_post_buffer_minutes"]
    default_gap = cn["merge_gap_minutes"]
    default_system = resolve_default_system(cfg)
    known_systems = set(cfg.get("systems") or {})

    errors: list = []
    warnings: list = []

    p = Path(path)
    if not p.exists():
        errors.append(
            f"Room map not found: {path} — copy space_mapping.example.yaml to "
            "space_mapping.yaml (or run editor.py) and add your rooms.")
        return SpaceMap({}, errors, warnings)

    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        errors.append(f"Room map {path} is not valid YAML: {exc}")
        return SpaceMap({}, errors, warnings)

    if not isinstance(data, dict):
        errors.append(f"Room map {path} must be a mapping with `buildings:` "
                      "and `spaces:` sections.")
        return SpaceMap({}, errors, warnings)

    # ── 1) Index the building definitions by id ──────────────────────────────
    buildings: dict = {}
    building_dest: dict = {}
    for b in (data.get("buildings") or []):
        bid = str(b.get("id") or "").strip()
        if not bid:
            errors.append("A building has no `id:`.")
            continue
        if bid in buildings:
            errors.append(f"Building id '{bid}' is defined more than once.")
            continue
        buildings[bid] = b
        target = _target_of(b, "Building", bid, errors)
        system = str(_resolve(b.get("system"), None, default_system) or "")
        if target is not None:
            if not system:
                errors.append(
                    f"Building {bid}: no `system:` and no default. Set "
                    "`default_system:` in config.yaml or name one per building.")
            elif known_systems and system not in known_systems:
                errors.append(
                    f"Building {bid}: system '{system}' is not defined under "
                    f"`systems:` in config.yaml. Known: "
                    f"{', '.join(sorted(known_systems)) or '(none)'}.")
            else:
                building_dest[bid] = Destination(system=system, target=target)

    space_map: dict = {}

    def _register(space_id: str, sc: SpaceConfig, label: str) -> None:
        if space_id in space_map:
            errors.append(
                f"25Live space_id {space_id} is mapped twice ({label} and "
                f"{space_map[space_id].space_name}). Each space may appear once.")
            return
        space_map[space_id] = sc

    # ── 2) Rooms ─────────────────────────────────────────────────────────────
    for row in (data.get("spaces") or []):
        raw_id = row.get("space_id")
        if raw_id in (None, ""):
            errors.append(f"A room has no `space_id:` ({row.get('space_name', '?')}).")
            continue
        space_id = str(raw_id)
        target = _target_of(row, "Room", space_id, errors)
        if target is None:
            continue

        building = None
        bdest = None
        building_id = row.get("building")
        if building_id not in (None, ""):
            building_id = str(building_id)
            building = buildings.get(building_id)
            if building is None:
                warnings.append(
                    f"Room {space_id} references unknown building "
                    f"'{building_id}' — it will NOT roll up. Add it under "
                    "buildings: or fix the name.")
            else:
                bdest = building_dest.get(building_id)

        bld = building or {}
        system = str(_resolve(row.get("system"), bld.get("system"),
                              default_system) or "")
        if not system:
            errors.append(
                f"Room {space_id}: no `system:` and no default. Set "
                "`default_system:` in config.yaml or name one per room.")
            continue
        if known_systems and system not in known_systems:
            errors.append(
                f"Room {space_id}: system '{system}' is not defined under "
                f"`systems:` in config.yaml. Known: "
                f"{', '.join(sorted(known_systems)) or '(none)'}.")
            continue

        _register(space_id, SpaceConfig(
            space_id=space_id,
            space_name=str(row.get("space_name") or space_id),
            space_type="room",
            destination=Destination(system=system, target=target),
            building_destination=bdest,
            pre_condition_minutes=int(_resolve(
                row.get("pre_condition_minutes"),
                bld.get("pre_condition_minutes"), default_pre)),
            post_buffer_minutes=int(_resolve(
                row.get("post_buffer_minutes"),
                bld.get("post_buffer_minutes"), default_post)),
            merge_gap_minutes=_int_or_default(
                row.get("merge_gap_minutes"), default_gap),
        ), f"room {row.get('space_name', space_id)}")

    # ── 3) Buildings that are themselves bookable in 25Live ──────────────────
    #     e.g. an atrium with its own 25Live space. Its own events then count
    #     toward the building schedule alongside the room roll-up.
    for building_id, b in buildings.items():
        if b.get("space_id") in (None, ""):
            continue
        dest = building_dest.get(building_id)
        if dest is None:
            continue
        space_id = str(b["space_id"])
        _register(space_id, SpaceConfig(
            space_id=space_id,
            space_name=str(b.get("name") or building_id),
            space_type="building",
            destination=dest,
            building_destination=None,
            pre_condition_minutes=_int_or_default(
                b.get("pre_condition_minutes"), default_pre),
            post_buffer_minutes=_int_or_default(
                b.get("post_buffer_minutes"), default_post),
            merge_gap_minutes=_int_or_default(
                b.get("merge_gap_minutes"), default_gap),
        ), f"building {building_id}")

    # ── 4) Two rooms pointing at one schedule ────────────────────────────────
    #     Legal and sometimes intentional (an air-wall room split into A/B in
    #     25Live but served by one AHU). The builder unions them rather than
    #     letting one overwrite the other, but say so — more often it is a
    #     copy-paste slip.
    seen: dict = {}
    for sc in space_map.values():
        if sc.space_type != "room":
            continue
        seen.setdefault(sc.destination, []).append(sc.space_id)
    for dest, ids in seen.items():
        if len(ids) > 1:
            warnings.append(
                f"Schedule {dest} is the target of {len(ids)} rooms "
                f"({', '.join(sorted(ids))}). Their bookings are unioned — "
                "intended for a divisible room, a mistake otherwise.")

    n_rooms = sum(1 for s in space_map.values() if s.space_type == "room")
    logging.info("Loaded %d rooms across %d buildings from %s",
                 n_rooms, len(buildings), path)
    for w in warnings:
        logging.warning("%s", w)
    return SpaceMap(space_map, errors, warnings, building_count=len(buildings))
