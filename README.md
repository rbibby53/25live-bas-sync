# 25Live → Niagara Schedule Sync

Drive your building automation occupancy from your room bookings. This tool pulls
confirmed events from **CollegeNET 25Live** (Series25 WebServices) and writes them
into a **Tridium Niagara** station as `BooleanSchedule` **SpecialEvents**, so HVAC
and lighting pre-condition for booked rooms and stand down when they're empty.

It's a single-run script meant to be scheduled nightly. It's read-only against
25Live and only writes occupancy schedules to Niagara.

> Built for one campus and generalized for others. **Two things are
> instance-specific and must be confirmed before your first live write:** the
> 25Live `state` request parameter and the Niagara REST contract (base path,
> SpecialEvent JSON, ORD style). Both are flagged in code and discussed below.

## Features

- Pulls the next *N* days of confirmed events for a configured set of spaces.
- Per-room **pre-conditioning** and **post-event** buffers (with global defaults).
- Merges overlapping/adjacent bookings into clean occupancy windows.
- **Building roll-up**: every room in a building automatically unions into that
  building's schedule — if *any* room is occupied, common areas run too.
- A small **Tkinter GUI** (`editor.py`) so non-developers can manage the room map,
  with built-in *Test connection* and *Preview* tools.
- `--dry-run`, `--validate` (pre-flight), and `--discover` modes; structured
  logging; a monitoring heartbeat; **automatic retries**; and **failure alerts**
  (webhook/email).

## How it works

```
25Live events  ──►  parse + apply pre/post buffers  ──►  merge into occupancy
windows  ──►  roll rooms up into building schedules  ──►  write to Niagara  ──►  heartbeat
```

The Python (`main.py`) stays generic. Everything site-specific lives in two YAML
files you create from the provided examples.

## Requirements

- **Python 3.9+** (uses `zoneinfo` and `list[...]`/`dict[...]` type hints).
- A **local 25Live account** (not SSO) with read access to your events/locations,
  and Series25 WebServices enabled.
- A **Niagara station** with `BooleanSchedule` components and a reachable web/REST
  service (see *Niagara setup*).

## Install

```bash
git clone https://github.com/rbibby53/25Live_Niagara_Sync.git
cd 25Live_Niagara_Sync
pip install -r requirements.txt
```

> **Windows:** `requirements.txt` includes `tzdata` on purpose — Windows has no
> system timezone database, so without it `ZoneInfo(...)` raises
> `ZoneInfoNotFoundError` and the sync won't start. Install the dependencies for
> the **same** `python` that the scheduled task and `Edit-Rooms.bat` invoke.

## Configure

**1. Settings** — copy the example and edit it for your institution:

```bash
cp config.example.yaml config.yaml
```

`config.yaml` holds your 25Live instance, Niagara host, accounts, timezone, and
schedule paths. It's **gitignored** so your settings never get committed. Anything
you omit falls back to the defaults in `main.py`. You can point at a different file
with `--config PATH` or the `BAS_CONFIG` environment variable.

**2. Secrets** — passwords are **never** stored in files; set them as environment
variables:

```bash
# Linux / macOS
export BAS_25LIVE_PASSWORD=...
export BAS_NIAGARA_PASSWORD=...

# Windows (PowerShell)
$env:BAS_25LIVE_PASSWORD  = "..."
$env:BAS_NIAGARA_PASSWORD = "..."
```

**3. Room map** — copy the example, then edit by hand or with the GUI:

```bash
cp space_mapping.example.yaml space_mapping.yaml
```

### The room map (`space_mapping.yaml`)

Two sections: `buildings:` (each roll-up schedule, defined once) and `spaces:`
(the rooms). Each room names its `building:` by id, and **every room in a building
is automatically unioned into that building's schedule** — you never repeat the
building's Niagara path on a room, so you can't forget to wire one up. Omit
`building:` on a room that should run standalone. If a building's common area is
itself bookable in 25Live (e.g. an atrium), give the building a `space_id:` and its
own events count too. `space_mapping.example.yaml` documents every field.

### Editing rooms with the GUI

Run `python editor.py` (Windows users can double-click `Edit-Rooms.bat`):

- **Rooms** tab — Add/Edit/Delete rooms; the **Building** field is a dropdown of
  your defined buildings, so a room joins its roll-up just by picking it.
- **Buildings** tab — manage roll-up schedules; renaming a building id repoints the
  rooms that referenced it.
- **Tools** menu — *Test 25Live connection*, *Test Niagara connection*, and
  *Preview (dry run)* run against your `config.yaml` without leaving the editor.

It validates required fields and duplicate IDs, warns on missing-building
references, and keeps a `.bak` of the previous version on save.

> Saving from the editor rewrites the file and **does not preserve inline `#`
> comments** — it regenerates a clean header. To keep a per-room note through
> edits, use the **Note** field (stored as a `note:` key the sync ignores).

## Running

```bash
python main.py --validate    # pre-flight: config, auth, reachability, ORDs exist (no writes)
python main.py --discover    # list 25Live spaces with upcoming events (read-only)
python main.py --dry-run     # fetch + build, print what WOULD be written (no writes)
python main.py               # live run (writes to Niagara)
python main.py --config /etc/25live/config.yaml --space-map /etc/25live/rooms.yaml
```

- **`--validate`** is the deployment-confidence command: it checks the room map
  loads, 25Live authenticates, Niagara is reachable, and **every schedule ORD
  exists** — without writing anything. Run it first.
- **`--discover`** (optionally `--discover-days N`, default 30) lists spaces that
  have bookings, as a starter you can paste into `space_mapping.yaml`.

Exit codes (for monitoring): `0` ok · `1` unhandled error · `2` empty/missing room
map · `3` Niagara unreachable · `4` 25Live fetch failed · `5` write failures ·
`6` validation failed.

## Reliability & alerts

- **Retries** — transient 25Live/Niagara errors (timeouts, 429, 5xx) are retried
  with exponential backoff (`retry:` in config). Reads and the idempotent clear
  retry; writes do not, so a retry can't create duplicate events.
- **Alerts** — set `alerts.enabled: true` to get a **webhook** (Slack/Teams/
  generic) and/or **email** notification when a run fails (or, optionally, on
  success). The SMTP password comes from the `BAS_SMTP_PASSWORD` env var. Pair
  this with the Niagara heartbeat point for end-to-end monitoring.

## Scheduling

Run it once per night. It's typically deployed **on the Niagara station server**
itself, but it can run anywhere that can reach both 25Live and the station.

**Windows Task Scheduler:**
- Program: `python.exe`
- Arguments: `<install-dir>\main.py`
- Start in: `<install-dir>`
- Trigger: Daily, e.g. 02:00
- Set the two `BAS_*_PASSWORD` env vars for the account that runs the task.

**Linux/macOS cron:**
```
0 2 * * *  /usr/bin/python3 /opt/25live-niagara-sync/main.py
```

Logs default to `logs/25live_sync.log` next to the script (override with
`log_file` in `config.yaml`); if that directory isn't writable the script logs to
stdout instead.

## Niagara setup

This populates the *calendar* of a schedule; you still wire the schedule into your
equipment once in Workbench:

1. Create a `BooleanSchedule` at each ORD in your map (the room ones **and** the
   building roll-up ones), under `niagara.schedule_base_path` (default
   `slot:/Schedules`).
2. Set the schedule's normal weekly default to **Unoccupied** (the sync only writes
   the booking *special events*).
3. Link each schedule's `out` into your occupancy logic (room → that zone; building
   → common AHUs/hallways).

**Confirm the REST contract for your station/version.** The write path
(`NiagaraClient`) assumes a REST surface — these three knobs are isolated so
adapting is a small change: `NIAGARA_REST_BASE` (default `/rest/v1`), the
`SpecialEvent` JSON in `_write_special_event`, and the `slot:/...` ORD style. If
your station exposes schedules via oBIX or BACnet instead, swap the write layer.
`--dry-run` never touches Niagara, so validate the 25Live side independently first.

## 25Live setup

- Create a **local** service account (not SSO) with read access to the relevant
  events and locations, and enable Series25 WebServices for it.
- Set `collegenet.instance` in `config.yaml` (CollegeNET-hosted), or set
  `collegenet.base_url` directly if self-hosted.
- The request filters confirmed events by the numeric `state` param derived from
  `include_states`. **Confirm the exact format for your instance** — it's flagged in
  `CollegeNetClient._fetch_batch` and is a one-line change if yours differs.
- Find a space's numeric `space_id` from its detail-page URL in 25Live, or via your
  Series25 admin tools.

## Tests

Offline tests (no 25Live/Niagara needed) cover the merge/roll-up logic, the loader,
and the editor's YAML round-trip:

```bash
python Test.py
```

## Contributing

Issues and pull requests welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). This is a
community effort to help campuses tie occupancy to bookings; contributions that
support other 25Live/Niagara configurations are especially valuable.

## License

Licensed under the **GNU General Public License v3.0** — see [LICENSE](LICENSE).
Copyright © 2026 Ryan Bibby and contributors.

## Disclaimer

This software writes occupancy schedules to building automation systems. **Test
thoroughly with `--dry-run` and against a non-production schedule before going
live.** Provided without warranty; you are responsible for validating behavior
against your own 25Live instance and Niagara station.

## Files

| File                         | Purpose                                             |
|------------------------------|-----------------------------------------------------|
| `main.py`                    | The sync script.                                    |
| `editor.py`                  | GUI to add/edit rooms & buildings (Tkinter).        |
| `Edit-Rooms.bat`             | Double-click launcher for the editor (Windows).     |
| `config.example.yaml`        | Settings template → copy to `config.yaml`.          |
| `space_mapping.example.yaml` | Room map template → copy to `space_mapping.yaml`.   |
| `requirements.txt`           | Python dependencies.                                |
| `Test.py`                    | Offline tests for the merge logic + editor I/O.     |
| `CONTRIBUTING.md`            | How to contribute.                                  |
| `CHANGELOG.md`               | Notable changes.                                    |
| `LICENSE`                    | GPL-3.0 license text.                               |
| `.github/workflows/ci.yml`   | CI: byte-compile + offline tests (Python 3.9, 3.12).|
