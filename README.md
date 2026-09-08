# 25Live → BAS Schedule Sync

Drive your building automation occupancy from your room bookings. This tool
pulls confirmed events from **CollegeNET 25Live** (Series25 WebServices) and
writes them into your building automation system as schedule exceptions, so HVAC
and lighting pre-condition for booked rooms and stand down when they're empty.

It speaks **standard BACnet**, so it works with **Tridium Niagara**, **Automated
Logic WebCTRL**, **Schneider EcoStruxure Building Operation**, and any other
BTL-listed controller — and one nightly run can drive all of them at once on a
mixed campus.

It's a single-run script meant to be scheduled nightly. It is read-only against
25Live and only ever writes occupancy schedules.

## Contents

- [Why BACnet](#why-bacnet)
- [Features](#features)
- [How it works](#how-it-works)
- [Install](#install)
- [Configure](#configure)
- [Running](#running)
- [Safety rails](#safety-rails)
- [BAS setup, by vendor](#bas-setup-by-vendor)
- [25Live setup](#25live-setup)
- [Upgrading from a pre-1.0 release](#upgrading-from-a-pre-10-release)
- [Tests](#tests)

## Why BACnet

Every one of the three major systems on a typical campus is BTL-listed and
exposes standard **Schedule objects** (ASHRAE 135 Object_Type 17). So rather
than chase three vendor APIs across their version histories, the default driver
writes the one thing all of them already understand: the `Exception_Schedule`
property.

| | BACnet | Vendor REST/SOAP |
|---|---|---|
| Contract | A published standard, stable for decades | Changes across versions and add-on packs |
| Coverage | Every BTL-listed system, one code path | One integration per vendor |
| Auth | None — the network *is* the security boundary | Real accounts and TLS |
| Visibility | Exceptions show up in the vendor tool | Native objects |

The tradeoff that matters is the third row: BACnet/IP has no authentication, so
this must run on a segmented controls network (and BACnet/SC is worth asking
your vendors about for new work). Where that isn't acceptable — or where a
station simply doesn't export its schedules — the native drivers are there.

**What it writes, and what it leaves alone.** Only `Exception_Schedule`. Your
weekly schedule, your schedule default, and the schedule's `Priority_For_Writing`
are untouched, so the building keeps its normal operating profile and this sync
is purely a booking overlay on top of it. Exceptions are written at
`eventPriority` 16 — the *lowest* — so any exception an operator adds by hand,
a holiday or a shutdown, overrides the bookings.

One special event is written **per calendar date**, with time/value pairs
alternating ON at each window start and OFF at each window end:

```
2026-06-10   09:00 → active, 11:30 → inactive, 13:00 → active, 17:00 → inactive
```

That matters on real hardware. A naive one-entry-per-booking encoding blows past
the `Exception_Schedule` array limits field controllers actually enforce (often
10–25 entries); grouping by date caps the array at one entry per day of
lookahead no matter how heavily booked the rooms are.

## Features

- Pulls the next *N* days of confirmed events for a configured set of spaces.
- **Mixed-vendor campus in one run** — every room and building names the BAS it
  lives on; rooms inherit their building's.
- Per-room **pre-conditioning** and **post-event** buffers, with building-wide
  and global defaults (precedence **room > building > global**).
- Merges overlapping/adjacent bookings into clean occupancy windows.
- **Building roll-up**: every room in a building automatically unions into that
  building's schedule — if *any* room is occupied, common areas run too.
- **Mass-clear safety rails** — refuses to stand the whole campus down because
  25Live had a bad day. See [Safety rails](#safety-rails).
- **Staged commissioning** — point a building at the `preview` driver and it
  logs (and CSV-exports) what it would do while the rest write for real.
- A **Tkinter GUI** (`editor.py`) so non-developers can manage the room map,
  with built-in *Test connections* and *Preview*.
- `--dry-run`, `--validate`, `--discover` modes; structured logging; a
  monitoring heartbeat; automatic retries; and failure alerts (webhook/email).

## How it works

```
25Live events ──► apply pre/post buffers ──► merge into occupancy windows
   ──► roll rooms up into building schedules ──► safety check
   ──► fan out to each BAS driver ──► heartbeat
```

The Python stays generic. Everything site-specific lives in three YAML files you
create from the provided examples.

## Install

```bash
git clone https://github.com/rbibby53/25live-bas-sync.git
cd 25live-bas-sync
pip install -r requirements.txt
```

If any system will use the BACnet driver, add:

```bash
pip install -r requirements-bacnet.txt
```

That pulls in [BACpypes3](https://github.com/JoelBender/BACpypes3). It's kept
separate and imported lazily, so a Niagara-only site never carries it.

**Requirements:** **Python 3.11 or newer** (3.9 and 3.10 are past end of life
and no longer receive security fixes; the script refuses to start on them). A
**local 25Live account** (not SSO) with read access and Series25 WebServices
enabled. Whatever your BAS side needs — see [BAS setup](#bas-setup-by-vendor).

Tested on 3.11, 3.12, 3.13 and 3.14.

> **Windows:** `requirements.txt` includes `tzdata` on purpose — Windows has no
> system timezone database, so without it `ZoneInfo(...)` raises
> `ZoneInfoNotFoundError` and the sync won't start. Install the dependencies for
> the **same** `python` that the scheduled task and `Edit-Rooms.bat` invoke —
> dependencies installed for one interpreter while the job runs another is the
> single most common deployment problem here.

## Configure

**1. Settings** — copy the example and edit it:

```bash
cp config.example.yaml config.yaml
```

`config.yaml` holds your 25Live instance, your BAS systems, accounts, timezone,
and safety limits. It's **gitignored**. Point elsewhere with `--config PATH` or
`$BAS_CONFIG`.

**2. Secrets** — passwords are **never** stored in files:

```bash
# Linux / macOS
export BAS_25LIVE_PASSWORD=...
export BAS_SYS_SUPERVISOR_PASSWORD=...     # one per system that needs one

# Windows (PowerShell)
$env:BAS_25LIVE_PASSWORD = "..."
$env:BAS_SYS_SUPERVISOR_PASSWORD = "..."
```

The variable name is `BAS_SYS_` + the system's name, uppercased with
non-alphanumerics as underscores. BACnet systems need no credential.

**3. Scheduling defaults** — `cp defaults.example.yaml defaults.yaml`. This is
the operator-tunable file (run-up / run-down / merge-gap / lookahead), kept
separate from the IT-managed connection settings. Edit it by hand or in the
editor's **Defaults** tab.

**4. Room map** — `cp space_mapping.example.yaml space_mapping.yaml`, then edit
by hand or with the GUI.

### The room map

Two sections: `buildings:` (each roll-up schedule, defined once) and `spaces:`
(the rooms). Each room names its `building:` by id, and **every room in a
building is automatically unioned into that building's schedule** — you never
repeat the building's address on a room, so you can't forget to wire one up.

Each entry also carries:

- **`system:`** — which BAS it lives on, a key from `systems:` in `config.yaml`.
  Rooms inherit their building's; buildings fall back to `default_system`. With
  one system defined you can omit it everywhere.
- **`target:`** — the schedule's address *within* that system:

  | driver | target | meaning |
  |---|---|---|
  | `bacnet` | `12001:5` | device instance 12001, Schedule object instance 5 |
  | `bacnet` | `12001:5@10.4.2.30` | …with the address pinned, skipping Who-Is |
  | `niagara` | `Bldg/Rm101_Occ` | ORD relative to `schedule_base_path` |
  | `niagara` | `slot:/Other/Sched` | an absolute ORD, used as-is |
  | `rest` | whatever your path template expects | |

`space_mapping.example.yaml` documents every field.

**Per-floor hallway HVAC.** For buildings controlled per floor, add a `floors:`
section (each floor = its `building`, a numeric `level`, and a hallway
`niagara_path`) and give each room a `floor:`. A room then drives its floor's
hallway schedule too, and floors roll up into the building (**room → floor →
building**): a floor hallway runs if any room on it is booked, and the building
runs if any floor is. The editor has a **Floors** tab and a per-room floor field.

### Editing rooms with the GUI

Run `python editor.py` (Windows users can double-click `Edit-Rooms.bat`):

- **Rooms** tab — Add/Edit/Delete rooms. **Building**, **Floor** and **System**
  are dropdowns, so joining a roll-up or moving a room to another BAS is a pick
  from a list rather than something to remember.
- **Buildings** tab — manage roll-up schedules; renaming a building id repoints
  the rooms and floors that referenced it.
- **Floors** tab — per-floor corridor schedules (building + floor # + target).
- **Connection** tab — edit `config.yaml` in the editor: the 25Live
  instance/account, your BAS systems, and the timezone. Pick a system from the
  dropdown and the fields follow its driver, so a BACnet system shows a NIC
  address and BBMD while a Niagara one shows host/port/ORDs. **Add…** creates a
  new system. Passwords are never stored here — they stay in the `BAS_*`
  environment variables — and sections you don't see (`retry`, `alerts`,
  `safety`) are preserved on save.
- **Defaults** tab — the global run-up/run-down/merge-gap/lookahead values
  (saved to `defaults.yaml`).
- **Tools** menu — *Test 25Live connection*, *Test BAS connections*
  (health-checks every configured system and reports them all, so "BACnet is
  fine, the supervisor is down" is the answer you get), and *Preview (dry run)*.

Every table has live search, click-to-sort headers, and a Duplicate action, and
the whole window **follows your OS light/dark setting** automatically. One Save
(Ctrl+S) writes the room map, connection settings, and defaults together — a
single place to configure everything.

It validates required fields and duplicate IDs, warns on missing-building
references, and keeps a `.bak` of the previous version on save.

> Saving rewrites the file and **does not preserve inline `#` comments** — it
> regenerates a clean header. To keep a per-room note through edits, use the
> **Note** field (stored as a `note:` key the sync ignores).

## Running

```bash
python main.py --list-drivers   # what BAS integrations are available
python main.py --validate       # pre-flight: config, auth, reachability, targets
python main.py --test-alert     # prove the failure alerts actually reach you
python main.py --discover       # list 25Live spaces with upcoming events
python main.py --dry-run        # fetch + build, print what WOULD be written
python main.py                  # live run
python main.py --system supervisor   # limit to one BAS (commissioning)
python main.py --force          # override the mass-clear safety check
```

- **`--validate`** is the deployment-confidence command: room map loads, 25Live
  authenticates, every BAS is reachable, and **every schedule target resolves** —
  without writing anything. Run it first.
- **`--dry-run`** contacts no BAS at all, and shows each driver's *actual*
  encoding — for BACnet, the per-date special events that would go on the wire.
- **`--discover`** (optionally `--discover-days N`, default 30) lists spaces
  with bookings, as a starter to paste into `space_mapping.yaml`.
- **`--test-alert`** sends a test notification through every configured channel
  and reports each one. Worth running the day you set alerting up: alerting is
  the one component that only runs when something has already gone wrong, which
  is a bad time to discover the relay rejects your `from` address.

### Alerting

Set `alerts.enabled: true` for a notification when a run fails (or, with
`notify_on_success`, when it succeeds). Two channels, either or both:

- **Webhook** — Slack, Teams, or anything accepting a JSON `{"text": ...}` POST.
- **SMTP email** — `starttls` (port 587, the default), `ssl` for implicit
  TLS/SMTPS (port 465), or `none` for an internal relay on a trusted network.
  Leave `username` blank for an open relay; if you set it, the password must be
  in `$BAS_SMTP_PASSWORD`. Connect, TLS, authentication, and per-recipient
  rejection are each reported separately, because they need four different
  fixes.

Alerting never changes a run's outcome — a dead mail relay won't turn a
successful sync into a failure. Pair it with the BAS heartbeat point so you also
catch the case where the job stops running entirely.

Exit codes (for monitoring): `0` ok · `1` unhandled error · `2` room map problem
· `3` BAS unreachable · `4` 25Live fetch failed · `5` write failures ·
`6` validation failed · `7` safety abort.

### Scheduling

Run it once per night, from anywhere that can reach both 25Live and your BAS.

**Windows Task Scheduler:** Program `python.exe`, Arguments
`<install-dir>\main.py`, Start in `<install-dir>`, trigger Daily at e.g. 02:00.
Set the `BAS_*` env vars for the account that runs the task.

**Linux/macOS cron:**
```
0 2 * * *  /usr/bin/python3 /opt/25live-bas-sync/main.py
```

Logs default to `logs/25live_sync.log` (override with `log_file`); if that
directory isn't writable the script logs to stdout instead.

## Run with Docker

A `Dockerfile` and `docker-compose.yml` are included for running the **sync**
headlessly in a container. (The Tkinter editor isn't containerized — edit your
YAML on a workstation, then mount it in.)

> **BACnet needs host networking.** The BACnet driver binds a real NIC address
> and relies on broadcast, neither of which survives Docker's default bridge.
> Run it with `network_mode: host` (already set in the compose file) and set
> `local_address` to the *host's* address. On Docker Desktop for Mac/Windows,
> host networking is limited — run the BACnet path directly on a Linux host or
> a VM on the controls network. The `niagara` and `rest` drivers are ordinary
> HTTP and work fine under bridge networking.

**1. Put your config where the container can mount it.** Create a `config/`
folder holding `config.yaml`, `defaults.yaml`, and `space_mapping.yaml` (copy the
`.example` files). **2. Put secrets in `.env`** (copy `.env.example`) — it's
gitignored and loaded automatically by compose:

```bash
mkdir -p config && cp config.example.yaml config/config.yaml   # + defaults / space_mapping
cp .env.example .env                                            # then fill in passwords
```

```bash
docker compose run --rm -e SYNC_AT= sync --validate   # one-shot pre-flight (no writes)
docker compose run --rm -e SYNC_AT= sync --dry-run    # one-shot, fetch + build only
docker compose up -d                                  # self-scheduling nightly sync
docker compose logs -f                                # watch it
```

Two ways to run it:

- **Self-scheduling (default).** `docker compose up -d` keeps the container up and
  runs the sync once a day at `SYNC_AT` (default `02:00`, in `TZ`). No host cron
  needed. Set `SYNC_ON_START=1` to also run once on startup.
- **One-shot.** Clearing `SYNC_AT` makes the container run once and exit with the
  sync's exit code — ideal for host cron, CI, or a **Kubernetes CronJob**:

  ```bash
  docker run --rm --network host \
    -e BAS_25LIVE_PASSWORD=... -e BAS_SYS_SUPERVISOR_PASSWORD=... \
    -e TZ=America/New_York \
    -v "$(pwd)/config:/config:ro" \
    25live-bas-sync --validate
  ```

Anything after the image name is passed straight to `main.py` (`--validate`,
`--dry-run`, `--discover`, `--test-alert`, …). Passwords come from the
environment only and are never baked into the image; logs go to
`docker compose logs` (and to the mounted `./logs` if present). The container
points at `/config/*.yaml` via the `BAS_CONFIG` / `BAS_DEFAULTS` /
`BAS_SPACE_MAP` env vars.

## Safety rails

The dangerous failure mode here is not a crash — it's a **successful-looking run
that writes empty schedules everywhere**. An expired 25Live service account, a
changed `state` query parameter, or a Series25 version bump all return HTTP 200
with zero events, and a naive sync would faithfully stand the entire campus
down. Nobody notices until Monday morning.

So each run compares itself to the last and refuses to write if:

- fewer than `safety.min_events` assignments came back from 25Live at all, or
- more than `safety.max_cleared_fraction` (default 34%) of the schedules that
  had bookings last time would be emptied now.

Both are about *change*, not absolute counts, so a genuinely quiet week still
has last week's state to compare against, and a first-ever run is allowed
through. The comparison state lives in `logs/last_run.json`.

`--force` overrides it — the right answer at semester break, when the drop is
real. A blocked run exits `7` and fires the configured alert.

This complements, rather than replaces, the other layers: `--validate` before
deploying, `--dry-run` before each change, the `preview` driver for staged
cut-over, and `verify_writes` reading BACnet writes back to confirm they took.

## BAS setup, by vendor

Whichever system you're on, this populates the *calendar*. You still wire the
schedule into your equipment once, in the vendor's own tool:

1. Create/identify a schedule object per room and per building roll-up.
2. Set its normal weekly value to **Unoccupied** — the sync only writes the
   booking exceptions on top.
3. Link its output into your occupancy logic (room → that zone; building →
   common AHUs and hallways).

Then run `python main.py --validate`.

### Tridium Niagara

**Either driver works.** Two real choices:

- **`niagara` (REST)** — bookings become native Niagara `SpecialEvent`s on a
  `BooleanSchedule`, visible and editable in Workbench. Best when the station
  owns the schedules. Confirm the REST contract for your version: `rest_base`
  (default `/rest/v1`), `special_event_type`, and the ORD style are all settable
  from `config.yaml`, so adapting is a YAML edit rather than a code change.
- **`bacnet`** — point it at the station's exported Schedule objects (Niagara's
  BACnet server exports a schedule via its BACnet Schedule Export descriptor).
  Best when you want one driver campus-wide.

Get the device instance from the station's BACnet device object; get schedule
object instances from the export descriptors.

### Automated Logic WebCTRL

**Use the `bacnet` driver.** ALC is BACnet-native — WebCTRL schedules *are*
BACnet Schedule objects in the controllers, so this is the supported, documented
integration path rather than a workaround.

Find the two numbers you need in WebCTRL: the controller's **device instance**
(under the module's BACnet properties) and the schedule's **object instance**.
`--validate` reads each schedule's `object-name` back, so a wrong number fails
pre-flight instead of writing somewhere unexpected.

> **Operational gotcha worth planning for:** a full database **download** from
> WebCTRL to a controller can overwrite exception schedules written externally.
> If your team downloads routinely, either re-run the sync after a download or
> schedule it to follow your maintenance window. Nothing detects this for you.

### Schneider EcoStruxure Building Operation

**Use the `bacnet` driver**, against the Schedule objects EBO exposes through
its BACnet Interface. Same two numbers: the device instance of the AS/AS-P, and
the schedule's object instance.

If your site would rather drive EBO's own REST API — to keep the bookings as
native EBO objects, or because BACnet isn't permitted between those VLANs — use
the **`rest`** driver and paste your endpoints into `config.yaml`. Nothing about
your API is assumed; `config.example.yaml` has a commented starting template and
`bassync/drivers/rest.py` documents every placeholder. Prove it with
`--validate` before going live.

### Networking for BACnet

- `local_address` must be a **real NIC address on this host**, with prefix
  length: `"10.4.1.55/24"`. The stack binds to it.
- If this host is **not on the controllers' subnet** — the usual case for a
  server writing to field panels — set `bbmd_address` to the BBMD for their
  network. The sync registers as a foreign device for the whole run.
- Pick a `device_id` that's free campus-wide and register it wherever your team
  tracks BACnet instance numbers.
- Pin addresses in targets (`12001:5@10.4.2.30`) on a large campus: it removes a
  broadcast round-trip per schedule and works where Who-Is doesn't cross subnets.

## 25Live setup

- Create a **local** service account (not SSO) with read access to the relevant
  events and locations, and enable Series25 WebServices for it.
- Set `collegenet.instance` (CollegeNET-hosted) or `collegenet.base_url`
  (self-hosted).
- The request filters confirmed events by the numeric `state` parameter.
  **Instances differ in how they want it encoded** — set
  `collegenet.state_param_style` to `plus` (default), `comma`, `repeat`, or
  `none` (filter client-side). Getting this wrong returns 200 with zero events,
  which is exactly what the safety rails are there to catch.
- Find a space's numeric `space_id` from its detail-page URL in 25Live, or run
  `--discover`.

## Upgrading from a pre-1.0 release

Existing beta/RC deployments keep working — the upgrade is additive:

- A pre-1.0 `config.yaml` with a top-level **`niagara:`** block is automatically
  promoted to `systems: {niagara: {driver: niagara, ...}}` and made the default.
  Nothing to edit.
- `BAS_NIAGARA_PASSWORD` is still honored alongside the new
  `BAS_SYS_<NAME>_PASSWORD` form.
- **`niagara_path:`** in `space_mapping.yaml` is still read. The editor renames
  it to `target:` the next time you save.
- New: `python main.py --list-drivers`, `--system`, `--force`, `--test-alert`;
  exit code `7`.

Two behaviour changes worth knowing before your first 1.0 run:

1. **Building roll-ups with no bookings are now cleared.** Previously only room
   schedules were, so a building whose rooms all went quiet kept conditioning on
   a stale schedule. Expect some buildings to genuinely stand down after the
   upgrade — that's the fix working.
2. **The safety rail is on by default.** If your first run happens during a
   break, it may block; check the log, then re-run with `--force`.

Run `--validate` and `--dry-run` after upgrading, as always.

## Tests

Offline tests (no 25Live, no BAS, no network) cover the merge/roll-up logic, the
loader and its inheritance rules, the BACnet exception-schedule encoding, the
safety rail, and the editor's YAML round-trip:

```bash
python Test.py
```

The BACnet encoding test skips itself when BACpypes3 isn't installed. CI runs
the suite on Python 3.11, 3.12, 3.13 and 3.14, each both with and without
BACpypes3 installed.

## Contributing

Issues and pull requests welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
Contributions that add BAS drivers or support more 25Live configurations are
especially valuable. A new driver is one file implementing
[`ScheduleWriter`](bassync/drivers/base.py) plus a line in the registry.

## License

Licensed under the **GNU General Public License v3.0** — see [LICENSE](LICENSE).
Copyright © 2026 Ryan Bibby and contributors.

## Disclaimer

This software writes occupancy schedules to building automation systems. **Test
thoroughly with `--validate` and `--dry-run`, and against a non-production
schedule, before going live.** Provided without warranty; you are responsible
for validating behavior against your own 25Live instance and your own BAS.

## Files

| File | Purpose |
|---|---|
| `main.py` | CLI entry point. |
| `bassync/` | The sync engine (importable, unit-tested). |
| `bassync/drivers/` | BAS integrations — `bacnet`, `niagara`, `rest`, `preview`. |
| `editor.py` | GUI to add/edit rooms, floors, buildings and connections (Tkinter). |
| `Edit-Rooms.bat` | Double-click launcher for the editor (Windows). |
| `config.example.yaml` | Connection/system template → copy to `config.yaml`. |
| `defaults.example.yaml` | Scheduling defaults → copy to `defaults.yaml`. |
| `space_mapping.example.yaml` | Room map template → copy to `space_mapping.yaml`. |
| `requirements.txt` | Core dependencies. |
| `requirements-bacnet.txt` | Extra dependency for the BACnet driver. |
| `Test.py` | Offline test suite. |
| `Dockerfile` | Container image for the headless sync. |
| `docker-compose.yml` | Compose service (scheduled or one-shot runs). |
| `docker-entrypoint.sh` | One-shot by default; optional built-in scheduler. |
| `.env.example` | Docker secrets template → copy to `.env`. |
| `CONTRIBUTING.md` · `CHANGELOG.md` · `LICENSE` | |
| `.github/workflows/ci.yml` | CI: offline tests on Python 3.11–3.14, with and without BACpypes3. |
