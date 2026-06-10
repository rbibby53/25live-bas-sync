#!/usr/bin/env python3
"""
Offline tests for the pure logic in main.py — no 25Live or Niagara needed.

Covers:
  - ScheduleBuilder: event merging + building roll-up.
  - load_space_map: rooms auto-union into their building's schedule.

Run with:
    python Test.py
"""

import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

from main import (
    RawEvent, SpaceConfig, ScheduleBuilder, OccupancyWindow,
    load_space_map, CONFIG,
)

TZ = ZoneInfo("America/New_York")


def dt(hour: int, minute: int = 0, day: int = 1) -> datetime:
    return datetime(2026, 6, day, hour, minute, tzinfo=TZ)


def space(space_id, ntype, niagara_path, building=None, merge_gap=5) -> SpaceConfig:
    return SpaceConfig(
        space_id=str(space_id), space_name=str(space_id), space_type=ntype,
        niagara_path=niagara_path, building_schedule_path=building,
        pre_condition_minutes=0, post_buffer_minutes=0,
        merge_gap_minutes=merge_gap,
    )


def event(space_id, start, end, eid="E1") -> RawEvent:
    return RawEvent(event_id=eid, title="t", space_id=str(space_id),
                    start=start, end=end)


def test_adjacent_events_merge():
    """Two back-to-back events (within the gap) collapse into one window."""
    builder = ScheduleBuilder(default_merge_gap_minutes=5)
    space_map = {"1": space(1, "room", "Bldg/Rm1")}
    events = [
        event(1, dt(9), dt(10)),
        event(1, dt(10, 3), dt(11)),   # 3-min gap < 5-min merge gap
    ]
    result = builder.build(events, space_map)
    windows = result["Bldg/Rm1"]
    assert len(windows) == 1, windows
    assert windows[0].start == dt(9) and windows[0].end == dt(11), windows[0]


def test_separated_events_stay_split():
    """Events further apart than the gap remain two windows."""
    builder = ScheduleBuilder(default_merge_gap_minutes=5)
    space_map = {"1": space(1, "room", "Bldg/Rm1")}
    events = [
        event(1, dt(9), dt(10)),
        event(1, dt(11), dt(12)),      # 1-hour gap
    ]
    result = builder.build(events, space_map)
    assert len(result["Bldg/Rm1"]) == 2, result["Bldg/Rm1"]


def test_building_rollup_unions_rooms():
    """Two rooms feeding one building schedule produce a merged building window."""
    builder = ScheduleBuilder(default_merge_gap_minutes=5)
    space_map = {
        "1": space(1, "room", "Bldg/Rm1", building="Bldg/Building_Occ"),
        "2": space(2, "room", "Bldg/Rm2", building="Bldg/Building_Occ"),
    }
    events = [
        event(1, dt(9), dt(10), "E1"),    # room 1: 09-10
        event(2, dt(10), dt(11), "E2"),   # room 2: 10-11 (adjacent to room 1)
    ]
    result = builder.build(events, space_map)
    # Each room keeps its own window...
    assert len(result["Bldg/Rm1"]) == 1
    assert len(result["Bldg/Rm2"]) == 1
    # ...and the building rolls both up into a single 09-11 window.
    bwin = result["Bldg/Building_Occ"]
    assert len(bwin) == 1, bwin
    assert bwin[0].start == dt(9) and bwin[0].end == dt(11), bwin[0]


def test_disjoint_rooms_give_building_two_windows():
    """If two rooms are occupied at different times, the building is occupied
    for BOTH windows (union, not just overlap)."""
    builder = ScheduleBuilder(default_merge_gap_minutes=5)
    space_map = {
        "1": space(1, "room", "Bldg/Rm1", building="Bldg/Building_Occ"),
        "2": space(2, "room", "Bldg/Rm2", building="Bldg/Building_Occ"),
    }
    events = [
        event(1, dt(9), dt(10), "E1"),     # morning booking in room 1
        event(2, dt(14), dt(15), "E2"),    # afternoon booking in room 2
    ]
    result = builder.build(events, space_map)
    bwin = result["Bldg/Building_Occ"]
    assert len(bwin) == 2, bwin


def test_per_room_merge_gap_overrides_default():
    """A room with a wide merge_gap_minutes collapses windows that the global
    default would have kept separate."""
    builder = ScheduleBuilder(default_merge_gap_minutes=5)
    space_map = {
        "1": space(1, "room", "Bldg/Rm1", merge_gap=30),   # wide gap
        "2": space(2, "room", "Bldg/Rm2", merge_gap=5),    # default gap
    }
    # 20-minute gap between the two bookings in each room.
    events = [
        event(1, dt(9), dt(10), "A1"), event(1, dt(10, 20), dt(11), "A2"),
        event(2, dt(9), dt(10), "B1"), event(2, dt(10, 20), dt(11), "B2"),
    ]
    result = builder.build(events, space_map)
    assert len(result["Bldg/Rm1"]) == 1, result["Bldg/Rm1"]   # merged (30 >= 20)
    assert len(result["Bldg/Rm2"]) == 2, result["Bldg/Rm2"]   # split  (5 < 20)


def test_room_gap_carries_into_building_contribution():
    """A room's own wide gap merges its windows, and that merged occupancy
    carries into the building (the building reflects when the room is occupied)."""
    builder = ScheduleBuilder(default_merge_gap_minutes=5)
    space_map = {
        "1": space(1, "room", "Bldg/Rm1", building="Bldg/Building_Occ", merge_gap=30),
    }
    # Two bookings 20 min apart. Room gap 30 merges them into 09-11, and the
    # building inherits that single window.
    events = [
        event(1, dt(9), dt(10), "A1"),
        event(1, dt(10, 20), dt(11), "A2"),
    ]
    result = builder.build(events, space_map)
    assert len(result["Bldg/Rm1"]) == 1, result["Bldg/Rm1"]
    assert len(result["Bldg/Building_Occ"]) == 1, result["Bldg/Building_Occ"]


def test_cross_room_building_merge_uses_default_gap():
    """Merging windows from DIFFERENT rooms into the building uses the global
    default gap, regardless of either room's own gap override."""
    space_map = {
        "1": space(1, "room", "Bldg/Rm1", building="Bldg/Building_Occ", merge_gap=30),
        "2": space(2, "room", "Bldg/Rm2", building="Bldg/Building_Occ", merge_gap=30),
    }
    # Room 1 occupied 09-10, room 2 occupied 10:20-11 — 20 min apart, in two
    # different rooms (each a single event, so no intra-room merge happens).
    events = [event(1, dt(9), dt(10), "A"), event(2, dt(10, 20), dt(11), "B")]

    # Default gap 5: the 20-min cross-room gap is NOT bridged -> two windows.
    r_tight = ScheduleBuilder(default_merge_gap_minutes=5).build(events, space_map)
    assert len(r_tight["Bldg/Building_Occ"]) == 2, r_tight["Bldg/Building_Occ"]

    # Default gap 30: now the building bridges the cross-room gap -> one window.
    r_wide = ScheduleBuilder(default_merge_gap_minutes=30).build(events, space_map)
    assert len(r_wide["Bldg/Building_Occ"]) == 1, r_wide["Bldg/Building_Occ"]


def test_loader_reads_per_room_merge_gap():
    """load_space_map picks up merge_gap_minutes per room, else the default."""
    yaml_text = """
buildings:
  - id: b
    niagara_path: "B/Building_Occ"
spaces:
  - space_id: 1
    niagara_path: "B/Rm1"
    merge_gap_minutes: 25
  - space_id: 2
    niagara_path: "B/Rm2"
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(yaml_text)
        path = fh.name
    try:
        space_map = load_space_map(path, CONFIG)
    finally:
        os.unlink(path)
    assert space_map["1"].merge_gap_minutes == 25
    # Room 2 has no override -> falls back to the global default.
    assert space_map["2"].merge_gap_minutes == CONFIG["collegenet"]["merge_gap_minutes"]


def test_overlap_helper():
    """Sanity-check the OccupancyWindow overlap primitive directly."""
    a = OccupancyWindow(dt(9), dt(10))
    b = OccupancyWindow(dt(10, 4), dt(11))   # 4-min gap
    assert a.overlaps_or_adjacent(b, gap_minutes=5)
    assert not a.overlaps_or_adjacent(b, gap_minutes=3)


def test_loader_assigns_all_rooms_to_building():
    """load_space_map resolves each room's `building` id to the building's
    Niagara path, so all rooms auto-roll-up — without repeating the path."""
    yaml_text = """
buildings:
  - id: bldg_a
    name: "Building A"
    niagara_path: "A/Building_Occ"
spaces:
  - space_id: 11
    building: bldg_a
    niagara_path: "A/Rm11_Occ"
  - space_id: 12
    building: bldg_a
    niagara_path: "A/Rm12_Occ"
  - space_id: 13
    niagara_path: "A/Rm13_Occ"   # no building -> should NOT roll up
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(yaml_text)
        path = fh.name
    try:
        space_map = load_space_map(path, CONFIG)
    finally:
        os.unlink(path)

    # Both bldg_a rooms resolve to the same building schedule path...
    assert space_map["11"].building_schedule_path == "A/Building_Occ"
    assert space_map["12"].building_schedule_path == "A/Building_Occ"
    # ...and the unassigned room rolls up to nothing.
    assert space_map["13"].building_schedule_path is None


def test_editor_roundtrip_feeds_loader():
    """The GUI editor's dump -> the sync's load_space_map: a room added in the
    editor resolves to its building's schedule path, end to end."""
    import editor

    buildings = [{"id": "bldg_a", "name": "Building A",
                  "niagara_path": "A/Building_Occ"}]
    rooms = [
        {"space_id": 11, "space_name": "A 101", "building": "bldg_a",
         "niagara_path": "A/Rm101_Occ", "pre_condition_minutes": 30},
        {"space_id": 12, "niagara_path": "A/Rm102_Occ"},   # no building
    ]
    text = editor.dump_mapping(buildings, rooms)

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(text)
        path = fh.name
    try:
        # Round-trips through the editor's own loader...
        b2, r2 = editor.load_mapping(path)
        assert len(b2) == 1 and len(r2) == 2, (b2, r2)
        # ...and is consumed correctly by the sync's loader.
        space_map = load_space_map(path, CONFIG)
    finally:
        os.unlink(path)

    assert space_map["11"].building_schedule_path == "A/Building_Occ"
    assert space_map["12"].building_schedule_path is None


def test_editor_flags_unknown_building():
    """unknown_building_refs catches a room pointing at a missing building."""
    import editor
    buildings = [{"id": "bldg_a", "niagara_path": "A/Building_Occ"}]
    rooms = [{"space_id": 11, "building": "typo_bldg", "niagara_path": "A/Rm.Occ"}]
    assert editor.unknown_building_refs(buildings, rooms) == ["11"]


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {t.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
