# 25Live -> BAS Schedule Sync — occupancy builder
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Turns raw 25Live space assignments into the set of occupancy windows each BAS
schedule should hold.

Occupancy rolls up in three tiers — room -> floor corridor -> building — so a
single evening booking on the third floor conditions that room and its corridor
without running the whole tower.

Two merges happen, and the distinction matters when tuning:

  1. Within a space, using that space's own `merge_gap_minutes` — "don't cycle
     the box off for the twelve minutes between two back-to-back classes".
  2. Across the spaces feeding a roll-up (floor or building), using the global
     default gap — "the corridor is occupied whenever any room off it is".
"""

import logging
from collections import defaultdict

from .model import Destination, OccupancyWindow


class ScheduleBuilder:
    def __init__(self, default_merge_gap_minutes: int):
        # Used for building roll-ups (which span multiple rooms) and as the
        # fallback when a space doesn't override merge_gap_minutes.
        self.default_merge_gap = default_merge_gap_minutes

    def build(self, events: list, space_map) -> dict:
        """Returns { Destination: [OccupancyWindow, ...] }."""
        spaces = getattr(space_map, "spaces", space_map)

        by_space: dict = defaultdict(list)
        for ev in events:
            by_space[ev.space_id].append(ev)

        result: dict = {}
        # Floor corridors and building roll-ups accumulate the same way, so
        # they share one bucket keyed by Destination.
        rollup_windows: dict = defaultdict(list)

        for space_id, evs in by_space.items():
            sc = spaces.get(space_id)
            if sc is None:
                # 25Live returned a space we don't map. The fetcher already
                # filters these out; belt and braces so an unmapped id can
                # never take the run down.
                continue
            windows = self._merge(
                sorted((OccupancyWindow(e.start, e.end, [e.event_id]) for e in evs),
                       key=lambda w: w.start),
                sc.merge_gap_minutes)

            if sc.space_type == "room":
                if sc.destination is not None:
                    # Union rather than assign: two 25Live spaces may
                    # legitimately share one schedule (a divisible room split
                    # A/B in 25Live but served by a single AHU). Assigning
                    # would silently drop the first room's bookings and leave
                    # that half of the room cold.
                    self._accumulate(result, sc.destination, windows,
                                     sc.merge_gap_minutes)
                # The room feeds its floor corridor AND its building — whether
                # or not it has a schedule of its own. A building that can only
                # be scheduled at the air handler still needs to know its rooms
                # are booked.
                for dest in sc.rollup_destinations():
                    rollup_windows[dest].extend(windows)
            elif sc.space_type == "building":
                # A directly-booked common area (e.g. an atrium).
                rollup_windows[sc.destination].extend(windows)

        # Roll-ups: rooms plus any direct common-area bookings, merged across
        # spaces with the global default gap. A room's own gap override shapes
        # the windows it contributes, so a room held "occupied" across a gap
        # keeps its corridor and building occupied too.
        for dest, windows in rollup_windows.items():
            self._accumulate(result, dest, windows, self.default_merge_gap)

        total = sum(len(v) for v in result.values())
        logging.info("Built %d occupancy window(s) across %d schedule(s)",
                     total, len(result))
        return result

    def _accumulate(self, result: dict, dest: Destination, windows: list,
                    gap_minutes: int) -> None:
        """Union `windows` into whatever this destination already holds."""
        combined = result.get(dest, []) + list(windows)
        result[dest] = self._merge(sorted(combined, key=lambda w: w.start),
                                   gap_minutes)

    @staticmethod
    def _merge(windows: list, gap_minutes: int) -> list:
        """Collapse a time-sorted list of windows that touch, overlap, or sit
        within `gap_minutes` of each other."""
        if not windows:
            return []
        merged = [windows[0]]
        for w in windows[1:]:
            if merged[-1].overlaps_or_adjacent(w, gap_minutes):
                merged[-1] = merged[-1].merge(w)
            else:
                merged.append(w)
        return merged
