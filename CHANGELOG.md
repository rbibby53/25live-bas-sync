# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
semantic versioning from 1.0 onward.

## [1.0] — 2026-09-08

**First stable release.** The sync is no longer Niagara-specific: it drives any
BTL-listed BAS over standard BACnet, so one nightly run can cover a mixed campus
of Tridium Niagara, Automated Logic WebCTRL and Schneider EcoStruxure.

Everything below is relative to **V1.0 RC1**. Existing RC/beta deployments keep
working untouched — see *Upgrading* at the end.

### Added
- **Pluggable BAS drivers.** `python main.py --list-drivers`:
  - **`bacnet`** — standard ASHRAE 135 Schedule objects over BACnet/IP, via
    [BACpypes3]. Covers Tridium Niagara, Automated Logic WebCTRL and Schneider
    EcoStruxure Building Operation with one code path. Writes only
    `Exception_Schedule`, one special event **per calendar date** (which keeps
    the array inside the limits real controllers enforce), at `eventPriority`
    16 so hand-entered exceptions always win. Optional foreign-device/BBMD
    registration, address pinning, and write-verification read-back.
  - **`niagara`** — the previous REST writer, with `rest_base`,
    `special_event_type` and the ORD style now settable from `config.yaml`.
  - **`rest`** — a generic driver whose endpoints and payloads you describe in
    `config.yaml`, for a vendor API when BACnet isn't available.
  - **`preview`** — writes nothing; logs and optionally exports CSV, for staged
    commissioning one building at a time.
- **`systems:` config block** and per-space `system:` / `target:` keys, so a
  mixed campus syncs in one run. Rooms inherit their building's system
  (precedence room > building > `default_system`).
- **Per-floor corridors work across vendors.** The `floors:` section from RC1
  now carries `system:` too and inherits from its building, so a part-finished
  retrofit can leave a corridor on the supervisor while its rooms move to
  BACnet. Floor schedules also join the managed set, so a corridor with no
  bookings is actively cleared instead of holding last week's occupancy.
- **Editor Connection tab is driver-aware.** Pick a system from the dropdown and
  the fields follow its driver — NIC address and BBMD for BACnet, host/port/ORDs
  for Niagara — with **Add…** to create one. Systems you aren't looking at, and
  config sections the form never shows (`retry`, `alerts`, `safety`), survive a
  save untouched.
- **Docker image carries the BACnet driver** and documents the host-networking
  requirement: BACnet binds a real NIC and needs broadcast, neither of which
  survives the default bridge. `.env` is passed through wholesale so per-system
  `BAS_SYS_<NAME>_PASSWORD` variables reach the container.
- **Mass-clear safety rails** (`safety:`). A run refuses to write if 25Live
  returned fewer than `min_events` assignments, or if more than
  `max_cleared_fraction` of previously-occupied schedules would be emptied at
  once — the signature of an auth failure or an API change, which would
  otherwise stand the whole campus down under a successful-looking run.
  `--force` overrides; new exit code `7`.
- **`--system NAME`** to limit a run to one BAS, and **`--list-drivers`**,
  **`--version`**, **`--verbose`**.
- `collegenet.state_param_style` (`plus`/`comma`/`repeat`/`none`) — Series25
  instances differ in how they want the `state` filter encoded, and getting it
  wrong returns 200 with zero events.
- Room-map validation now reports **every** problem at once: missing targets,
  duplicate `space_id`s, unknown `system:` names, unparseable YAML.
- Editor: **System** dropdown fed from `config.yaml`, and *Test BAS
  connections* health-checks every configured system in one pass.
- **`--test-alert`** sends a test notification through every configured channel
  and reports each result — usable while `alerts.enabled` is still false, so
  the plumbing can be proven before anyone depends on it.
- SMTP alerting gained implicit TLS (`security: ssl`, port 465) alongside
  STARTTLS, explicit `Date`/`Message-ID` headers so alerts aren't quarantined,
  and separate reporting for connect / TLS / authentication / refused-recipient
  failures. A `username` with no `$BAS_SMTP_PASSWORD` is now caught before
  connecting instead of being rejected by the relay with an opaque 5xx.
- CI runs the suite both with and without BACpypes3 installed.

### Fixed
- **Building roll-ups with no bookings were never cleared.** The clear-loop only
  considered room targets, so a building whose rooms all lost their bookings
  kept conditioning on the previous run's schedule indefinitely.
- **Two rooms sharing one schedule silently lost one room's bookings.** The
  builder assigned rather than unioned, so whichever room was processed second
  erased the first — a real pattern for a divisible room split A/B in 25Live but
  served by a single AHU. Their windows are now unioned, and the loader warns
  when it sees a shared target.
- **A crash sent two alerts** — one "CRASHED", then one "FAILED" for the same
  event.
- **`--validate` passed on a broken 25Live endpoint.** Any response at all was
  treated as success, so a 404 from a wrong `instance`/`base_url`, or an SSO
  login page where Series25 XML was expected, both reported PASS.
- A 25Live response that isn't XML (a login page, a proxy error) now reports
  what it actually got instead of raising a bare `ParseError`.
- Duplicate `space_id` elements within one reservation no longer produce
  duplicate events.
- Reservations that end at or before they start are skipped with a warning
  rather than producing a negative-length window.
- `verify_tls: false` no longer reaches through the deprecated
  `requests.packages.urllib3` path to silence warnings.

### Changed
- Code reorganised into the importable **`bassync/`** package; `main.py` is now
  the CLI. `Test.py` remains the test entry point.
- `event_priority` is documented and configurable, replacing the previous
  hard-coded `BACNET_SCHEDULE_PRIORITY = 14` — which conflated the
  BACnetSpecialEvent precedence (which exception wins) with the commandable
  priority array (which is a different mechanism, and one this tool never
  touches).
- Per-system passwords via `BAS_SYS_<NAME>_PASSWORD`; a password left in
  `config.yaml` now warns.
- **Python 3.11 is now the minimum**, checked at startup with an actionable
  message rather than a traceback from inside a dependency. 3.9 and 3.10 are
  past end of life and no longer receive security fixes, which matters for a
  process holding service credentials on a controls network. CI covers 3.11
  through 3.14.
- Repository renamed to **25live-bas-sync**.

### Upgrading from V1.0 RC1 or a beta
No configuration changes are required:
- A top-level `niagara:` block is promoted to `systems:` automatically and made
  the default.
- `BAS_NIAGARA_PASSWORD` and `niagara_path:` are both still honored; the editor
  renames `niagara_path:` to `target:` the next time it saves.

Two behaviour changes to expect on the first run:
1. **Building roll-ups with no bookings are now cleared.** Some buildings will
   genuinely stand down after the upgrade — that is the fix working, not a
   regression.
2. **The mass-clear safety rail is on by default.** A first run during a break
   may stop and exit 7. Read the log, then re-run with `--force` if the drop is
   real.

Run `--validate` and `--dry-run` first, as always.

[BACpypes3]: https://github.com/JoelBender/BACpypes3

## [V1.0 RC1] and earlier pre-releases

### Added
- **Docker support.** A `Dockerfile`, `docker-compose.yml`, and entrypoint run
  the headless sync in a container. One-shot by default (pass `--validate` /
  `--dry-run` straight through; ideal for host cron or a k8s CronJob) with an
  optional built-in daily scheduler (`SYNC_AT=HH:MM`) so `docker compose up -d`
  is a self-contained nightly sync. Secrets stay in the environment / a
  gitignored `.env`; config is mounted at `/config`. New `BAS_SPACE_MAP` env var
  (parallel to `BAS_CONFIG` / `BAS_DEFAULTS`) points the sync at its room map.
- **Editor: Connection tab + auto theme.** The GUI now edits `config.yaml`
  (25Live, Niagara, timezone) directly, so it's a one-stop shop — one Save writes
  the room map, connection settings, and defaults together. Passwords stay in env
  vars and unexposed sections (`retry`, `alerts`) are preserved. The window also
  follows the OS light/dark appearance.
- **Editor: usability pass.** Per-table live search, click-to-sort columns,
  vertical scrollbars, a Duplicate action, a right-click context menu, keyboard
  shortcuts (Enter to edit, Del to delete), a status bar with live counts, and
  tab labels that show item counts.
- **Per-floor hallway HVAC.** Optional `floors:` section (building + level +
  niagara_path) and a per-room `floor:`. A room drives its floor's hallway
  schedule, and floors roll up into the building (room → floor → building). New
  **Floors** tab and per-room floor field in the editor.
- **`defaults.yaml`** — global scheduling defaults (run-up, run-down, merge-gap,
  lookahead) split into their own operator-tunable file, with a **Defaults** tab
  in the editor and a `--defaults` flag. Connection/auth settings stay in
  `config.yaml`.
- **Per-building run-up/run-down override.** A building may set
  `pre_condition_minutes`/`post_buffer_minutes` that its rooms inherit;
  precedence is room > building > global default.
- **Retry with backoff** for transient 25Live/Niagara errors (timeouts, 429,
  5xx). Applied to reads and the idempotent clear; POST writes are not
  auto-retried, to avoid duplicate special events. Configurable via `retry:`.
- **Failure alerts** — webhook (Slack/Teams/generic) and/or SMTP email on a
  failed (or optionally successful) sync run. Configurable via `alerts:`; the
  SMTP password comes from `BAS_SMTP_PASSWORD`.
- **`--validate`** pre-flight mode: checks config, 25Live auth, Niagara
  reachability, and that every schedule ORD exists — without writing.
- **`--discover`** read-only mode: lists 25Live spaces with upcoming events as a
  starter for `space_mapping.yaml` (`--discover-days` sets the window).
- **GUI tools** in `editor.py`: *Test 25Live connection*, *Test Niagara
  connection*, and *Preview (dry run)*.
- GitHub Actions **CI** (byte-compile + offline tests on Python 3.9 and 3.12),
  `CONTRIBUTING.md`, `CHANGELOG.md`, and the full GPL-3.0 `LICENSE`.

### Changed
- **Open-source ready / institution-agnostic.** All site-specific settings moved
  out of `main.py` into `config.yaml` (copied from `config.example.yaml`, merged
  over built-in defaults by `load_config()`; `--config`/`$BAS_CONFIG` select the
  path). The room map ships as `space_mapping.example.yaml`. Both real files are
  gitignored.

### Fixed
- **Critical:** add `tzdata` so `zoneinfo` works on Windows (was crashing at
  startup).
- **High:** robust 25Live pagination — stop on a short page instead of relying on
  a `total_count` element, which could silently drop events past the first page.
- Honor explicit `0` for pre/post/merge-gap minutes (was coerced to the default).
- URL-encode Niagara ORDs so names with spaces don't malform request URLs.
- Interpret naive 25Live timestamps in the configured timezone, not the server's.
- Suppress per-request `InsecureRequestWarning` when `verify_tls` is false.
