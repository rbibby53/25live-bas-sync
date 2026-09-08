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
25Live -> BAS Schedule Sync.

Pulls room bookings from CollegeNET 25Live and writes them into building
automation schedules. The BAS side is pluggable (see `bassync.drivers`) so one
campus can drive Tridium Niagara, Automated Logic WebCTRL, Schneider
EcoStruxure Building Operation, and any BTL-listed device that exposes a
standard BACnet Schedule object — from a single run.

`main.py` is the CLI; this package holds the logic so it can be unit-tested
without a network.
"""

__version__ = "1.1.0"
