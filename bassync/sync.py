# 25Live -> BAS Schedule Sync — run orchestration
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
The three things a run can do — sync, validate, discover — and the exit codes
that let monitoring tell them apart.

A sync pass:
    load room map -> fetch 25Live -> apply buffers -> merge -> roll up
    -> safety check -> fan out to each BAS driver -> heartbeat -> save state
"""

import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from . import safety
from .collegenet import CollegeNetClient, CollegeNetError
from .drivers import DriverError, build_driver
from .model import Destination
from .schedule import ScheduleBuilder
from .spacemap import load_space_map

# Exit codes, for cron/Task Scheduler monitoring.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_MAP = 2
EXIT_BAS_UNREACHABLE = 3
EXIT_FETCH_FAILED = 4
EXIT_WRITE_FAILURES = 5
EXIT_VALIDATION_FAILED = 6
EXIT_SAFETY_ABORT = 7

EXIT_CODE_HELP = {
    EXIT_ERROR: "Unhandled error (see the log).",
    EXIT_NO_MAP: "Room map empty, missing, or invalid.",
    EXIT_BAS_UNREACHABLE: "A BAS system was unreachable.",
    EXIT_FETCH_FAILED: "25Live fetch failed (auth, network, or config).",
    EXIT_WRITE_FAILURES: "One or more BAS writes failed.",
    EXIT_VALIDATION_FAILED: "Validation failed.",
    EXIT_SAFETY_ABORT: ("Refused to clear a large share of schedules at once — "
                        "check 25Live, then re-run with --force if the drop is real."),
}


def _fetch(cfg: dict, tz: ZoneInfo, space_map):
    """Pull events, translating transport failures into CollegeNetError."""
    client = CollegeNetClient(cfg["collegenet"], tz, cfg.get("retry"))
    try:
        return client.fetch_events(space_map)
    except requests.RequestException as exc:
        raise CollegeNetError(f"Failed to fetch from 25Live: {exc}") from exc
    finally:
        client.close()


def _report_map_problems(space_map) -> None:
    for err in space_map.errors:
        logging.error("Room map: %s", err)


def run_sync(cfg: dict, dry_run: bool = False, force: bool = False,
             only_system: Optional[str] = None) -> int:
    """
    One full sync pass. Returns a process exit code.

    dry_run fetches and builds, logs what *would* be written per driver, and
    contacts no BAS. It is the same code path as a live run right up to the
    write, so a clean dry run means the fetch, mapping and merge are all good.
    """
    tz = ZoneInfo(cfg["timezone"])

    space_map = load_space_map(cfg["space_map_file"], cfg)
    _report_map_problems(space_map)
    if space_map.errors:
        logging.error("Room map has %d problem(s) — fix them before syncing.",
                      len(space_map.errors))
        return EXIT_NO_MAP
    if not space_map:
        logging.error("Room map is empty — nothing to sync.")
        return EXIT_NO_MAP

    if not cfg["collegenet"].get("base_url"):
        logging.error(
            "25Live base_url is not set — set collegenet.instance (for "
            "CollegeNET-hosted sites) or collegenet.base_url in config.yaml. "
            "Did you copy config.example.yaml to config.yaml?")
        return EXIT_FETCH_FAILED

    try:
        events = _fetch(cfg, tz, space_map)
    except CollegeNetError as exc:
        logging.error("%s", exc)
        return EXIT_FETCH_FAILED

    builder = ScheduleBuilder(cfg["collegenet"]["merge_gap_minutes"])
    schedule = builder.build(events, space_map)

    # Every schedule this map owns — including roll-ups and rooms with no
    # bookings this week, which must be actively cleared rather than left
    # holding last week's occupancy.
    all_destinations = space_map.destinations()
    if only_system:
        all_destinations = {d for d in all_destinations if d.system == only_system}
        schedule = {d: w for d, w in schedule.items() if d.system == only_system}
        logging.info("Limited to system '%s': %d schedule(s)",
                     only_system, len(all_destinations))
        if not all_destinations:
            logging.error("No schedules belong to system '%s'.", only_system)
            return EXIT_NO_MAP

    for dest in all_destinations:
        schedule.setdefault(dest, [])

    if dry_run:
        return _preview(cfg, tz, schedule)

    verdict = safety.check(cfg, schedule, all_destinations, len(events),
                           only_system=only_system)
    if not verdict:
        if force:
            logging.warning("SAFETY OVERRIDE (--force): %s", verdict.reason)
        else:
            logging.error("ABORTING before any write. %s", verdict.reason)
            return EXIT_SAFETY_ABORT
    else:
        logging.info("Safety check passed: %s", verdict.reason)

    code, written = _write_all(cfg, tz, schedule)
    # Record what actually landed, not what was intended. A destination whose
    # write failed keeps its previous baseline, so the next run compares
    # against reality; recording the intent would quietly assert that a
    # building is scheduled when it isn't. Saving on a partial failure also
    # keeps the baseline fresh — otherwise one persistently broken system
    # freezes it, and weeks of legitimate drift eventually reads as a mass
    # clear.
    if written:
        safety.save_state(cfg["safety"]["state_file"],
                          {d: w for d, w in schedule.items() if d in written},
                          len(events), only_system=only_system,
                          merge=code != EXIT_OK)
    return code


def _group_by_system(schedule: dict) -> dict:
    by_system: dict = defaultdict(dict)
    for dest, windows in schedule.items():
        by_system[dest.system][dest.target] = windows
    return by_system


def _preview(cfg: dict, tz: ZoneInfo, schedule: dict) -> int:
    """Log what a live run would write, using each driver's own encoding so
    the preview reflects what actually goes on the wire."""
    logging.info("DRY RUN — no BAS was contacted. Schedules that WOULD be written:")
    systems = cfg.get("systems") or {}
    for system_name, targets in sorted(_group_by_system(schedule).items()):
        sys_cfg = systems.get(system_name)
        driver = None
        if sys_cfg:
            try:
                driver = build_driver(system_name, sys_cfg, tz, cfg.get("retry"))
            except DriverError as exc:
                logging.error("System '%s': %s", system_name, exc)
        logging.info("--- system '%s' (%s) — %d schedule(s) ---", system_name,
                     (sys_cfg or {}).get("driver", "unconfigured"), len(targets))
        for target, windows in sorted(targets.items()):
            if driver is not None:
                # describe() is pure formatting in every shipped driver, so a
                # dry run stays offline even for BACnet.
                logging.info("  %s", driver.describe(target, windows))
            else:
                logging.info("  %s: %d window(s)", target, len(windows))
        if driver is not None:
            driver.close()
    return EXIT_OK


def _write_all(cfg: dict, tz: ZoneInfo, schedule: dict) -> tuple:
    """
    Fan out to each system's driver. One unreachable BAS fails that system's
    schedules, not the whole campus.

    Returns (exit_code, set_of_destinations_actually_written).
    """
    systems = cfg.get("systems") or {}
    failures = 0
    written: set = set()
    unreachable = 0

    for system_name, targets in sorted(_group_by_system(schedule).items()):
        sys_cfg = systems.get(system_name)
        if not sys_cfg:
            logging.error("System '%s' is referenced by %d schedule(s) but not "
                          "defined under `systems:` in config.yaml.",
                          system_name, len(targets))
            failures += len(targets)
            continue

        try:
            driver = build_driver(system_name, sys_cfg, tz, cfg.get("retry"))
        except DriverError as exc:
            logging.error("System '%s': %s", system_name, exc)
            failures += len(targets)
            continue

        try:
            try:
                driver.connect()
                ok, detail = driver.health_check()
            except Exception as exc:                      # noqa: BLE001
                # A driver that cannot even start (bad NIC address, failed
                # login) fails ITS schedules. The other systems still get
                # their night's write.
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            if not ok:
                logging.error("System '%s' unreachable (%s) — skipping its %d "
                              "schedule(s).", system_name, detail, len(targets))
                unreachable += 1
                failures += len(targets)
                continue
            logging.info("--- system '%s' (%s): %s ---", system_name,
                         sys_cfg.get("driver"), detail)

            for target, windows in sorted(targets.items()):
                try:
                    driver.write_schedule(target, windows)
                    written.add(Destination(system_name, target))
                except Exception as exc:                  # noqa: BLE001
                    logging.error("Error writing %s:%s — %s",
                                  system_name, target, exc)
                    failures += 1

            try:
                driver.write_heartbeat(datetime.now(tz))
            except Exception as exc:                      # noqa: BLE001
                logging.warning("System '%s': heartbeat failed: %s",
                                system_name, exc)
        finally:
            driver.close()

    if failures:
        logging.error("Sync finished with %d write failure(s); %d schedule(s) "
                      "written successfully.", failures, len(written))
        # An unreachable system is a different operational problem from a
        # rejected write, and worth its own exit code for monitoring.
        code = (EXIT_BAS_UNREACHABLE
                if unreachable and not written else EXIT_WRITE_FAILURES)
        return code, written

    logging.info("Sync completed successfully — %d schedule(s) written across "
                 "%d system(s).", len(written), len(_group_by_system(schedule)))
    return EXIT_OK, written


def run_validate(cfg: dict) -> int:
    """
    Pre-flight (no writes): config, room map, 25Live auth, every BAS reachable,
    every schedule target resolvable. Logs a PASS/FAIL line per check and
    returns 0 only if all pass.
    """
    tz = ZoneInfo(cfg["timezone"])
    checks: list = []

    space_map = load_space_map(cfg["space_map_file"], cfg)
    checks.append(("Room map loads", not space_map.errors and bool(space_map),
                   f"{len(space_map)} space(s), {space_map.building_count} building(s)"
                   if space_map and not space_map.errors
                   else "; ".join(space_map.errors) or "empty"))

    base_url = cfg["collegenet"].get("base_url")
    checks.append(("25Live base_url configured", bool(base_url),
                   base_url or "set collegenet.instance or base_url"))

    if base_url:
        client = CollegeNetClient(cfg["collegenet"], tz, cfg.get("retry"))
        try:
            ok, detail = client.check_connection()
        finally:
            client.close()
        checks.append(("25Live reachable + authenticated", ok, detail))

    systems = cfg.get("systems") or {}
    if not systems:
        checks.append(("BAS systems configured", False,
                       "no `systems:` block in config.yaml"))

    used = space_map.systems_used() if space_map else set(systems)
    by_system: dict = defaultdict(list)
    for dest in (space_map.destinations() if space_map else set()):
        by_system[dest.system].append(dest.target)

    for system_name in sorted(used):
        sys_cfg = systems.get(system_name)
        if not sys_cfg:
            checks.append((f"System '{system_name}' defined", False,
                           "referenced by the room map but not in `systems:`"))
            continue
        try:
            driver = build_driver(system_name, sys_cfg, tz, cfg.get("retry"))
        except DriverError as exc:
            checks.append((f"System '{system_name}' driver", False, str(exc)))
            continue
        try:
            try:
                driver.connect()
                ok, detail = driver.health_check()
            except Exception as exc:                      # noqa: BLE001
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            checks.append((f"System '{system_name}' reachable", ok, detail))
            if ok:
                missing = []
                for target in sorted(set(by_system.get(system_name, []))):
                    try:
                        exists, why = driver.target_exists(target)
                    except Exception as exc:              # noqa: BLE001
                        exists, why = False, f"{type(exc).__name__}: {exc}"
                    if not exists:
                        missing.append(f"{target} ({why})")
                checks.append((f"System '{system_name}' schedules exist",
                               not missing,
                               "all present" if not missing
                               else f"{len(missing)} missing: "
                                    f"{', '.join(missing[:5])}"
                                    + (f" (+{len(missing) - 5} more)"
                                       if len(missing) > 5 else "")))
        finally:
            driver.close()

    logging.info("=== Validation results ===")
    for name, ok, detail in checks:
        logging.info("  [%-4s] %s — %s", "PASS" if ok else "FAIL", name, detail)
    for warning in (space_map.warnings if space_map else []):
        logging.info("  [WARN] %s", warning)

    all_ok = all(ok for _, ok, _ in checks)
    logging.info("=== Validation %s ===", "PASSED" if all_ok else "FAILED")
    return EXIT_OK if all_ok else EXIT_VALIDATION_FAILED


def run_discover(cfg: dict, days: int) -> int:
    """List 25Live spaces with events in the next `days` days, as a starter for
    space_mapping.yaml. Read-only."""
    tz = ZoneInfo(cfg["timezone"])
    if not cfg["collegenet"].get("base_url"):
        logging.error("25Live base_url is not set — configure "
                      "collegenet.instance or base_url in config.yaml.")
        return EXIT_FETCH_FAILED

    client = CollegeNetClient(cfg["collegenet"], tz, cfg.get("retry"))
    try:
        spaces = client.discover_spaces(days)
    except (requests.RequestException, CollegeNetError) as exc:
        logging.error("Discovery failed: %s", exc)
        return EXIT_FETCH_FAILED
    finally:
        client.close()

    logging.info("Discovered %d space(s) with events in the next %d days:",
                 len(spaces), days)
    lines = ["", "# --- discovered spaces (add building + system + target) ---",
             "spaces:"]
    for s in spaces:
        lines.append(f"  - space_id: {s['space_id']}")
        lines.append(f"    space_name: \"{s['space_name']}\"")
        lines.append("    target: \"\"   # TODO: the BAS schedule address")
    print("\n".join(lines))
    return EXIT_OK
