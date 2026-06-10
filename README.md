# 25Live → Niagara N4 Schedule Sync

A single-run script (designed for a daily 2 AM schedule) that pulls confirmed
events from CollegeNET **25Live** and writes them into a **Niagara N4** station as
BACnet `SpecialEvents` at priority 14, so HVAC/lighting pre-conditions for booked
rooms and stands down when they're empty.

## How it works

```
25Live events.xml ──► parse + apply pre/post buffers ──► merge into occupancy
windows ──► roll rooms up into building schedules ──► write to Niagara N4 ──► heartbeat
```

Everything lives in **`main.py`**. The only file you normally edit to change *what*
gets synced is **`space_mapping.yaml`**.

### Building occupancy = union of all its rooms

`space_mapping.yaml` has a `buildings:` section (each building defined once) and a
`spaces:` section (the rooms). Each room names the building it belongs to with a
`building:` id. **Every room in a building is automatically rolled up into that
building's schedule** — if *any* room is occupied, the building schedule (hallways,
lobbies, common AHUs) runs HVAC too. You never repeat the building's Niagara path on
a room, so you can't forget to wire one up. Omit `building:` on a room that should
run on its own only. If a building's common area is itself bookable in 25Live (e.g.
an atrium), give the building a `space_id:` and its own events count too.

## Setup

```bash
pip install -r requirements.txt
```

Set the two passwords as environment variables (never commit them):

```bash
# Windows (PowerShell)
$env:BAS_25LIVE_PASSWORD  = "..."
$env:BAS_NIAGARA_PASSWORD = "..."

# Linux / macOS
export BAS_25LIVE_PASSWORD=...
export BAS_NIAGARA_PASSWORD=...
```

Then edit the `CONFIG` block at the top of `main.py` for your environment
(hosts, usernames, timezone) and fill in `space_mapping.yaml`.

## Editing rooms (GUI)

You don't have to hand-edit YAML. **Double-click `Edit-Rooms.bat`** (or run
`python editor.py`) to open the Room Mapping Editor:

- **Rooms** tab — Add / Edit / Delete rooms. The **Building** field is a dropdown
  of your defined buildings, so a room joins its building's roll-up just by
  picking it. Double-click a row to edit.
- **Buildings** tab — manage the building roll-up schedules. Renaming a building
  id automatically repoints the rooms that referenced it.

It edits the same `space_mapping.yaml` the nightly sync reads, validates required
fields and duplicate IDs, warns if a room points at a missing building, and keeps
a `space_mapping.yaml.bak` of the previous version on every save.

> Note: saving from the editor rewrites the file and **does not preserve inline
> `#` comments** — it regenerates a clean header instead. To keep a per-room note
> through edits, use the **Note** field (saved as a `note:` key the sync ignores).

## Running

```bash
# Safe: fetch from 25Live and print what WOULD be written — no Niagara writes.
python main.py --dry-run

# Live run (writes to Niagara).
python main.py

# Point at a different map file.
python main.py --space-map /path/to/space_mapping.yaml
```

Exit codes (for monitoring): `0` ok · `2` empty map · `3` Niagara unreachable ·
`4` 25Live fetch failed · `5` one or more write failures.

## Scheduling

This runs on the **Niagara 4.15 server itself**, deployed in `D:\BAS`.

**Windows Task Scheduler:**
- Program: `python.exe`
- Arguments: `D:\BAS\main.py`
- Start in: `D:\BAS`
- Trigger: Daily, 02:00
- Set the two `BAS_*_PASSWORD` env vars for the service account that runs the task.

**Linux/macOS cron (if ever deployed off-host):**
```
0 2 * * *  /usr/bin/python3 /opt/bas/main.py
```

Logs go to `D:\BAS\logs\25live_sync.log` on Windows (or `/var/log/bas/` on
Linux/macOS); override via `CONFIG["log_file"]`. If that directory isn't writable
the script logs to stdout instead.

### Niagara 4.15 REST surface — confirm before the first live write

The write side targets N4.15 but its exact REST contract depends on the station's
web service. Three knobs are gathered so they're a one-line change if your station
differs (each is flagged in `NiagaraClient`):
- `NIAGARA_REST_BASE` (default `/rest/v1`) — the REST base path,
- the `SpecialEvent` JSON in `_write_special_event`,
- the schedule slot ORD style (`slot:/Schedules/...`).

Confirm these against the station's REST docs / Workbench `rest` service. `--dry-run`
never touches Niagara, so use it to validate the 25Live side first.

## Changes from the original review

This version is a readability/maintainability refactor of the original script with
a few **conservative correctness fixes** — the core sync semantics (merge logic,
priority-14 writes, clear-then-write idempotency, heartbeat) are unchanged.

Maintainability:
- Magic values gathered into named constants at the top
  (`BACNET_SCHEDULE_PRIORITY`, `PAGE_SIZE`, timeouts, etc.).
- Secret loading centralized in `load_credentials()`, which now **warns** if a
  password is still the `CHANGE_ME` placeholder.
- `--dry-run` no longer duplicates fetch/build logic — it's one path inside
  `run_sync(dry_run=...)`, so the two modes can't drift.
- Consistent type hints; `import os` moved to the top.

Correctness fixes (each called out in code comments):
1. **25Live state filter** — the request now sends the numeric `state` query
   param (derived from `include_states`) instead of `include=confirmed`, so
   `CONFIG` actually drives the request. ⚠️ Verify the exact param format against
   your Kennesaw Series25 WebServices docs — it's flagged with a comment in
   `CollegeNetClient._fetch_batch` and is a one-line change if your instance
   differs.
2. **Log path / schedule mismatch** — log path is now OS-aware (Windows vs
   Linux) instead of a hard-coded `C:\` path, and the docstring shows both
   Windows Task Scheduler and cron.
3. **TLS verification** — `verify_tls=False` now logs an explicit warning so it's
   never silently off; documented how to point it at a CA bundle for production.
4. **URL length** — space IDs are requested in batches of
   `SPACE_IDS_PER_REQUEST` (50) so a large map can't blow the query-string limit.

## Files

| File                  | Purpose                                          |
|-----------------------|--------------------------------------------------|
| `main.py`             | The sync script.                                 |
| `space_mapping.yaml`  | 25Live space → Niagara path map (edit this).     |
| `editor.py`           | GUI to add/edit rooms & buildings (Tkinter).     |
| `Edit-Rooms.bat`      | Double-click launcher for the editor (Windows).  |
| `requirements.txt`    | Python dependencies.                             |
| `Test.py`             | Offline checks for the merge logic + editor I/O. |
