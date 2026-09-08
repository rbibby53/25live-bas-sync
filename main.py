#!/usr/bin/env python3
# 25Live -> BAS Schedule Sync
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
25Live -> BAS Schedule Sync
===========================
Pulls room bookings from CollegeNET 25Live and writes them into building
automation schedules, so HVAC and lighting pre-condition for booked rooms and
stand down when they're empty.

The BAS side is pluggable. One run can drive a mixed campus:

    bacnet    standard BACnet/IP Schedule objects — Tridium Niagara,
              Automated Logic WebCTRL, Schneider EcoStruxure Building
              Operation, and any other BTL-listed controller
    niagara   Niagara N4 BooleanSchedule SpecialEvents over REST
    rest      a vendor REST API you describe in config.yaml
    preview   writes nothing; logs and optionally exports CSV

Runs ONCE per invocation — schedule it nightly (e.g. 2 AM) via Windows Task
Scheduler or cron. See README.md for deployment details.

Quick start:
    pip install -r requirements.txt
    cp config.example.yaml config.yaml         # then edit it for your site
    export BAS_25LIVE_PASSWORD=...
    python main.py --validate                  # pre-flight, no writes
    python main.py --dry-run                   # what WOULD be written
    python main.py                             # live
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from bassync import __version__
from bassync.config import (default_log_file, load_config, load_credentials,
                            resolve_default_system)
from bassync.drivers import driver_names, load_driver_class
from bassync.notify import send_alert
from bassync.sync import (EXIT_CODE_HELP, EXIT_ERROR, EXIT_OK,
                          run_discover, run_sync, run_validate)

PROJECT_DIR = Path(__file__).resolve().parent


def setup_logging(log_file: str, verbose: bool = False) -> None:
    handlers: list = [logging.StreamHandler(sys.stdout)]
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    except OSError:
        # If the log directory isn't writable, fall back to stdout only rather
        # than refusing to run — a nightly job that won't start is worse than
        # one that only logs to the scheduler's captured output.
        pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def print_drivers() -> int:
    print("Available BAS drivers (set as `driver:` under `systems:`):\n")
    for name in driver_names():
        cls = load_driver_class(name)
        print(f"  {name:<10} {cls.description}")
    print("\nFull setup notes for each are in README.md and in the driver's "
          "own module docstring under bassync/drivers/.")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="25Live -> BAS schedule sync (single run).",
        epilog="Exit codes: 0 ok · 1 error · 2 room map · 3 BAS unreachable · "
               "4 25Live fetch · 5 write failures · 6 validation · 7 safety abort.")
    parser.add_argument("--version", action="version",
                        version=f"25live-bas-sync {__version__}")
    parser.add_argument("--config",
                        help="Path to config.yaml (default: ./config.yaml, or $BAS_CONFIG)")
    parser.add_argument("--defaults",
                        help="Path to defaults.yaml (default: ./defaults.yaml, or $BAS_DEFAULTS)")
    parser.add_argument("--space-map", help="Override the path to space_mapping.yaml")

    mode = parser.add_argument_group("modes (default: live sync)")
    mode.add_argument("--dry-run", action="store_true",
                      help="Fetch and build, print what WOULD be written; "
                           "contacts no BAS")
    mode.add_argument("--validate", action="store_true",
                      help="Pre-flight only: config, room map, auth, "
                           "reachability, schedule targets; no writes")
    mode.add_argument("--discover", action="store_true",
                      help="List 25Live spaces with upcoming events (read-only)")
    mode.add_argument("--discover-days", type=int, default=30,
                      help="Window for --discover, in days (default 30)")
    mode.add_argument("--list-drivers", action="store_true",
                      help="Show the available BAS drivers and exit")

    parser.add_argument("--system", metavar="NAME",
                        help="Limit the run to one system from `systems:` — "
                             "use it to commission a building at a time")
    parser.add_argument("--force", action="store_true",
                        help="Override the mass-clear safety check. Needed at "
                             "semester break, when a big drop in bookings is real")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Debug-level logging")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_drivers:
        return print_drivers()

    cfg_path = (args.config or os.environ.get("BAS_CONFIG")
                or str(PROJECT_DIR / "config.yaml"))
    defaults_path = (args.defaults or os.environ.get("BAS_DEFAULTS")
                     or str(PROJECT_DIR / "defaults.yaml"))
    cfg = load_config(cfg_path, defaults_path)

    if args.space_map:
        cfg["space_map_file"] = args.space_map
    if cfg["log_file"] is None:
        cfg["log_file"] = default_log_file()

    setup_logging(cfg["log_file"], args.verbose)
    if not Path(cfg_path).exists():
        logging.warning(
            "No config file at %s — using built-in defaults. Copy "
            "config.example.yaml to config.yaml and edit it for your site.",
            cfg_path)
    load_credentials(cfg)   # after logging is up, so its warnings are captured

    if args.system and args.system not in (cfg.get("systems") or {}):
        logging.error("--system '%s' is not defined under `systems:`. Known: %s",
                      args.system,
                      ", ".join(sorted(cfg.get("systems") or {})) or "(none)")
        return EXIT_ERROR

    # Only a live sync alerts; the other modes are interactive and just return
    # a code to whoever ran them.
    is_live_sync = not (args.validate or args.discover or args.dry_run)

    mode = ("VALIDATE" if args.validate else "DISCOVER" if args.discover
            else "DRY RUN" if args.dry_run else "SYNC")
    systems = cfg.get("systems") or {}
    logging.info("=== 25Live -> BAS sync %s starting (%s, lookahead %d days) ===",
                 __version__, mode, cfg["collegenet"]["lookahead_days"])
    logging.info("Systems: %s | default: %s",
                 ", ".join(f"{n} ({c.get('driver', '?')})"
                           for n, c in sorted(systems.items())) or "(none)",
                 resolve_default_system(cfg) or "(none)")

    code = EXIT_ERROR
    try:
        if args.validate:
            code = run_validate(cfg)
        elif args.discover:
            code = run_discover(cfg, args.discover_days)
        else:
            code = run_sync(cfg, dry_run=args.dry_run, force=args.force,
                            only_system=args.system)
    except KeyboardInterrupt:
        logging.warning("Interrupted — some schedules may be partially written. "
                        "Re-run to bring everything back into agreement.")
        return 130
    except Exception as exc:                       # noqa: BLE001 — last-resort guard
        logging.exception("Unhandled error during run")
        code = EXIT_ERROR
        if is_live_sync:
            send_alert(cfg["alerts"], "25Live -> BAS sync CRASHED",
                       f"Unhandled error: {exc}")
            logging.info("=== Exited with code %d ===", code)
            # Return here so a crash sends exactly one alert, not a "crashed"
            # and a "failed" for the same event.
            return code

    if is_live_sync:
        if code != EXIT_OK:
            send_alert(cfg["alerts"],
                       f"25Live -> BAS sync FAILED (exit {code})",
                       EXIT_CODE_HELP.get(code, "See the log for details."))
        elif cfg["alerts"].get("notify_on_success"):
            send_alert(cfg["alerts"], "25Live -> BAS sync OK",
                       "Sync completed successfully.")

    logging.info("=== Exited with code %d ===", code)
    return code


if __name__ == "__main__":
    sys.exit(main())
