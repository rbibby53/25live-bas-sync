# 25Live -> BAS Schedule Sync — preview driver
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
A driver that writes nothing.

Two real uses beyond testing:

  * Commission one building at a time. Point the buildings you have not cut
    over yet at a `preview` system and they log what they would do while the
    live ones actually write.
  * Hand a controls contractor a CSV of exactly what the sync intends to do,
    before anyone lets it near the station. Set `csv_file` and every window
    lands in one spreadsheet.
"""

import csv
import logging
from pathlib import Path

from .base import ScheduleWriter


class PreviewScheduleWriter(ScheduleWriter):
    """Logs (and optionally CSV-exports) what would be written."""

    name = "preview"
    description = "Writes nothing — logs, and can export a CSV of the intent."

    def __init__(self, system_name: str, cfg: dict, tz, retry=None):
        super().__init__(system_name, cfg, tz, retry)
        self.csv_file = cfg.get("csv_file") or ""
        self._rows: list = []

    def health_check(self) -> tuple[bool, str]:
        return True, "preview driver (no BAS contacted)"

    def write_schedule(self, target: str, windows: list) -> None:
        logging.info("[preview] %s", self.describe(target, windows))
        for w in windows:
            self._rows.append({
                "system": self.system_name,
                "target": target,
                "start": w.start.isoformat(),
                "end": w.end.isoformat(),
                "hours": round(w.duration.total_seconds() / 3600, 2),
                "event_ids": " ".join(str(e) for e in w.source_event_ids),
            })
        if not windows:
            self._rows.append({
                "system": self.system_name, "target": target,
                "start": "", "end": "", "hours": 0, "event_ids": "",
            })

    def close(self) -> None:
        if not (self.csv_file and self._rows):
            return
        path = Path(self.csv_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(self._rows[0]))
                writer.writeheader()
                writer.writerows(self._rows)
        except OSError as exc:
            logging.warning("Could not write preview CSV %s: %s", path, exc)
            return
        logging.info("Preview CSV written: %s (%d rows)", path, len(self._rows))
