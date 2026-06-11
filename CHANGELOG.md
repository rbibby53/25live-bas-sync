# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
semantic versioning once it reaches a tagged release.

## [Unreleased]

### Added
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
