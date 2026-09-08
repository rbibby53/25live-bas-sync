#!/usr/bin/env python3
"""
Offline tests — no 25Live, no BAS, no network.

Covers the parts where a bug is expensive and silent: the merge/roll-up logic,
the room map loader and its inheritance rules, the BACnet exception-schedule
encoding, the mass-clear safety rail, and the editor's YAML round-trip.

Run with:
    python Test.py
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bassync.config import load_config, migrate_legacy_systems, resolve_default_system
from bassync.model import Destination, OccupancyWindow, RawEvent, SpaceConfig
from bassync.schedule import ScheduleBuilder
from bassync.spacemap import load_space_map

TZ = ZoneInfo("America/New_York")


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def dt(hour: int, minute: int = 0, day: int = 1) -> datetime:
    return datetime(2026, 6, day, hour, minute, tzinfo=TZ)


def dest(target: str, system: str = "sys") -> Destination:
    return Destination(system=system, target=target)


def space(space_id, kind, target, building=None, merge_gap=5,
          system="sys") -> SpaceConfig:
    return SpaceConfig(
        space_id=str(space_id), space_name=str(space_id), space_type=kind,
        destination=dest(target, system),
        building_destination=dest(building, system) if building else None,
        pre_condition_minutes=0, post_buffer_minutes=0,
        merge_gap_minutes=merge_gap,
    )


def event(space_id, start, end, eid="E1") -> RawEvent:
    return RawEvent(event_id=eid, title="t", space_id=str(space_id),
                    start=start, end=end)


def with_yaml(text: str, fn):
    """Run fn(path) against a temp YAML file, always cleaning up."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(text)
        path = fh.name
    try:
        return fn(path)
    finally:
        os.unlink(path)


def base_config(**overrides) -> dict:
    cfg = load_config("/nonexistent/config.yaml")
    cfg["systems"] = {"sys": {"driver": "preview"}}
    cfg["default_system"] = "sys"
    cfg.update(overrides)
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# merging + building roll-up
# ─────────────────────────────────────────────────────────────────────────────

def test_adjacent_events_merge():
    """Two back-to-back events (within the gap) collapse into one window."""
    result = ScheduleBuilder(5).build(
        [event(1, dt(9), dt(10)), event(1, dt(10, 3), dt(11))],
        {"1": space(1, "room", "Bldg/Rm1")})
    windows = result[dest("Bldg/Rm1")]
    assert len(windows) == 1, windows
    assert windows[0].start == dt(9) and windows[0].end == dt(11), windows[0]


def test_separated_events_stay_split():
    """Events further apart than the gap remain two windows."""
    result = ScheduleBuilder(5).build(
        [event(1, dt(9), dt(10)), event(1, dt(11), dt(12))],
        {"1": space(1, "room", "Bldg/Rm1")})
    assert len(result[dest("Bldg/Rm1")]) == 2


def test_building_rollup_unions_rooms():
    """Two rooms feeding one building schedule produce a merged window."""
    space_map = {
        "1": space(1, "room", "Bldg/Rm1", building="Bldg/Occ"),
        "2": space(2, "room", "Bldg/Rm2", building="Bldg/Occ"),
    }
    result = ScheduleBuilder(5).build(
        [event(1, dt(9), dt(10), "E1"), event(2, dt(10), dt(11), "E2")], space_map)
    assert len(result[dest("Bldg/Rm1")]) == 1
    assert len(result[dest("Bldg/Rm2")]) == 1
    bwin = result[dest("Bldg/Occ")]
    assert len(bwin) == 1, bwin
    assert bwin[0].start == dt(9) and bwin[0].end == dt(11), bwin[0]


def test_disjoint_rooms_give_building_two_windows():
    """Rooms occupied at different times leave the building occupied for BOTH
    windows (a union, not an intersection)."""
    space_map = {
        "1": space(1, "room", "Bldg/Rm1", building="Bldg/Occ"),
        "2": space(2, "room", "Bldg/Rm2", building="Bldg/Occ"),
    }
    result = ScheduleBuilder(5).build(
        [event(1, dt(9), dt(10), "E1"), event(2, dt(14), dt(15), "E2")], space_map)
    assert len(result[dest("Bldg/Occ")]) == 2


def test_per_room_merge_gap_overrides_default():
    """A room with a wide merge_gap collapses windows the global default keeps
    separate."""
    space_map = {
        "1": space(1, "room", "Bldg/Rm1", merge_gap=30),
        "2": space(2, "room", "Bldg/Rm2", merge_gap=5),
    }
    events = [event(1, dt(9), dt(10), "A1"), event(1, dt(10, 20), dt(11), "A2"),
              event(2, dt(9), dt(10), "B1"), event(2, dt(10, 20), dt(11), "B2")]
    result = ScheduleBuilder(5).build(events, space_map)
    assert len(result[dest("Bldg/Rm1")]) == 1, result[dest("Bldg/Rm1")]
    assert len(result[dest("Bldg/Rm2")]) == 2, result[dest("Bldg/Rm2")]


def test_cross_room_building_merge_uses_default_gap():
    """Merging windows from DIFFERENT rooms into the building uses the global
    default gap, regardless of either room's own override."""
    space_map = {
        "1": space(1, "room", "Bldg/Rm1", building="Bldg/Occ", merge_gap=30),
        "2": space(2, "room", "Bldg/Rm2", building="Bldg/Occ", merge_gap=30),
    }
    events = [event(1, dt(9), dt(10), "A"), event(2, dt(10, 20), dt(11), "B")]
    tight = ScheduleBuilder(5).build(events, space_map)
    assert len(tight[dest("Bldg/Occ")]) == 2, tight[dest("Bldg/Occ")]
    wide = ScheduleBuilder(30).build(events, space_map)
    assert len(wide[dest("Bldg/Occ")]) == 1, wide[dest("Bldg/Occ")]


def test_two_rooms_sharing_one_target_are_unioned():
    """A divisible room mapped as two 25Live spaces onto ONE schedule keeps
    both halves' bookings. Regression: the old builder assigned rather than
    unioned, so whichever room was processed second silently erased the first
    and half the room never got conditioned."""
    space_map = {
        "1": space(1, "room", "Bldg/Rm100"),
        "2": space(2, "room", "Bldg/Rm100"),
    }
    result = ScheduleBuilder(5).build(
        [event(1, dt(9), dt(10), "A"), event(2, dt(14), dt(15), "B")], space_map)
    windows = result[dest("Bldg/Rm100")]
    assert len(windows) == 2, windows
    assert {w.start for w in windows} == {dt(9), dt(14)}, windows


def test_builder_skips_unmapped_space():
    """An event for a space that isn't in the map can't take the run down."""
    result = ScheduleBuilder(5).build([event(99, dt(9), dt(10))],
                                      {"1": space(1, "room", "Bldg/Rm1")})
    assert result == {}, result


def test_direct_building_booking_joins_rollup():
    """A bookable common area's own events land on the building schedule
    alongside the rooms that roll up into it."""
    space_map = {
        "1": space(1, "room", "Bldg/Rm1", building="Bldg/Occ"),
        "9": space(9, "building", "Bldg/Occ"),
    }
    result = ScheduleBuilder(5).build(
        [event(1, dt(9), dt(10), "R"), event(9, dt(13), dt(14), "A")], space_map)
    assert len(result[dest("Bldg/Occ")]) == 2, result[dest("Bldg/Occ")]


def test_overlap_helper():
    """Sanity-check the OccupancyWindow overlap primitive directly."""
    a = OccupancyWindow(dt(9), dt(10))
    b = OccupancyWindow(dt(10, 4), dt(11))
    assert a.overlaps_or_adjacent(b, gap_minutes=5)
    assert not a.overlaps_or_adjacent(b, gap_minutes=3)


# ─────────────────────────────────────────────────────────────────────────────
# room map loader
# ─────────────────────────────────────────────────────────────────────────────

def test_loader_assigns_all_rooms_to_building():
    """Each room's `building` id resolves to the building's destination, so all
    rooms auto-roll-up without repeating the address."""
    text = """
buildings:
  - id: bldg_a
    name: "Building A"
    target: "A/Building_Occ"
spaces:
  - space_id: 11
    building: bldg_a
    target: "A/Rm11_Occ"
  - space_id: 12
    building: bldg_a
    target: "A/Rm12_Occ"
  - space_id: 13
    target: "A/Rm13_Occ"
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert not sm.errors, sm.errors
    assert sm.spaces["11"].building_destination == dest("A/Building_Occ")
    assert sm.spaces["12"].building_destination == dest("A/Building_Occ")
    assert sm.spaces["13"].building_destination is None


def test_loader_accepts_legacy_niagara_path():
    """A pre-1.0 map using `niagara_path:` still loads unchanged, so an
    existing campus upgrades without a mass edit."""
    text = """
buildings:
  - id: b
    niagara_path: "B/Building_Occ"
spaces:
  - space_id: 1
    building: b
    niagara_path: "B/Rm1"
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert not sm.errors, sm.errors
    assert sm.spaces["1"].destination == dest("B/Rm1")
    assert sm.spaces["1"].building_destination == dest("B/Building_Occ")


def test_loader_reads_per_room_merge_gap():
    """merge_gap_minutes is picked up per room, else the global default."""
    text = """
buildings: []
spaces:
  - space_id: 1
    target: "B/Rm1"
    merge_gap_minutes: 25
  - space_id: 2
    target: "B/Rm2"
"""
    cfg = base_config()
    sm = with_yaml(text, lambda p: load_space_map(p, cfg))
    assert sm.spaces["1"].merge_gap_minutes == 25
    assert sm.spaces["2"].merge_gap_minutes == cfg["collegenet"]["merge_gap_minutes"]


def test_loader_preserves_explicit_zero_buffers():
    """An explicit 0 must be honored, not replaced by the default. Regression
    for the `value or default` coalescing bug — a room that deliberately
    disables pre-conditioning would otherwise start 30 minutes early."""
    text = """
buildings: []
spaces:
  - space_id: 1
    target: "B/Rm1"
    pre_condition_minutes: 0
    post_buffer_minutes: 0
    merge_gap_minutes: 0
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    s = sm.spaces["1"]
    assert (s.pre_condition_minutes, s.post_buffer_minutes,
            s.merge_gap_minutes) == (0, 0, 0), s


def test_building_runup_overrides_global_but_not_room():
    """Run-up/run-down precedence: room > building > global."""
    text = """
buildings:
  - id: b
    target: "B/Bldg"
    pre_condition_minutes: 50
    post_buffer_minutes: 20
spaces:
  - space_id: 1
    building: b
    target: "B/Rm1"
  - space_id: 2
    building: b
    target: "B/Rm2"
    pre_condition_minutes: 5
  - space_id: 3
    target: "B/Rm3"
"""
    cfg = base_config()
    sm = with_yaml(text, lambda p: load_space_map(p, cfg))
    assert sm.spaces["1"].pre_condition_minutes == 50
    assert sm.spaces["1"].post_buffer_minutes == 20
    assert sm.spaces["2"].pre_condition_minutes == 5
    assert sm.spaces["2"].post_buffer_minutes == 20
    assert sm.spaces["3"].pre_condition_minutes == \
        cfg["collegenet"]["default_pre_condition_minutes"]


def test_system_precedence_room_over_building_over_default():
    """`system` inherits with the same precedence as the minute settings, so a
    building can move to a new BAS in one edit."""
    text = """
buildings:
  - id: b
    system: niagara
    target: "B/Bldg"
spaces:
  - space_id: 1
    building: b
    target: "B/Rm1"
  - space_id: 2
    building: b
    system: bacnet_campus
    target: "12001:5"
  - space_id: 3
    target: "B/Rm3"
"""
    cfg = base_config()
    cfg["systems"] = {"niagara": {"driver": "preview"},
                      "bacnet_campus": {"driver": "preview"},
                      "sys": {"driver": "preview"}}
    sm = with_yaml(text, lambda p: load_space_map(p, cfg))
    assert not sm.errors, sm.errors
    assert sm.spaces["1"].destination.system == "niagara"
    assert sm.spaces["2"].destination.system == "bacnet_campus"
    assert sm.spaces["3"].destination.system == "sys"      # the default
    assert sm.spaces["1"].building_destination.system == "niagara"


def test_loader_rejects_unknown_system():
    """A typo'd system name is a hard error, not a silent write somewhere else."""
    text = """
buildings: []
spaces:
  - space_id: 1
    system: typo_system
    target: "B/Rm1"
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert any("typo_system" in e for e in sm.errors), sm.errors


def test_loader_reports_missing_target_and_duplicate_space_id():
    """Structural mistakes come back as errors, all at once."""
    text = """
buildings: []
spaces:
  - space_id: 1
    space_name: "no target"
  - space_id: 2
    target: "B/Rm2"
  - space_id: 2
    target: "B/Rm2dup"
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert any("no `target:`" in e for e in sm.errors), sm.errors
    assert any("mapped twice" in e for e in sm.errors), sm.errors


def test_loader_warns_on_shared_target():
    """Two rooms on one schedule is legal but worth flagging."""
    text = """
buildings: []
spaces:
  - space_id: 1
    target: "B/Shared"
  - space_id: 2
    target: "B/Shared"
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert not sm.errors, sm.errors
    assert any("2 rooms" in w for w in sm.warnings), sm.warnings


def test_loader_survives_broken_yaml():
    """A malformed map reports an error instead of raising into the run."""
    sm = with_yaml("buildings: [\nspaces:", lambda p: load_space_map(p, base_config()))
    assert sm.errors and not sm.spaces


def test_destinations_include_rollups():
    """Every schedule the sync owns — rooms AND roll-ups — is enumerated.

    Regression: the old clear-loop only checked room paths, so a building whose
    rooms all lost their bookings kept conditioning on last week's schedule
    indefinitely."""
    text = """
buildings:
  - id: b
    target: "B/Occ"
spaces:
  - space_id: 1
    building: b
    target: "B/Rm1"
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert sm.destinations() == {dest("B/Rm1"), dest("B/Occ")}, sm.destinations()


# ─────────────────────────────────────────────────────────────────────────────
# config
# ─────────────────────────────────────────────────────────────────────────────

def test_load_config_merges_and_builds_base_url():
    """config.yaml deep-merges over the built-ins and base_url derives from the
    instance name."""
    text = """
collegenet:
  instance: demo-univ
  lookahead_days: 14
timezone: America/Chicago
systems:
  main:
    driver: preview
"""
    cfg = with_yaml(text, load_config)
    assert cfg["collegenet"]["lookahead_days"] == 14
    assert cfg["timezone"] == "America/Chicago"
    # A deep merge preserves untouched defaults rather than replacing the section.
    assert cfg["collegenet"]["merge_gap_minutes"] == 5
    assert cfg["collegenet"]["base_url"].endswith("/demo-univ/run")
    cfg2 = load_config("/nonexistent/path/config.yaml")
    assert cfg2["collegenet"]["lookahead_days"] == 7


def test_load_config_reads_defaults_file():
    """defaults.yaml overrides the built-in scheduling defaults."""
    def _run(cpath):
        return with_yaml(
            "pre_condition_minutes: 45\npost_buffer_minutes: 25\n"
            "merge_gap_minutes: 8\nlookahead_days: 14\n",
            lambda dpath: load_config(cpath, dpath))
    cn = with_yaml("collegenet:\n  instance: demo\n", _run)["collegenet"]
    assert (cn["default_pre_condition_minutes"], cn["default_post_buffer_minutes"],
            cn["merge_gap_minutes"], cn["lookahead_days"]) == (45, 25, 8, 14), cn


def test_legacy_niagara_block_becomes_a_system():
    """A pre-1.0 config with a bare `niagara:` block keeps working: it is
    promoted to a system and becomes the default."""
    cfg = {"niagara": {"host": "n4.example.edu", "port": 8443,
                       "schedule_base_path": "slot:/Schedules"}}
    migrate_legacy_systems(cfg)
    assert "niagara" not in cfg, "the legacy block should be consumed"
    assert cfg["systems"]["niagara"]["driver"] == "niagara"
    assert cfg["systems"]["niagara"]["host"] == "n4.example.edu"
    assert cfg["default_system"] == "niagara"


def test_explicit_systems_beat_the_legacy_block():
    """A site mid-migration keeps both; the explicit entry wins."""
    cfg = {"niagara": {"host": "old.example.edu", "port": 8443},
           "systems": {"niagara": {"driver": "niagara", "host": "new.example.edu"}}}
    migrate_legacy_systems(cfg)
    assert cfg["systems"]["niagara"]["host"] == "new.example.edu"
    assert cfg["systems"]["niagara"]["port"] == 8443   # gap filled by the legacy block


def test_single_system_is_the_implicit_default():
    """One system needs no `default_system`; several with none is ambiguous and
    must not be guessed at."""
    assert resolve_default_system({"systems": {"only": {}}}) == "only"
    assert resolve_default_system({"systems": {"a": {}, "b": {}}}) == ""
    assert resolve_default_system({"systems": {"a": {}, "b": {}},
                                   "default_system": "b"}) == "b"


# ─────────────────────────────────────────────────────────────────────────────
# BACnet encoding
# ─────────────────────────────────────────────────────────────────────────────

def test_bacnet_target_parsing():
    from bassync.drivers.bacnet import DEFAULT_BACNET_PORT, parse_target
    t = parse_target("12001:5")
    assert (t.device_id, t.schedule_instance, t.address) == (12001, 5, None)
    t = parse_target("12001:5@10.4.2.30")
    assert t.address == f"10.4.2.30:{DEFAULT_BACNET_PORT}"
    assert parse_target("12001:5@10.4.2.30:47810").address == "10.4.2.30:47810"


def test_bacnet_target_rejects_garbage():
    from bassync.drivers.base import DriverError
    from bassync.drivers.bacnet import parse_target
    for bad in ("", "slot:/Schedules/Rm1", "12001", "abc:5"):
        try:
            parse_target(bad)
        except DriverError:
            continue
        raise AssertionError(f"{bad!r} should not parse as a BACnet target")


def test_bacnet_groups_windows_by_date():
    """One special event per calendar date, with alternating ON/OFF times.

    This is what keeps the Exception_Schedule array inside the limits real
    controllers enforce — a per-booking encoding would need one array entry per
    class."""
    from bassync.drivers.bacnet import windows_to_daily
    windows = [OccupancyWindow(dt(9, day=10), dt(11, day=10)),
               OccupancyWindow(dt(13, day=10), dt(17, day=10)),
               OccupancyWindow(dt(8, day=11), dt(9, day=11))]
    by_date = windows_to_daily(windows, TZ)
    assert len(by_date) == 2, by_date
    day10 = by_date[dt(9, day=10).date()]
    assert [(d.strftime("%H:%M"), v) for d, v in day10] == [
        ("09:00", True), ("11:00", False), ("13:00", True), ("17:00", False)], day10


def test_bacnet_splits_windows_across_midnight():
    """A booking running past midnight becomes an entry on each date — BACnet
    exceptions are keyed by calendar date and a time cannot be 24:00."""
    from bassync.drivers.bacnet import windows_to_daily
    by_date = windows_to_daily(
        [OccupancyWindow(dt(22, day=10), dt(2, day=11))], TZ)
    assert len(by_date) == 2, by_date
    first = by_date[dt(0, day=10).date()]
    second = by_date[dt(0, day=11).date()]
    # Day one turns ON and does not turn OFF — midnight rollover re-evaluates
    # against day two's exception, so a trailing OFF would be both illegal and
    # redundant.
    assert [(d.strftime("%H:%M"), v) for d, v in first] == [("22:00", True)], first
    assert [(d.strftime("%H:%M"), v) for d, v in second] == [
        ("00:00", True), ("02:00", False)], second


def test_bacnet_encodes_a_real_exception_schedule():
    """The array actually encodes and decodes as BACnet. Skipped when
    BACpypes3 isn't installed, since it is an optional dependency."""
    try:
        from bacpypes3.basetypes import SpecialEvent
        from bacpypes3.constructeddata import ArrayOf
    except ImportError:
        print("      (skipped — bacpypes3 not installed)")
        return
    from bassync.drivers.bacnet import _bacnet_date, windows_to_daily
    from bacpypes3.basetypes import CalendarEntry, SpecialEventPeriod, TimeValue
    from bacpypes3.primitivedata import Boolean, Time

    by_date = windows_to_daily(
        [OccupancyWindow(dt(9, day=10), dt(17, day=10))], TZ)
    day = sorted(by_date)[0]
    events = [SpecialEvent(
        period=SpecialEventPeriod(calendarEntry=CalendarEntry(date=_bacnet_date(day))),
        listOfTimeValues=[TimeValue(time=Time((d.hour, d.minute, d.second, 0)),
                                    value=Boolean(v)) for d, v in by_date[day]],
        eventPriority=16)]
    array_type = ArrayOf(SpecialEvent)
    decoded = array_type.decode(array_type(events).encode())
    assert len(decoded) == 1
    assert decoded[0].eventPriority == 16
    assert len(decoded[0].listOfTimeValues) == 2
    assert str(decoded[0].period.calendarEntry.date).startswith("2026-6-10")


def test_bacnet_describe_is_offline():
    """--dry-run must never touch the network, including for BACnet."""
    from bassync.drivers.bacnet import BacnetScheduleWriter
    writer = BacnetScheduleWriter("campus", {"local_address": "10.0.0.1/24"}, TZ)
    text = writer.describe("12001:5", [OccupancyWindow(dt(9, day=10), dt(17, day=10))])
    assert "12001:5" in text and "09:00->ON" in text and "17:00->OFF" in text, text
    assert "CLEAR" in writer.describe("12001:5", [])


def test_bacnet_fails_fast_on_a_wrong_local_address():
    """A local_address that isn't this host's must fail immediately with a
    usable message. BACpypes3 retries a failed bind forever, so without this
    check a nightly run hangs instead of alerting — strictly worse, because a
    hung job never tells anyone."""
    import time
    from bassync.drivers.base import DriverError
    from bassync.drivers.bacnet import BacnetScheduleWriter
    # TEST-NET-1: reserved by RFC 5737, so it is never a real host address.
    writer = BacnetScheduleWriter("campus", {"local_address": "192.0.2.77/24"}, TZ)
    started = time.monotonic()
    try:
        ok, detail = writer.health_check()
    finally:
        writer.close()
    elapsed = time.monotonic() - started
    assert not ok, "a bogus local_address must not report healthy"
    assert elapsed < 5, f"took {elapsed:.1f}s — it should fail fast, not retry"
    # Without BACpypes3 the missing library is reported first: it is the harder
    # blocker, and naming the address instead would send the operator to fix
    # the wrong thing.
    import importlib.util
    expected = ("not an address on this host"
                if importlib.util.find_spec("bacpypes3")
                else "needs BACpypes3")
    assert expected in detail, detail
    try:
        writer.connect()
    except DriverError:
        return
    raise AssertionError("connect() should raise DriverError")


def test_bacnet_rejects_bad_event_priority():
    """eventPriority is 1-16; anything else is a config error caught at startup
    rather than a rejected write at 2 AM."""
    from bassync.drivers.base import DriverError
    from bassync.drivers.bacnet import BacnetScheduleWriter
    try:
        BacnetScheduleWriter("x", {"local_address": "10.0.0.1/24",
                                   "event_priority": 0}, TZ)
    except DriverError:
        return
    raise AssertionError("event_priority 0 should be rejected")


def test_split_at_midnight_handles_dst_forward():
    """The spring-forward day is 23 hours long. Splitting must still land on
    real local midnight, not 24 hours after the start."""
    from bassync.drivers.base import ScheduleWriter
    # 2026-03-08 is the US DST spring-forward date.
    start = datetime(2026, 3, 7, 22, 0, tzinfo=TZ)
    end = datetime(2026, 3, 8, 4, 0, tzinfo=TZ)
    pieces = ScheduleWriter.split_at_midnight([OccupancyWindow(start, end)])
    assert len(pieces) == 2, pieces
    boundary = pieces[0].end
    assert (boundary.hour, boundary.minute) == (0, 0), boundary
    assert boundary.date() == end.date(), boundary
    assert pieces[1].start == boundary and pieces[1].end == end


# ─────────────────────────────────────────────────────────────────────────────
# drivers: registry + generic REST
# ─────────────────────────────────────────────────────────────────────────────

def test_driver_registry_resolves_and_rejects():
    from bassync.drivers import DriverError, driver_names, load_driver_class
    assert {"bacnet", "niagara", "rest", "preview"} <= set(driver_names())
    assert load_driver_class("n4").name == "niagara"      # alias
    try:
        load_driver_class("honeywell_webs")
    except DriverError as exc:
        assert "Unknown BAS driver" in str(exc)
        return
    raise AssertionError("an unknown driver should raise")


def test_rest_driver_renders_templates_without_calling_out():
    """Placeholders resolve into the path and payload — checked without a
    server, since the whole point of the driver is a site-supplied contract."""
    from bassync.drivers.rest import RestScheduleWriter, _fill, _window_context
    writer = RestScheduleWriter("ebo", {
        "base_url": "https://ebo.example.edu",
        "write": {"method": "POST", "path": "/api/sched/{target}/exceptions",
                  "payload": {"start": "{start_local}", "value": "{value}"}},
    }, TZ)
    ctx = writer._context("/Server 1/Bldg A/Occ",
                          _window_context(OccupancyWindow(dt(9, day=10),
                                                          dt(17, day=10)), 0, 1))
    path = writer.write_cfg["path"].replace("{target}", ctx["target"])
    assert "%2FServer%201" in path, path        # target is URL-encoded in paths
    body = _fill(writer.write_cfg["payload"], ctx)
    assert body == {"start": "2026-06-10T09:00:00", "value": "true"}, body


def test_preview_driver_writes_csv():
    from bassync.drivers.preview import PreviewScheduleWriter
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "preview.csv")
        writer = PreviewScheduleWriter("p", {"csv_file": csv_path}, TZ)
        writer.write_schedule("A/Rm1", [OccupancyWindow(dt(9), dt(10))])
        writer.write_schedule("A/Rm2", [])
        writer.close()
        rows = open(csv_path, encoding="utf-8").read().splitlines()
    assert len(rows) == 3, rows            # header + two targets
    assert "A/Rm1" in rows[1] and "A/Rm2" in rows[2], rows


def test_niagara_retry_adapter_excludes_post():
    """The session retries GET/DELETE but never POST, so a retry can't create
    duplicate special events."""
    from bassync.drivers.niagara import NiagaraScheduleWriter
    writer = NiagaraScheduleWriter("n", {"host": "h", "port": 443},
                                   TZ, {"attempts": 2, "backoff_seconds": 0})
    methods = set(writer.session.get_adapter("https://x").max_retries.allowed_methods)
    assert "GET" in methods and "DELETE" in methods, methods
    assert "POST" not in methods, methods
    writer.close()


def test_niagara_ord_encoding_and_absolute_targets():
    """Spaces in schedule names are encoded; an absolute ORD bypasses the base
    path so a schedule outside it is still reachable."""
    from bassync.drivers.niagara import NiagaraScheduleWriter
    writer = NiagaraScheduleWriter(
        "n", {"host": "h", "port": 443, "schedule_base_path": "slot:/Schedules"}, TZ)
    assert writer._full_ord("Bldg A/Rm 101") == "slot:/Schedules/Bldg A/Rm 101"
    assert writer._full_ord("slot:/Other/Sched") == "slot:/Other/Sched"
    assert "%20" in writer._endpoint("Bldg A/Rm 101")
    assert "slot:/Schedules" in writer._endpoint("Bldg A/Rm 101")
    writer.close()


# ─────────────────────────────────────────────────────────────────────────────
# safety rail
# ─────────────────────────────────────────────────────────────────────────────

def test_safety_blocks_a_campus_wide_clear():
    """Zero events from 25Live is an auth/query failure far more often than an
    empty campus, and writing it would stand every building down."""
    from bassync import safety
    cfg = base_config()
    verdict = safety.check(cfg, {}, {dest("A/Rm1"), dest("A/Rm2")},
                           event_count=0, previous={})
    assert not verdict
    assert "min_events" in verdict.reason


def test_safety_blocks_a_large_partial_clear():
    """Most of the campus going dark at once is stopped even when some events
    did come back."""
    from bassync import safety
    previous = {"windows": {"sys:A/Rm1": 3, "sys:A/Rm2": 2, "sys:A/Rm3": 4}}
    schedule = {dest("A/Rm1"): [OccupancyWindow(dt(9), dt(10))],
                dest("A/Rm2"): [], dest("A/Rm3"): []}
    verdict = safety.check(base_config(), schedule, set(schedule),
                           event_count=1, previous=previous)
    assert not verdict and "max_cleared_fraction" in verdict.reason, verdict.reason


def test_safety_allows_a_normal_run():
    """One room going quiet is ordinary and must not block the night's sync."""
    from bassync import safety
    previous = {"windows": {"sys:A/Rm1": 3, "sys:A/Rm2": 2, "sys:A/Rm3": 4}}
    schedule = {dest("A/Rm1"): [OccupancyWindow(dt(9), dt(10))],
                dest("A/Rm2"): [OccupancyWindow(dt(9), dt(10))],
                dest("A/Rm3"): []}
    assert safety.check(base_config(), schedule, set(schedule),
                        event_count=12, previous=previous)


def test_safety_allows_the_first_ever_run():
    """No history means nothing to compare against — a guard that blocks the
    first run is just an outage."""
    from bassync import safety
    schedule = {dest("A/Rm1"): [OccupancyWindow(dt(9), dt(10))]}
    verdict = safety.check(base_config(), schedule, set(schedule),
                           event_count=5, previous={})
    assert verdict and "no previous run" in verdict.reason


def test_safety_scopes_to_one_system_when_limited():
    """A --system run must compare against that system only. Otherwise every
    OTHER system's schedules look 'cleared' and the rail blocks a perfectly
    normal single-building commissioning run."""
    from bassync import safety
    previous = {"windows": {"supervisor:A/Rm1": 3, "campus_bacnet:12001:5": 2,
                            "campus_bacnet:12001:6": 4}}
    schedule = {Destination("supervisor", "A/Rm1"): [OccupancyWindow(dt(9), dt(10))]}
    assert safety.check(base_config(), schedule, set(schedule), event_count=5,
                        previous=previous, only_system="supervisor")
    # Unscoped, the same run reads as a campus-wide clear.
    assert not safety.check(base_config(), schedule, set(schedule),
                            event_count=5, previous=previous)


def test_safety_state_merges_on_a_scoped_run():
    """A --system run must not wipe the other systems' baseline; the next full
    run would then trip its own rail."""
    from bassync import safety
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "last_run.json")
        safety.save_state(path, {Destination("a", "R1"): [OccupancyWindow(dt(9), dt(10))],
                                 Destination("b", "R2"): [OccupancyWindow(dt(9), dt(10))]},
                          event_count=4)
        safety.save_state(path, {Destination("a", "R1"): []}, event_count=1,
                          only_system="a")
        state = safety.load_state(path)
    assert state["windows"] == {"a:R1": 0, "b:R2": 1}, state


def test_safety_can_be_disabled():
    from bassync import safety
    cfg = base_config()
    cfg["safety"]["enabled"] = False
    assert safety.check(cfg, {}, {dest("A/Rm1")}, event_count=0, previous={})


def test_safety_state_roundtrip():
    from bassync import safety
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "sub", "last_run.json")
        safety.save_state(path, {dest("A/Rm1"): [OccupancyWindow(dt(9), dt(10))],
                                 dest("A/Rm2"): []}, event_count=7)
        state = safety.load_state(path)
    assert state["event_count"] == 7
    assert state["windows"] == {"sys:A/Rm1": 1, "sys:A/Rm2": 0}, state
    assert safety.load_state("/nonexistent/state.json") == {}


# ─────────────────────────────────────────────────────────────────────────────
# 25Live client
# ─────────────────────────────────────────────────────────────────────────────

def test_naive_25live_datetime_uses_configured_tz():
    """A naive 25Live timestamp is read in the configured campus timezone, not
    the server's. Regression for .astimezone() on a naive datetime, which
    shifted every booking when the job ran on a UTC host."""
    from bassync.collegenet import CollegeNetClient
    client = CollegeNetClient({"base_url": "http://x"}, TZ)
    d = client._to_tz("2026-06-10T09:00:00")
    assert d.tzinfo is not None and (d.hour, d.minute) == (9, 0), d
    assert d.utcoffset() == timedelta(hours=-4), d.utcoffset()
    client.close()


def test_state_param_styles():
    """Series25 instances differ in how they want the state filter, and getting
    it wrong returns 200 with zero events."""
    from bassync.collegenet import CollegeNetClient

    def client(style):
        return CollegeNetClient({"base_url": "http://x", "include_states": [2, 4],
                                 "state_param_style": style}, TZ)
    assert client("plus")._state_params() == {"state": "2+4"}
    assert client("comma")._state_params() == {"state": "2,4"}
    assert client("repeat")._state_params() == {"state": ["2", "4"]}
    assert client("none")._state_params() == {}


def test_discover_collects_spaces_from_xml():
    import xml.etree.ElementTree as ET
    from bassync.collegenet import CollegeNetClient
    client = CollegeNetClient({"base_url": "http://x"}, TZ)
    xml = """<r25:results xmlns:r25="http://www.collegenet.com/r25">
      <r25:event><r25:reservations><r25:reservation><r25:space_reservation>
        <r25:space_id>101</r25:space_id><r25:space_name>Room A</r25:space_name>
      </r25:space_reservation></r25:reservation></r25:reservations></r25:event>
      <r25:event><r25:reservations><r25:reservation><r25:space_reservation>
        <r25:space_id>102</r25:space_id><r25:formal_name>Room B Formal</r25:formal_name>
      </r25:space_reservation></r25:reservation></r25:reservations></r25:event>
    </r25:results>"""
    seen: dict = {}
    client._collect_spaces_from(ET.fromstring(xml), seen)
    assert seen == {"101": "Room A", "102": "Room B Formal"}, seen
    client.close()


def test_parse_event_applies_buffers_and_dedupes_spaces():
    """Buffers widen the window, and a space id repeated in the XML yields ONE
    RawEvent rather than a duplicate per nesting level."""
    import xml.etree.ElementTree as ET
    from bassync.collegenet import CollegeNetClient
    client = CollegeNetClient({"base_url": "http://x", "include_states": [2]}, TZ)
    xml = """<r25:event xmlns:r25="http://www.collegenet.com/r25">
      <r25:event_id>E9</r25:event_id><r25:event_name>Chem 101</r25:event_name>
      <r25:state>2</r25:state>
      <r25:reservations><r25:reservation>
        <r25:event_start_dt>2026-06-10T09:00:00</r25:event_start_dt>
        <r25:event_end_dt>2026-06-10T10:00:00</r25:event_end_dt>
        <r25:space_reservation><r25:space_id>1</r25:space_id>
          <r25:space><r25:space_id>1</r25:space_id></r25:space>
        </r25:space_reservation>
      </r25:reservation></r25:reservations>
    </r25:event>"""
    sm = {"1": space(1, "room", "B/Rm1")}
    sm["1"].pre_condition_minutes = 30
    sm["1"].post_buffer_minutes = 15
    events = client._parse_event(ET.fromstring(xml), sm)
    assert len(events) == 1, events
    assert events[0].start == dt(8, 30, day=10), events[0].start
    assert events[0].end == dt(10, 15, day=10), events[0].end
    client.close()


def test_parse_event_honors_25live_setup_teardown():
    """When 25Live's own setup/takedown is wider than our buffers, it wins —
    the room really is in use for the setup crew."""
    import xml.etree.ElementTree as ET
    from bassync.collegenet import CollegeNetClient
    client = CollegeNetClient({"base_url": "http://x"}, TZ)
    xml = """<r25:event xmlns:r25="http://www.collegenet.com/r25">
      <r25:event_id>E1</r25:event_id>
      <r25:reservations><r25:reservation>
        <r25:event_start_dt>2026-06-10T09:00:00</r25:event_start_dt>
        <r25:event_end_dt>2026-06-10T10:00:00</r25:event_end_dt>
        <r25:setup_dt>2026-06-10T07:00:00</r25:setup_dt>
        <r25:takedown_dt>2026-06-10T12:00:00</r25:takedown_dt>
        <r25:space_id>1</r25:space_id>
      </r25:reservation></r25:reservations>
    </r25:event>"""
    sm = {"1": space(1, "room", "B/Rm1")}
    sm["1"].pre_condition_minutes = 30
    sm["1"].post_buffer_minutes = 15
    ev = client._parse_event(ET.fromstring(xml), sm)[0]
    assert ev.start == dt(7, day=10) and ev.end == dt(12, day=10), (ev.start, ev.end)
    client.close()


def test_parse_event_filters_by_state_client_side():
    """The state filter is applied to the response too, so `state_param_style:
    none` still only syncs confirmed events."""
    import xml.etree.ElementTree as ET
    from bassync.collegenet import CollegeNetClient
    client = CollegeNetClient({"base_url": "http://x", "include_states": [2]}, TZ)
    xml = """<r25:event xmlns:r25="http://www.collegenet.com/r25">
      <r25:event_id>E1</r25:event_id><r25:state>1</r25:state>
      <r25:reservations><r25:reservation>
        <r25:event_start_dt>2026-06-10T09:00:00</r25:event_start_dt>
        <r25:event_end_dt>2026-06-10T10:00:00</r25:event_end_dt>
        <r25:space_id>1</r25:space_id>
      </r25:reservation></r25:reservations></r25:event>"""
    assert client._parse_event(ET.fromstring(xml),
                              {"1": space(1, "room", "B/Rm1")}) == []
    client.close()


def test_html_error_page_is_reported_clearly():
    """A login page where XML was expected must say so, not raise a bare
    ParseError from deep in the stdlib."""
    from bassync.collegenet import CollegeNetClient, CollegeNetError
    client = CollegeNetClient({"base_url": "http://x"}, TZ)
    # Real HTML, with the unclosed tags that make it invalid XML.
    page = ('<!DOCTYPE html><html><head><meta charset="utf-8">'
            "<title>Sign in</title></head><body>Please sign in<br></body></html>")
    try:
        client._parse_xml(page, "fetching events")
    except CollegeNetError as exc:
        assert "not valid XML" in str(exc) and "Sign in" in str(exc), exc
        return
    finally:
        client.close()
    raise AssertionError("an HTML error page should raise CollegeNetError")


def test_wellformed_but_wrong_xml_is_caught_by_validate():
    """An SSO login page that happens to be valid XHTML parses fine and then
    yields zero events — which a live run would read as an empty campus. The
    connection check names the real cause instead."""
    from bassync.collegenet import CollegeNetClient
    client = CollegeNetClient({"base_url": "http://x"}, TZ)

    class FakeResponse:
        status_code = 200
        text = "<html><body>Please sign in with your NetID</body></html>"

    client.session.get = lambda *a, **kw: FakeResponse()
    ok, detail = client.check_connection()
    assert not ok, detail
    assert "SSO" in detail and "<html>" in detail, detail
    client.close()


# ─────────────────────────────────────────────────────────────────────────────
# editor round-trip
# ─────────────────────────────────────────────────────────────────────────────

def test_editor_roundtrip_feeds_loader():
    """A room added in the editor resolves to its building's schedule, end to
    end through the editor's dump and the sync's loader."""
    import editor
    buildings = [{"id": "bldg_a", "name": "Building A", "target": "A/Building_Occ"}]
    rooms = [{"space_id": 11, "space_name": "A 101", "building": "bldg_a",
              "target": "A/Rm101_Occ", "pre_condition_minutes": 30},
             {"space_id": 12, "target": "A/Rm102_Occ"}]

    def _check(path):
        b2, f2, r2 = editor.load_mapping(path)
        assert len(b2) == 1 and not f2 and len(r2) == 2, (b2, f2, r2)
        return load_space_map(path, base_config())

    sm = with_yaml(editor.dump_mapping(buildings, [], rooms), _check)
    assert not sm.errors, sm.errors
    assert sm.spaces["11"].building_destination == dest("A/Building_Occ")
    assert sm.spaces["12"].building_destination is None


def test_editor_keeps_system_through_a_roundtrip():
    import editor
    rooms = [{"space_id": 1, "system": "campus_bacnet", "target": "12001:5"}]
    _b, _f, r2 = with_yaml(editor.dump_mapping([], [], rooms),
                           editor.load_mapping)
    assert r2[0]["system"] == "campus_bacnet", r2


def test_editor_flags_unknown_building():
    import editor
    assert editor.unknown_building_refs(
        [{"id": "bldg_a", "target": "A/Occ"}],
        [{"space_id": 11, "building": "typo_bldg", "target": "A/Rm.Occ"}]) == ["11"]


def test_editor_reads_systems_from_config():
    """The System dropdowns are populated from config.yaml, and a pre-1.0
    config still offers its single Niagara station."""
    import editor
    raw = {"systems": {"campus_bacnet": {"driver": "bacnet"},
                       "supervisor": {"driver": "niagara"}}}
    assert editor.config_systems(raw) == {"campus_bacnet": "bacnet",
                                          "supervisor": "niagara"}
    # A pre-1.0 config has a bare `niagara:` block instead of `systems:`.
    assert editor.config_systems({"niagara": {"host": "n4"}}) == {"niagara": "niagara"}
    # A missing or broken config must not stop the editor from opening.
    assert editor.config_systems({}) == {}
    assert editor.read_config_raw("/nonexistent/config.yaml") == {}


def test_editor_defaults_roundtrip_and_fallback():
    import editor
    text = editor.dump_defaults({"pre_condition_minutes": 40,
                                 "post_buffer_minutes": 10,
                                 "merge_gap_minutes": 3, "lookahead_days": 21})
    d = with_yaml(text, editor.load_defaults)
    assert d == {"pre_condition_minutes": 40, "post_buffer_minutes": 10,
                 "merge_gap_minutes": 3, "lookahead_days": 21}, d
    d2 = editor.load_defaults("/nonexistent/defaults.yaml")
    assert d2["pre_condition_minutes"] == 30 and d2["lookahead_days"] == 7, d2


# ─────────────────────────────────────────────────────────────────────────────
# alerting
# ─────────────────────────────────────────────────────────────────────────────

def test_send_alert_webhook_and_disabled():
    from bassync import notify
    captured = {}

    def fake_post(url, json=None, timeout=None, **kw):
        captured.update(url=url, json=json)
        return type("R", (), {"status_code": 200, "text": ""})()

    original = notify.requests.post
    notify.requests.post = fake_post
    try:
        results = notify.send_alert({"enabled": True, "webhook_url": "http://hook"},
                                    "Subject X", "Body Y")
        assert captured.get("url") == "http://hook"
        assert "Subject X" in captured["json"]["text"]
        assert len(results) == 1 and results[0].ok, [str(r) for r in results]
        captured.clear()
        assert notify.send_alert({"enabled": False, "webhook_url": "http://hook"},
                                 "s", "b") == []
        assert captured == {}, "disabled alerts must not post"
    finally:
        notify.requests.post = original


def test_smtp_security_resolution():
    """`security:` wins; the pre-1.0 `use_tls:` boolean still works; and with
    neither, port 465 means implicit TLS and everything else STARTTLS. The
    default must never be plaintext — that would put a relay password on the
    wire without anyone asking for it."""
    from bassync.notify import _resolve_security
    assert _resolve_security({"security": "ssl"}, 587) == "ssl"
    assert _resolve_security({"security": "SMTPS"}, 587) == "ssl"
    assert _resolve_security({"security": "none"}, 465) == "none"
    assert _resolve_security({"use_tls": False}, 587) == "none"
    assert _resolve_security({"use_tls": True}, 587) == "starttls"
    assert _resolve_security({}, 465) == "ssl"
    assert _resolve_security({}, 587) == "starttls"
    assert _resolve_security({}, 25) == "starttls"


def test_smtp_config_validation():
    """Missing settings are named before we open a socket, so the operator
    sees the real problem instead of a relay's opaque 5xx."""
    from bassync.notify import _validate_email_cfg
    good = {"smtp_host": "smtp.example.edu", "from_addr": "a@example.edu",
            "to_addrs": ["b@example.edu"]}
    assert _validate_email_cfg(good) is None
    assert "smtp_host" in _validate_email_cfg({**good, "smtp_host": ""})
    assert "from_addr" in _validate_email_cfg({**good, "from_addr": " "})
    assert "to_addrs" in _validate_email_cfg({**good, "to_addrs": []})
    assert "to_addrs" in _validate_email_cfg({**good, "to_addrs": ["", "  "]})
    # A username with no password reaches the relay as an unauthenticated send
    # and gets rejected confusingly. Catch it here instead.
    os.environ.pop("BAS_SMTP_PASSWORD", None)
    problem = _validate_email_cfg({**good, "username": "svc"})
    assert problem and "BAS_SMTP_PASSWORD" in problem, problem
    os.environ["BAS_SMTP_PASSWORD"] = "x"
    try:
        assert _validate_email_cfg({**good, "username": "svc"}) is None
    finally:
        os.environ.pop("BAS_SMTP_PASSWORD", None)


def test_smtp_message_headers():
    """Date and Message-ID are set explicitly — mail without them gets
    quarantined by some filters, and an alert nobody sees is no alert."""
    from bassync.notify import build_message
    msg = build_message({"from_addr": "alerts@example.edu",
                         "to_addrs": ["a@example.edu", " b@example.edu "]},
                        "Subject X", "Body Y")
    assert msg["Subject"] == "Subject X"
    assert msg["To"] == "a@example.edu, b@example.edu", msg["To"]
    assert msg["Date"] and msg["Message-ID"], dict(msg)
    assert "example.edu" in msg["Message-ID"]
    assert msg.get_content().strip() == "Body Y"
    # A single string recipient is accepted as well as a list.
    single = build_message({"from_addr": "a@x.edu", "to_addrs": "b@x.edu"}, "s", "b")
    assert single["To"] == "b@x.edu"


def test_smtp_reports_refused_recipients():
    """A relay that accepts the message but rejects a recipient is a failure,
    not a success — that person never hears about the outage."""
    from bassync import notify

    class StubSMTP:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def send_message(self, msg):
            return {"bad@example.edu": (550, b"No such user")}

    original = notify.smtplib.SMTP
    notify.smtplib.SMTP = StubSMTP
    try:
        result = notify._send_email(
            {"smtp_host": "smtp.example.edu", "from_addr": "a@example.edu",
             "to_addrs": ["good@example.edu", "bad@example.edu"]}, "s", "b")
    finally:
        notify.smtplib.SMTP = original
    assert not result.ok, result
    assert "bad@example.edu" in result.detail, result.detail


def test_smtp_connection_failure_is_reported_not_raised():
    """An unreachable relay must be reported, never raised — alerting failing
    must not turn a successful sync into a crash."""
    from bassync import notify
    result = notify._send_email(
        # TEST-NET-1 is reserved and unroutable, so this cannot connect.
        {"smtp_host": "192.0.2.1", "smtp_port": 2525, "security": "none",
         "from_addr": "a@example.edu", "to_addrs": ["b@example.edu"]},
        "s", "b")
    assert not result.ok
    assert "could not connect" in result.detail, result.detail


def test_send_alert_force_sends_while_disabled():
    """--test-alert must exercise the channels even before alerts are switched
    on — that is the point of testing them."""
    from bassync import notify
    posted = []
    original = notify.requests.post
    notify.requests.post = lambda url, **kw: (
        posted.append(url), type("R", (), {"status_code": 200, "text": ""})())[1]
    try:
        cfg = {"enabled": False, "webhook_url": "http://hook"}
        assert notify.send_alert(cfg, "s", "b") == []
        results = notify.send_alert(cfg, "s", "b", force=True)
    finally:
        notify.requests.post = original
    assert len(results) == 1 and results[0].ok, [str(r) for r in results]
    assert posted == ["http://hook"], posted


def test_send_alert_survives_a_dead_webhook():
    """An alerting failure must never change the run's outcome."""
    from bassync import notify

    def boom(*a, **kw):
        raise notify.requests.RequestException("no route to host")

    original = notify.requests.post
    notify.requests.post = boom
    try:
        notify.send_alert({"enabled": True, "webhook_url": "http://hook"}, "s", "b")
    finally:
        notify.requests.post = original


# ─────────────────────────────────────────────────────────────────────────────
# end to end
# ─────────────────────────────────────────────────────────────────────────────

def test_mixed_campus_dry_run_routes_to_each_system():
    """The whole pipeline, three vendors at once: rooms land on their own
    system and the roll-up follows its building's."""
    from bassync.sync import _group_by_system
    text = """
buildings:
  - id: soc
    target: "12001:100"
  - id: eng
    system: supervisor
    target: "EngTech/Building_Occ"
spaces:
  - space_id: 1
    building: soc
    target: "12001:5"
  - space_id: 2
    building: soc
    target: "12001:6"
  - space_id: 3
    building: eng
    target: "EngTech/Rm110_Occ"
"""
    cfg = base_config()
    cfg["systems"] = {"campus_bacnet": {"driver": "bacnet",
                                        "local_address": "10.0.0.1/24"},
                      "supervisor": {"driver": "niagara", "host": "h"}}
    cfg["default_system"] = "campus_bacnet"
    sm = with_yaml(text, lambda p: load_space_map(p, cfg))
    assert not sm.errors, sm.errors

    schedule = ScheduleBuilder(5).build(
        [event(1, dt(9, day=10), dt(10, day=10), "A"),
         event(2, dt(10, day=10), dt(11, day=10), "B"),
         event(3, dt(13, day=10), dt(14, day=10), "C")], sm)
    for d in sm.destinations():
        schedule.setdefault(d, [])

    grouped = _group_by_system(schedule)
    assert set(grouped) == {"campus_bacnet", "supervisor"}, grouped
    assert set(grouped["campus_bacnet"]) == {"12001:5", "12001:6", "12001:100"}
    assert set(grouped["supervisor"]) == {"EngTech/Rm110_Occ",
                                          "EngTech/Building_Occ"}
    # The BACnet roll-up unions both rooms into one 09:00-11:00 window.
    rollup = grouped["campus_bacnet"]["12001:100"]
    assert len(rollup) == 1 and rollup[0].end == dt(11, day=10), rollup


def test_empty_schedules_are_still_written_so_stale_occupancy_clears():
    """A room and a building with no bookings this week both appear in the
    write set, with empty window lists. Regression: the old clear-loop skipped
    building roll-ups entirely, so a building whose rooms all went quiet kept
    running on last week's schedule."""
    text = """
buildings:
  - id: b
    target: "B/Occ"
spaces:
  - space_id: 1
    building: b
    target: "B/Rm1"
  - space_id: 2
    building: b
    target: "B/Rm2"
"""
    cfg = base_config()
    sm = with_yaml(text, lambda p: load_space_map(p, cfg))
    schedule = ScheduleBuilder(5).build([event(1, dt(9), dt(10))], sm)
    for d in sm.destinations():
        schedule.setdefault(d, [])
    assert schedule[dest("B/Rm2")] == [], "an unbooked room must be cleared"
    assert len(schedule[dest("B/Occ")]) == 1
    assert set(schedule) == sm.destinations()


def test_run_sync_end_to_end_writes_and_records_state():
    """The real run_sync path — fetch, build, safety check, fan out, save state
    — with 25Live stubbed and every system on the `preview` driver.

    Exercises what the unit tests can't: that a system failure is contained,
    that unbooked schedules are actually written empty, and that the run leaves
    a state file the next run can compare against."""
    import bassync.sync as sync_mod
    from bassync.model import RawEvent as RE

    map_text = """
buildings:
  - id: b
    target: "B/Occ"
spaces:
  - space_id: 1
    building: b
    target: "B/Rm1"
  - space_id: 2
    building: b
    target: "B/Rm2"
  - space_id: 3
    system: broken
    target: "X/Rm3"
"""
    with tempfile.TemporaryDirectory() as tmp:
        map_path = os.path.join(tmp, "map.yaml")
        with open(map_path, "w", encoding="utf-8") as fh:
            fh.write(map_text)
        csv_path = os.path.join(tmp, "out.csv")
        state_path = os.path.join(tmp, "last_run.json")

        cfg = load_config("/nonexistent/config.yaml")
        cfg["collegenet"]["base_url"] = "http://stub"
        cfg["space_map_file"] = map_path
        cfg["safety"]["state_file"] = state_path
        cfg["systems"] = {"sys": {"driver": "preview", "csv_file": csv_path},
                          # A system whose driver cannot be built at all.
                          "broken": {"driver": "rest", "base_url": ""}}
        cfg["default_system"] = "sys"

        original = sync_mod._fetch
        sync_mod._fetch = lambda *a, **kw: [
            RE("E1", "Class", "1", dt(9, day=10), dt(10, day=10)),
            RE("E2", "Lab", "1", dt(13, day=10), dt(14, day=10)),
        ]
        try:
            code = sync_mod.run_sync(cfg)
        finally:
            sync_mod._fetch = original

        # The broken system fails its own schedule; the rest still wrote.
        assert code == sync_mod.EXIT_WRITE_FAILURES, code
        rows = open(csv_path, encoding="utf-8").read()
        state = __import__("json").load(open(state_path, encoding="utf-8"))

    # Room 1 booked twice, room 2 unbooked but still written (cleared), and the
    # building rolled up from room 1.
    assert "B/Rm1" in rows and "B/Rm2" in rows and "B/Occ" in rows, rows
    assert state["windows"]["sys:B/Rm1"] == 2, state
    assert state["windows"]["sys:B/Rm2"] == 0, state
    assert state["windows"]["sys:B/Occ"] == 2, state


def test_run_sync_aborts_instead_of_clearing_the_campus():
    """25Live returning nothing must not become a campus-wide stand-down."""
    import bassync.sync as sync_mod
    map_text = """
buildings: []
spaces:
  - space_id: 1
    target: "B/Rm1"
  - space_id: 2
    target: "B/Rm2"
"""
    with tempfile.TemporaryDirectory() as tmp:
        map_path = os.path.join(tmp, "map.yaml")
        with open(map_path, "w", encoding="utf-8") as fh:
            fh.write(map_text)
        csv_path = os.path.join(tmp, "out.csv")
        cfg = load_config("/nonexistent/config.yaml")
        cfg["collegenet"]["base_url"] = "http://stub"
        cfg["space_map_file"] = map_path
        cfg["safety"]["state_file"] = os.path.join(tmp, "last_run.json")
        cfg["systems"] = {"sys": {"driver": "preview", "csv_file": csv_path}}
        cfg["default_system"] = "sys"

        original = sync_mod._fetch
        sync_mod._fetch = lambda *a, **kw: []
        try:
            code = sync_mod.run_sync(cfg)
            wrote_anything = os.path.exists(csv_path)
            # --force is the documented escape hatch and must actually work.
            forced = sync_mod.run_sync(cfg, force=True)
        finally:
            sync_mod._fetch = original
        forced_wrote = os.path.exists(csv_path)

    assert code == sync_mod.EXIT_SAFETY_ABORT, code
    assert not wrote_anything, "the aborted run must not have written"
    assert forced == sync_mod.EXIT_OK, forced
    assert forced_wrote, "--force must let the run through"


# ─────────────────────────────────────────────────────────────────────────────
# per-floor corridor roll-up (room -> floor -> building)
# ─────────────────────────────────────────────────────────────────────────────

def test_floor_rollup_room_to_floor_to_building():
    """A booked room drives its own zone, its floor's corridor, AND its
    building — while the OTHER floor's corridor stays off. That separation is
    the whole point: one evening seminar shouldn't condition the whole tower."""
    text = """
buildings:
  - id: b
    target: "B/Occ"
floors:
  - building: b
    level: 1
    target: "B/F1_Corridor"
  - building: b
    level: 2
    target: "B/F2_Corridor"
spaces:
  - space_id: 1
    building: b
    floor: 1
    target: "B/Rm101"
  - space_id: 2
    building: b
    floor: 2
    target: "B/Rm201"
"""
    cfg = base_config()
    sm = with_yaml(text, lambda p: load_space_map(p, cfg))
    assert not sm.errors, sm.errors
    assert sm.floor_count == 2, sm.floor_count
    assert sm.spaces["1"].floor_destination == dest("B/F1_Corridor")

    # Only the floor-1 room is booked.
    schedule = ScheduleBuilder(5).build([event(1, dt(18), dt(20), "E")], sm)
    for d in sm.destinations():
        schedule.setdefault(d, [])
    assert len(schedule[dest("B/Rm101")]) == 1
    assert len(schedule[dest("B/F1_Corridor")]) == 1, "its own corridor runs"
    assert len(schedule[dest("B/Occ")]) == 1, "the building runs"
    assert schedule[dest("B/F2_Corridor")] == [], "the other floor stays off"
    assert schedule[dest("B/Rm201")] == []


def test_floor_destinations_are_cleared_when_empty():
    """Floor corridors join the managed set, so one with no bookings this week
    is actively cleared rather than left on last week's schedule."""
    text = """
buildings:
  - id: b
    target: "B/Occ"
floors:
  - building: b
    level: 1
    target: "B/F1_Corridor"
spaces:
  - space_id: 1
    building: b
    floor: 1
    target: "B/Rm101"
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert sm.destinations() == {dest("B/Rm101"), dest("B/F1_Corridor"),
                                 dest("B/Occ")}, sm.destinations()


def test_unknown_floor_warns_and_skips():
    """A room naming a floor with no matching entry still syncs and still rolls
    up to its building — it just drives no corridor. A typo shouldn't cost the
    room its heat."""
    text = """
buildings:
  - id: b
    target: "B/Occ"
floors:
  - building: b
    level: 1
    target: "B/F1_Corridor"
spaces:
  - space_id: 1
    building: b
    floor: 9
    target: "B/Rm901"
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert not sm.errors, sm.errors
    assert any("floor 9" in w for w in sm.warnings), sm.warnings
    assert sm.spaces["1"].floor_destination is None
    assert sm.spaces["1"].building_destination == dest("B/Occ")


def test_floor_inherits_building_system_and_can_override():
    """A corridor is usually on the same panel as the rooms off it, so it
    inherits — but a part-finished retrofit can put it elsewhere."""
    text = """
buildings:
  - id: b
    system: supervisor
    target: "B/Occ"
floors:
  - building: b
    level: 1
    target: "B/F1_Corridor"
  - building: b
    level: 2
    system: campus_bacnet
    target: "12001:120"
spaces:
  - space_id: 1
    building: b
    floor: 1
    target: "B/Rm101"
  - space_id: 2
    building: b
    floor: 2
    target: "B/Rm201"
"""
    cfg = base_config()
    cfg["systems"] = {"supervisor": {"driver": "preview"},
                      "campus_bacnet": {"driver": "preview"},
                      "sys": {"driver": "preview"}}
    sm = with_yaml(text, lambda p: load_space_map(p, cfg))
    assert not sm.errors, sm.errors
    assert sm.spaces["1"].floor_destination.system == "supervisor"
    assert sm.spaces["2"].floor_destination.system == "campus_bacnet"


def test_floor_errors_are_reported():
    """Structural mistakes in floors: are errors, not silent skips."""
    text = """
buildings:
  - id: b
    target: "B/Occ"
floors:
  - building: nosuch
    level: 1
    target: "X/Hall"
  - building: b
    level: notanumber
    target: "B/Hall"
  - building: b
    level: 1
    target: "B/F1"
  - building: b
    level: 1
    target: "B/F1_again"
spaces: []
"""
    sm = with_yaml(text, lambda p: load_space_map(p, base_config()))
    assert any("unknown building 'nosuch'" in e for e in sm.errors), sm.errors
    assert any("non-numeric level" in e for e in sm.errors), sm.errors
    assert any("defined twice" in e for e in sm.errors), sm.errors


def test_room_gap_carries_into_rollup_contribution():
    """A room's own wide gap merges its windows, and that merged occupancy
    carries into the roll-ups — the corridor reflects when the room is really
    occupied, not when its individual bookings happen to start."""
    space_map = {
        "1": space(1, "room", "B/Rm1", building="B/Occ", merge_gap=30),
    }
    result = ScheduleBuilder(5).build(
        [event(1, dt(9), dt(10), "A1"), event(1, dt(10, 20), dt(11), "A2")],
        space_map)
    assert len(result[dest("B/Rm1")]) == 1, result[dest("B/Rm1")]
    assert len(result[dest("B/Occ")]) == 1, result[dest("B/Occ")]


# ─────────────────────────────────────────────────────────────────────────────
# malformed input handling
# ─────────────────────────────────────────────────────────────────────────────

def test_load_config_rejects_malformed_yaml():
    """A stray tab in config.yaml fails with one clear, file-named line rather
    than a stack trace — and never by silently falling back to defaults, which
    would write the wrong schedules to the wrong station."""
    from bassync.config import ConfigError
    try:
        with_yaml("collegenet:\n\tinstance: oops\n", load_config)
    except ConfigError as exc:
        assert ".yaml" in str(exc), exc
    else:
        raise AssertionError("malformed YAML should raise ConfigError")
    # A top level that isn't a mapping is equally unusable.
    try:
        with_yaml("- just\n- a list\n", load_config)
    except ConfigError as exc:
        assert "mapping" in str(exc), exc
    else:
        raise AssertionError("a non-mapping config should raise ConfigError")


def test_read_yaml_tolerates_missing_and_empty():
    """Missing and empty are normal, not errors — a site that hasn't written
    defaults.yaml yet must still run."""
    from bassync.config import read_yaml
    assert read_yaml("/nonexistent/nothing.yaml") == {}
    assert with_yaml("", read_yaml) == {}
    assert with_yaml("# just a comment\n", read_yaml) == {}


def test_editor_config_form_roundtrip_preserves_other_sections():
    """The Connection tab must not eat config it doesn't display — retry,
    alerts and safety all have to survive a save."""
    import editor
    raw = {"systems": {"campus": {"driver": "bacnet",
                                  "local_address": "10.1.1.5/24"},
                       "sup": {"driver": "niagara", "host": "n4", "port": 8443}},
           "default_system": "campus",
           "retry": {"attempts": 5},
           "alerts": {"enabled": True, "webhook_url": "http://hook"},
           "safety": {"max_cleared_fraction": 0.5}}
    assert editor.config_systems(raw) == {"campus": "bacnet", "sup": "niagara"}

    form = editor.form_from_raw(raw, "campus", "bacnet")
    assert form["systems.campus.local_address"] == "10.1.1.5/24"
    form["systems.campus.local_address"] = "10.9.9.9/24"
    out = editor.apply_config_form(raw, form, "campus", "bacnet")

    assert out["systems"]["campus"]["local_address"] == "10.9.9.9/24"
    # The system that wasn't on screen is untouched...
    assert out["systems"]["sup"] == {"driver": "niagara", "host": "n4",
                                     "port": 8443}, out["systems"]["sup"]
    # ...and so is everything the form never shows.
    assert out["retry"] == {"attempts": 5}
    assert out["alerts"]["webhook_url"] == "http://hook"
    assert out["safety"] == {"max_cleared_fraction": 0.5}


def test_editor_config_form_shows_driver_specific_fields():
    """A BACnet system must not be offered Niagara's ORD boxes."""
    import editor
    bacnet = {k for k, *_ in editor.system_config_fields("s", "bacnet")}
    niagara = {k for k, *_ in editor.system_config_fields("s", "niagara")}
    assert ("systems", "s", "local_address") in bacnet
    assert ("systems", "s", "schedule_base_path") not in bacnet
    assert ("systems", "s", "schedule_base_path") in niagara
    assert ("systems", "s", "local_address") not in niagara
    # An unrecognised driver contributes no fields rather than exploding.
    assert editor.system_config_fields("s", "nonesuch") == []


def test_editor_config_form_invalid_int_raises():
    """A non-numeric port is reported against its label, not swallowed."""
    import editor
    form = editor.form_from_raw({}, "sup", "niagara")
    form["systems.sup.port"] = "eight-thousand"
    try:
        editor.apply_config_form({}, form, "sup", "niagara")
    except ValueError as exc:
        assert "Port" in str(exc), exc
        return
    raise AssertionError("a non-numeric int should raise ValueError")


def test_editor_floors_roundtrip():
    """Floors survive the editor's dump -> load -> sync-loader path."""
    import editor
    buildings = [{"id": "b", "target": "B/Occ"}]
    floors = [{"building": "b", "level": 3, "target": "B/F3_Corridor"}]
    rooms = [{"space_id": 1, "building": "b", "floor": 3, "target": "B/Rm301"}]

    def _check(path):
        b2, f2, r2 = editor.load_mapping(path)
        assert len(b2) == 1 and len(f2) == 1 and len(r2) == 1, (b2, f2, r2)
        return load_space_map(path, base_config())

    sm = with_yaml(editor.dump_mapping(buildings, floors, rooms), _check)
    assert not sm.errors, sm.errors
    assert sm.spaces["1"].floor_destination == dest("B/F3_Corridor")


def test_editor_migrates_legacy_key_in_all_three_sections():
    """A pre-1.0 map — buildings, floors and rooms — opens with every
    niagara_path renamed to target."""
    import editor
    text = ("buildings:\n  - id: b\n    niagara_path: 'B/Occ'\n"
            "floors:\n  - building: b\n    level: 1\n"
            "    niagara_path: 'B/F1'\n"
            "spaces:\n  - space_id: 1\n    niagara_path: 'B/Rm1'\n")
    buildings, floors, rooms = with_yaml(text, editor.load_mapping)
    for row in (buildings[0], floors[0], rooms[0]):
        assert "niagara_path" not in row, row
        assert row["target"].startswith("B/"), row


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = []
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as exc:
            failures.append(t.__name__)
            print(f"FAIL  {t.__name__}: {exc}")
        except Exception as exc:                          # noqa: BLE001
            failures.append(t.__name__)
            print(f"ERROR {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    if failures:
        print("Failed: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
