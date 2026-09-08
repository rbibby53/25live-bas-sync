# 25Live -> BAS Schedule Sync — room map loader
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Parses space_mapping.yaml — the cross-reference between 25Live spaces and BAS
schedules.

    buildings:  each building's roll-up schedule, defined ONCE.
    floors:     optional per-floor corridor schedules, keyed by building+level.
    spaces:     the rooms; each room names the building (and optionally floor)
                it belongs to.

Every room that names a building is automatically unioned into that building's
occupancy schedule, so if ANY room in the building is occupied the building
schedule (lobbies, common AHUs) runs too. A room never repeats its building's
target — it just names the building — which makes "all rooms in the building"
the default and removes the chance of forgetting to wire one up.

A room that also names a `floor` feeds that floor's corridor schedule as well,
so occupancy rolls up room -> floor -> building. That is what keeps a single
evening booking on the third floor from conditioning the whole tower while
still lighting and tempering the corridor someone has to walk down.

Inheritance, all with the same precedence — room > building > global:
    pre_condition_minutes   HVAC run-up before a booking
    post_buffer_minutes     run-down after it
    system                  which BAS this schedule lives on
"""

import logging
from pathlib import Path
from typing import Optional

from .config import ConfigError, read_yaml
from .model import Destination, SpaceConfig


class RowError(ValueError):
    """A single row in the room map is unusable. Carries a message for the
    errors list so one bad room is reported, not raised — the other 400 rooms
    should still sync."""


def _space_id(value) -> str:
    """
    Normalise a 25Live space id to the string form the API returns.

    YAML reads an unquoted `1234` as int and `1234.0` as float; str() on the
    latter gives "1234.0", which would never match the "1234" 25Live sends, and
    the room would silently never sync. Integral floats are folded back to int.
    """
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _require_int(value, what: str, where: str) -> int:
    """int(value) or a RowError naming the field — a typo'd number is a
    reportable mapping mistake, not a traceback."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise RowError(f"{where}: `{what}` must be a whole number, got "
                       f"{value!r}.") from None


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


def _int_or_default_checked(value, default: int, what: str, where: str) -> int:
    """_int_or_default, but reporting a bad value instead of raising."""
    if value is None:
        return default
    return _require_int(value, what, where)


def _resolve(room_value, building_value, default):
    """room override > building override > global default.

    A value counts as "set" only when not None, so an explicit 0 (or an empty
    string for a system name) is honored at any level."""
    if room_value is not None:
        return room_value
    if building_value is not None:
        return building_value
    return default


def _target_of(row: dict) -> Optional[str]:
    """
    The schedule address for a row, or None when it has none.

    `target:` is the current key. `niagara_path:` is the pre-1.0 name and is
    still accepted verbatim, so an existing campus map keeps working after an
    upgrade without a mass edit.

    Returning None is meaningful rather than an error: a room in a building
    that can only be scheduled at the floor or air-handler level has no
    schedule of its own, and still contributes its bookings to its roll-ups.
    Buildings and floors, which exist *only* to be a schedule, are checked by
    their callers.
    """
    target = row.get("target")
    if target in (None, ""):
        target = row.get("niagara_path")
    if target in (None, ""):
        return None
    return str(target).strip() or None


class SpaceMap:
    """The loaded room map, plus whatever was wrong with it."""

    def __init__(self, spaces: dict, errors: list, warnings: list,
                 building_count: int = 0, floor_count: int = 0):
        self.spaces = spaces              # { space_id: SpaceConfig }
        self.errors = errors              # fatal — the run should not proceed
        self.warnings = warnings          # worth saying, not worth stopping for
        self.building_count = building_count
        self.floor_count = floor_count

    def __bool__(self) -> bool:
        return bool(self.spaces)

    def __len__(self) -> int:
        return len(self.spaces)

    def destinations(self) -> set:
        """
        Every distinct schedule the sync manages — rooms, floor corridors and
        building roll-ups.

        This is the set that gets cleared when a space has no bookings, so a
        roll-up missing from here is a schedule that would silently keep running
        last week's occupancy forever.
        """
        out = set()
        for sc in self.spaces.values():
            out.update(sc.all_destinations())
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
        data = read_yaml(p)
    except ConfigError as exc:
        # Reported rather than raised: --validate should list every problem it
        # can find in one pass, not stop at the first.
        errors.append(f"Room map {exc}")
        return SpaceMap({}, errors, warnings)

    # ── 1) Index the building definitions by id ──────────────────────────────
    buildings: dict = {}
    building_dest: dict = {}
    for b in (data.get("buildings") or []):
        if not isinstance(b, dict):
            errors.append(f"A `buildings:` entry is not a mapping: {b!r}")
            continue
        bid = str(b.get("id") or "").strip()
        if not bid:
            errors.append("A building has no `id:`.")
            continue
        if bid in buildings:
            errors.append(f"Building id '{bid}' is defined more than once.")
            continue
        buildings[bid] = b
        target = _target_of(b)
        system = str(_resolve(b.get("system"), None, default_system) or "")
        if target is None:
            errors.append(
                f"Building {bid}: no `target:` — a building entry exists to "
                "name its roll-up schedule, so it needs one. Rooms may omit "
                "`target:`; buildings may not.")
        else:
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

    # ── 1b) Index floor corridor schedules by (building id, level) ───────────
    floor_dest: dict = {}
    for f in (data.get("floors") or []):
        if not isinstance(f, dict):
            errors.append(f"A floors: entry is not a mapping: {f!r}")
            continue
        raw_building = f.get("building")
        raw_level = f.get("level")
        if raw_building in (None, "") or raw_level in (None, ""):
            errors.append(
                f"Floor entry {f!r} needs both `building:` and `level:`.")
            continue
        building_id = str(raw_building)
        try:
            level = _require_int(raw_level, "level", f"Floor of '{building_id}'")
        except RowError as exc:
            errors.append(str(exc))
            continue
        if building_id not in buildings:
            errors.append(
                f"Floor {level} references unknown building '{building_id}'.")
            continue

        target = _target_of(f)
        if target is None:
            errors.append(f"Floor {level} of '{building_id}': no `target:` — a "
                          "floor entry exists to name its corridor schedule.")
            continue
        # A floor inherits its building's system unless it says otherwise —
        # a corridor is served by the same panel as the rooms off it far more
        # often than not.
        bld = buildings[building_id]
        system = str(_resolve(f.get("system"), bld.get("system"),
                              default_system) or "")
        if not system:
            errors.append(
                f"Floor {level} of '{building_id}': no `system:` and no default.")
            continue
        if known_systems and system not in known_systems:
            errors.append(
                f"Floor {level} of '{building_id}': system '{system}' is not "
                f"defined under `systems:` in config.yaml.")
            continue
        key = (building_id, level)
        if key in floor_dest:
            errors.append(
                f"Floor {level} of building '{building_id}' is defined twice.")
            continue
        floor_dest[key] = Destination(system=system, target=target)

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
        if not isinstance(row, dict):
            errors.append(f"A `spaces:` entry is not a mapping: {row!r}")
            continue
        raw_id = row.get("space_id")
        if raw_id in (None, ""):
            errors.append(f"A room has no `space_id:` ({row.get('space_name', '?')}).")
            continue
        space_id = _space_id(raw_id)
        where = f"Room {space_id}"

        building = None
        bdest = None
        building_id = row.get("building")
        if building_id not in (None, ""):
            building_id = str(building_id)
            building = buildings.get(building_id)
            if building is None:
                warnings.append(
                    f"{where} references unknown building '{building_id}' — it "
                    "will NOT roll up. Add it under buildings: or fix the name.")
            else:
                bdest = building_dest.get(building_id)

        # Optional per-floor corridor schedule (room -> floor -> building).
        # Only meaningful for a room that belongs to a building, since floors
        # are keyed by building.
        floor = None
        fdest = None
        if row.get("floor") not in (None, ""):
            try:
                floor = _require_int(row["floor"], "floor", where)
            except RowError as exc:
                warnings.append(f"{exc} Ignoring the floor.")
            if floor is not None:
                if building_id in (None, ""):
                    warnings.append(
                        f"{where} names floor {floor} but no building, so there "
                        "is nothing to look the floor up against — it will NOT "
                        "drive a corridor schedule.")
                else:
                    fdest = floor_dest.get((str(building_id), floor))
                    if fdest is None:
                        warnings.append(
                            f"{where} references floor {floor} of building "
                            f"'{building_id}' with no matching floors: entry — "
                            "it will NOT drive a corridor schedule.")

        bld = building or {}
        system = str(_resolve(row.get("system"), bld.get("system"),
                              default_system) or "")
        if not system:
            errors.append(
                f"{where}: no `system:` and no default. Set `default_system:` "
                "in config.yaml or name one per room.")
            continue
        if known_systems and system not in known_systems:
            errors.append(
                f"{where}: system '{system}' is not defined under `systems:` in "
                f"config.yaml. Known: "
                f"{', '.join(sorted(known_systems)) or '(none)'}.")
            continue

        # A room without its own `target:` is normal: plenty of buildings can
        # only be scheduled at the floor or air-handler level. It still feeds
        # its roll-ups. What is NOT useful is a room that drives nothing at
        # all — that room's bookings would vanish silently, so say so.
        target = _target_of(row)
        rdest = Destination(system=system, target=target) if target else None
        if rdest is None and bdest is None and fdest is None:
            errors.append(
                f"{where}: no `target:` and no roll-up to contribute to, so its "
                "bookings would drive nothing. Give it a `target:`, or a "
                "`building:` (and optionally `floor:`) to roll up into.")
            continue

        try:
            space = SpaceConfig(
                space_id=space_id,
                space_name=str(row.get("space_name") or space_id),
                space_type="room",
                destination=rdest,
                building_destination=bdest,
                pre_condition_minutes=_require_int(
                    _resolve(row.get("pre_condition_minutes"),
                             bld.get("pre_condition_minutes"), default_pre),
                    "pre_condition_minutes", where),
                post_buffer_minutes=_require_int(
                    _resolve(row.get("post_buffer_minutes"),
                             bld.get("post_buffer_minutes"), default_post),
                    "post_buffer_minutes", where),
                merge_gap_minutes=_int_or_default_checked(
                    row.get("merge_gap_minutes"), default_gap,
                    "merge_gap_minutes", where),
                floor=floor,
                floor_destination=fdest,
            )
        except RowError as exc:
            errors.append(str(exc))
            continue

        _register(space_id, space, f"room {row.get('space_name', space_id)}")

    # ── 3) Buildings that are themselves bookable in 25Live ──────────────────
    #     e.g. an atrium with its own 25Live space. Its own events then count
    #     toward the building schedule alongside the room roll-up.
    for building_id, b in buildings.items():
        if b.get("space_id") in (None, ""):
            continue
        dest = building_dest.get(building_id)
        if dest is None:
            continue
        space_id = _space_id(b["space_id"])
        where = f"Building {building_id}"
        try:
            space = SpaceConfig(
                space_id=space_id,
                space_name=str(b.get("name") or building_id),
                space_type="building",
                destination=dest,
                building_destination=None,
                pre_condition_minutes=_int_or_default_checked(
                    b.get("pre_condition_minutes"), default_pre,
                    "pre_condition_minutes", where),
                post_buffer_minutes=_int_or_default_checked(
                    b.get("post_buffer_minutes"), default_post,
                    "post_buffer_minutes", where),
                merge_gap_minutes=_int_or_default_checked(
                    b.get("merge_gap_minutes"), default_gap,
                    "merge_gap_minutes", where),
            )
        except RowError as exc:
            errors.append(str(exc))
            continue
        _register(space_id, space, f"building {building_id}")

    # ── 4) Two rooms pointing at one schedule ────────────────────────────────
    #     Legal and sometimes intentional (an air-wall room split into A/B in
    #     25Live but served by one AHU). The builder unions them rather than
    #     letting one overwrite the other, but say so — more often it is a
    #     copy-paste slip.
    seen: dict = {}
    for sc in space_map.values():
        if sc.space_type != "room" or sc.destination is None:
            continue
        seen.setdefault(sc.destination, []).append(sc.space_id)
    for dest, ids in seen.items():
        if len(ids) > 1:
            warnings.append(
                f"Schedule {dest} is the target of {len(ids)} rooms "
                f"({', '.join(sorted(ids))}). Their bookings are unioned — "
                "intended for a divisible room, a mistake otherwise.")

    rooms = [s for s in space_map.values() if s.space_type == "room"]
    rollup_only = sum(1 for s in rooms if s.destination is None)
    logging.info(
        "Loaded %d rooms (%d roll-up only) across %d buildings, %d floor "
        "schedules, from %s",
        len(rooms), rollup_only, len(buildings), len(floor_dest), path)
    for w in warnings:
        logging.warning("%s", w)
    return SpaceMap(space_map, errors, warnings,
                    building_count=len(buildings), floor_count=len(floor_dest))
