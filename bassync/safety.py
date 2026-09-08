# 25Live -> BAS Schedule Sync — mass-clear safety rail
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Refuses to turn the campus off by accident.

The failure that matters here is not a crash — it is a *successful-looking*
run that writes empty schedules everywhere. An expired 25Live service account,
a changed `state` query parameter, a Series25 version bump that renames an XML
element: each one returns HTTP 200 with zero events, and the sync then
faithfully clears every schedule it manages. Nobody notices until Monday
morning when the buildings are cold.

So a run compares itself to the last one and stops if too much occupancy
disappeared at once:

  * fewer than `min_events` assignments came back from 25Live at all, or
  * more than `max_cleared_fraction` of the schedules that had bookings last
    time would be emptied now.

Both are deliberately about *change*, not absolute counts — a genuinely quiet
week (spring break) still has last week's state to compare against, and a
first-ever run has nothing to compare so it is allowed through.

`--force` overrides, and is the right answer at the end of a semester when the
drop is real.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class SafetyVerdict:
    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


def load_state(path: str) -> dict:
    """Previous run's per-destination window counts. Missing/corrupt state is
    treated as "no history" rather than an error — the rail is a guard, and a
    guard that blocks the first run is just an outage."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: str, schedule: dict, event_count: int,
               only_system: Optional[str] = None, merge: bool = False) -> None:
    """
    Record what this run wrote, for the next run to compare against.

    Merges into the existing state rather than replacing it when the run only
    covered part of the campus:

      * `only_system` — a `--system` run knows nothing about the other systems'
        schedules, and overwriting their baseline with silence would make the
        NEXT full run look like a campus-wide clear.
      * `merge` — a partially failed run. Only the destinations that actually
        got written are passed in; everything else keeps its prior baseline.
    """
    windows = {str(dest): len(w) for dest, w in schedule.items()}
    if only_system or merge:
        merged = (load_state(path).get("windows") or {})
        merged.update(windows)
        windows = merged
    payload = {
        "written_at": datetime.now().astimezone().isoformat(),
        "event_count": event_count,
        "windows": windows,
    }
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except OSError as exc:
        logging.warning("Could not save run state to %s: %s — the mass-clear "
                        "safety check will have no history next run.", path, exc)


def check(cfg: dict, schedule: dict, all_destinations: set,
          event_count: int, previous: Optional[dict] = None) -> SafetyVerdict:
    """
    Decide whether this run is safe to write.

    `schedule` is what was built (destinations with bookings); anything in
    `all_destinations` and not in `schedule` gets cleared.

    The comparison is scoped to `all_destinations`, which handles both a
    `--system` run (where it holds only that system's schedules) and rooms
    removed from the map since the last run.
    """
    safety = cfg.get("safety") or {}
    if not safety.get("enabled", True):
        return SafetyVerdict(True, "safety checks disabled in config")

    min_events = int(safety.get("min_events", 1))
    if event_count < min_events:
        return SafetyVerdict(False, (
            f"25Live returned {event_count} space assignment(s), below "
            f"min_events={min_events}. That usually means an auth or query "
            "problem rather than an empty campus — writing now would clear "
            f"all {len(all_destinations)} schedule(s). Check the 25Live "
            "credentials and the `state` parameter, or re-run with --force if "
            "the campus really is empty."))

    if previous is None:
        previous = load_state(safety.get("state_file") or "")
    prior_windows = previous.get("windows") or {}
    # Only schedules this run still manages can be "cleared" by it. A room
    # taken out of the map — decommissioned, handed to a contractor, moved to
    # another system — is no longer written at all, so counting it as cleared
    # would block the next sync with a false alarm about buildings nobody is
    # touching.
    managed = {str(d) for d in all_destinations}
    prior_windows = {k: v for k, v in prior_windows.items() if k in managed}
    previously_occupied = {k for k, v in prior_windows.items() if v}
    if not previously_occupied:
        return SafetyVerdict(True, "no previous run to compare against")

    now_occupied = {str(dest) for dest, windows in schedule.items() if windows}
    cleared = previously_occupied - now_occupied
    fraction = len(cleared) / len(previously_occupied)
    limit = float(safety.get("max_cleared_fraction", 0.34))

    if fraction > limit:
        sample = ", ".join(sorted(cleared)[:5])
        more = f" (+{len(cleared) - 5} more)" if len(cleared) > 5 else ""
        return SafetyVerdict(False, (
            f"{len(cleared)} of {len(previously_occupied)} previously-occupied "
            f"schedules ({fraction:.0%}) would be cleared this run, above "
            f"max_cleared_fraction={limit:.0%}. Affected: {sample}{more}. If "
            "this drop is real — semester break, a building taken offline — "
            "re-run with --force. Otherwise check 25Live before writing."))

    return SafetyVerdict(True, (
        f"{len(cleared)}/{len(previously_occupied)} previously-occupied "
        f"schedules clearing ({fraction:.0%}), within the "
        f"{limit:.0%} limit"))
