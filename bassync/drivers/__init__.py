# 25Live -> BAS Schedule Sync — driver registry
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Driver registry.

    bacnet    Standard BACnet/IP Schedule objects, and the driver to reach
              for first. Automated Logic WebCTRL, Schneider EcoStruxure
              Building Operation, Tridium Niagara and any other BTL-listed
              controller all expose them, so one integration covers the whole
              campus against a published standard rather than a per-vendor,
              per-version API.
    niagara   Tridium Niagara N4 BooleanSchedule SpecialEvents over REST — for
              stations whose schedules are not exported to BACnet, or where you
              want the bookings to live natively in the station.
    rest      Generic REST driver you describe in config.yaml — the escape
              hatch for a vendor API when BACnet is not available.
    preview   Writes nothing; logs and optionally exports CSV. Useful for
              staged commissioning, one building at a time.

Drivers are imported lazily so a site never has to install a dependency for a
system it does not use — BACpypes3 is only needed if something actually uses
the bacnet driver.
"""

from typing import Optional
from zoneinfo import ZoneInfo

from .base import DriverError, ScheduleWriter

# driver name -> (module, class). Import happens on first use.
_REGISTRY = {
    "bacnet": (".bacnet", "BacnetScheduleWriter"),
    "niagara": (".niagara", "NiagaraScheduleWriter"),
    "rest": (".rest", "RestScheduleWriter"),
    "preview": (".preview", "PreviewScheduleWriter"),
}

# Names people reasonably type that mean an existing driver. Kept explicit so
# `--list-drivers` stays a short, honest list rather than pretending there is a
# bespoke integration behind every vendor name.
_ALIASES = {
    "bacnet-ip": "bacnet",
    "bacnet/ip": "bacnet",
    "tridium": "niagara",
    "n4": "niagara",
    "dry-run": "preview",
    "none": "preview",
}


def driver_names() -> list:
    return sorted(_REGISTRY)


def load_driver_class(driver: str):
    """Import and return a driver class by name."""
    key = _ALIASES.get((driver or "").strip().lower(), (driver or "").strip().lower())
    if key not in _REGISTRY:
        raise DriverError(
            f"Unknown BAS driver {driver!r}. Available: "
            f"{', '.join(driver_names())}.")
    module_name, class_name = _REGISTRY[key]
    from importlib import import_module
    module = import_module(module_name, package=__name__)
    return getattr(module, class_name)


def build_driver(system_name: str, sys_cfg: dict, tz: ZoneInfo,
                 retry: Optional[dict] = None) -> ScheduleWriter:
    """Instantiate the driver for one entry of the `systems:` config block."""
    if not isinstance(sys_cfg, dict):
        raise DriverError(
            f"System '{system_name}' must be a mapping of settings, got "
            f"{type(sys_cfg).__name__}.")
    driver = sys_cfg.get("driver")
    if not driver:
        raise DriverError(
            f"System '{system_name}' has no `driver:`. Set one of: "
            f"{', '.join(driver_names())}.")
    cls = load_driver_class(driver)
    return cls(system_name, sys_cfg, tz, retry)


__all__ = ["DriverError", "ScheduleWriter", "build_driver",
           "load_driver_class", "driver_names"]
