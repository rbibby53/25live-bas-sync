# 25Live -> BAS Schedule Sync — configuration
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Configuration loading and secret handling.

Three files, deliberately separated by who owns them:

    config.yaml     IT/controls: 25Live endpoint, BAS systems, credentials,
                    retries, alerting, safety limits.
    defaults.yaml   Operators: run-up / run-down / merge-gap / lookahead.
    space_mapping.yaml  The room -> schedule cross-reference.

Passwords never live in any of them; see load_credentials().
"""

import os
import copy
import logging
import re
from pathlib import Path
from typing import Optional

import yaml

# Sentinel left in the defaults so a forgotten password is obvious (and warned
# about) rather than silently sent as the literal string "CHANGE_ME".
PLACEHOLDER_PASSWORD = "CHANGE_ME"

# For CollegeNET-hosted 25Live the WebServices base URL is built from the
# instance name. Self-hosted sites set collegenet.base_url directly instead.
COLLEGENET_URL_TEMPLATE = "https://webservices.collegenet.com/r25ws/wrd/{instance}/run"

# Repo root — the directory holding main.py, config.yaml, etc.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


DEFAULTS = {
    # ── CollegeNET 25Live Series25 WebServices (XML API) ──
    "collegenet": {
        "instance": "",                       # your 25Live instance name
        "base_url": "",                       # blank -> built from `instance`
        "username": "",                       # a LOCAL 25Live account (not SSO)
        "password": PLACEHOLDER_PASSWORD,     # set env var BAS_25LIVE_PASSWORD
        "lookahead_days": 7,
        "include_states": [2],                # 2=confirmed (add 4 for tentative)
        # How the `state` filter is encoded in the query string. Series25
        # instances differ; --validate reports which one answered. See
        # bassync/collegenet.py.
        "state_param_style": "plus",          # plus | comma | repeat | none
        "default_pre_condition_minutes": 30,
        "default_post_buffer_minutes": 15,
        "merge_gap_minutes": 5,
    },

    # ── BAS systems this campus writes to ──
    # Each key is a system name that space_mapping.yaml can reference. The
    # `driver` picks the integration; every other key is passed to that driver.
    # A single-BAS site can leave this alone and use the legacy `niagara:`
    # block — migrate_legacy_systems() folds it in automatically.
    "systems": {},

    # System used by any building/room that doesn't name one. Blank means "the
    # only system defined", which keeps single-BAS configs free of boilerplate.
    "default_system": "",

    "timezone": "America/New_York",           # campus timezone (IANA name)

    # ── Resilience: retry transient 25Live/BAS errors (timeouts, 5xx) ──
    # Applied to reads and idempotent clears; writes are not auto-retried, so a
    # retry can never duplicate a schedule entry.
    "retry": {
        "attempts": 3,
        "backoff_seconds": 2.0,
    },

    # ── Safety rails ──
    # A sync that writes empty schedules everywhere turns off HVAC campus-wide.
    # That is exactly what a bad credential, a changed `state` parameter, or an
    # API version bump looks like, so the run refuses to do it unless the drop
    # is small or an operator passes --force. See bassync/safety.py.
    "safety": {
        "enabled": True,
        "min_events": 1,                # abort if 25Live returned fewer events
        "max_cleared_fraction": 0.34,   # abort if more than this share of
                                        #   previously-occupied schedules would
                                        #   be emptied in one run
        "state_file": "",               # blank -> logs/last_run.json
    },

    # ── Alerting: notify on a failed (or optionally successful) run ──
    "alerts": {
        "enabled": False,
        "notify_on_success": False,
        "webhook_url": "",                    # Slack/Teams/generic incoming webhook
        "email": {
            "enabled": False,
            "smtp_host": "",
            "smtp_port": 587,
            "use_tls": True,
            "username": "",                   # SMTP user (password: BAS_SMTP_PASSWORD)
            "from_addr": "",
            "to_addrs": [],
        },
    },

    "space_map_file": str(PROJECT_ROOT / "space_mapping.yaml"),
    "log_file": None,                         # None -> default_log_file()
}


def default_log_file() -> str:
    """Default log path: a logs/ folder next to the project (portable across
    OSes). setup_logging() falls back to stdout if it isn't writable, so this
    never blocks a run."""
    return str(PROJECT_ROOT / "logs" / "25live_sync.log")


def default_state_file() -> str:
    """Where the safety rail remembers the previous run's schedule sizes."""
    return str(PROJECT_ROOT / "logs" / "last_run.json")


def _deep_merge(base: dict, override: dict) -> None:
    """Recursively merge `override` into `base` in place (nested dicts merged,
    scalars/lists replaced)."""
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val


# Global scheduling defaults live in defaults.yaml — a small, GUI-editable file.
# These flat keys map onto the internal config so the rest of the code is
# unchanged.
DEFAULTS_FILE_MAP = {
    "pre_condition_minutes": "default_pre_condition_minutes",
    "post_buffer_minutes":   "default_post_buffer_minutes",
    "merge_gap_minutes":     "merge_gap_minutes",
    "lookahead_days":        "lookahead_days",
}


# Keys of the pre-2.0 top-level `niagara:` block. When a config has that and no
# `systems:`, it is folded into a system named "niagara" so existing
# deployments upgrade without touching their YAML.
LEGACY_NIAGARA_KEYS = {
    "host", "port", "https", "username", "password", "verify_tls",
    "schedule_base_path", "heartbeat_path", "rest_base",
}


def migrate_legacy_systems(cfg: dict) -> None:
    """
    Fold a pre-2.0 top-level `niagara:` block into `systems:` in place.

    Before 2.0 there was exactly one BAS and its settings lived under
    `niagara:`. Rather than force every existing site to rewrite config.yaml,
    that block is promoted to `systems: {niagara: {driver: niagara, ...}}` and
    becomes the default system. An explicit `systems:` block always wins; the
    two can coexist while a site migrates.
    """
    legacy = cfg.pop("niagara", None)
    if not legacy:
        return
    systems = cfg.setdefault("systems", {})
    if "niagara" in systems:
        # An explicit systems: entry of the same name takes precedence; the
        # legacy block only fills gaps it didn't specify.
        merged = {"driver": "niagara", **legacy}
        merged.update(systems["niagara"])
        systems["niagara"] = merged
    else:
        systems["niagara"] = {"driver": "niagara", **legacy}
    if not cfg.get("default_system"):
        cfg["default_system"] = "niagara"


def resolve_default_system(cfg: dict) -> str:
    """
    The system a building/room gets when it doesn't name one.

    An explicit `default_system` wins. Otherwise, if exactly one system is
    defined, that is unambiguous and becomes the default — so single-BAS sites
    never have to name it. With several systems and no default, spaces must say
    which one they mean; that is reported as a mapping error rather than
    guessed at, because guessing writes occupancy to the wrong building.
    """
    explicit = (cfg.get("default_system") or "").strip()
    if explicit:
        return explicit
    systems = cfg.get("systems") or {}
    if len(systems) == 1:
        return next(iter(systems))
    return ""


def load_config(path: str, defaults_path: Optional[str] = None) -> dict:
    """
    Build the runtime config: a deep copy of DEFAULTS, with config.yaml merged
    over it, then defaults.yaml applied on top of the scheduling knobs.

    A missing file just leaves the built-ins in place. Secrets are applied
    separately by load_credentials().
    """
    cfg = copy.deepcopy(DEFAULTS)
    p = Path(path)
    if p.exists():
        with open(p, "r", encoding="utf-8") as fh:
            user = yaml.safe_load(fh) or {}
        _deep_merge(cfg, user)

    if defaults_path and Path(defaults_path).exists():
        with open(defaults_path, "r", encoding="utf-8") as fh:
            gd = yaml.safe_load(fh) or {}
        for file_key, cfg_key in DEFAULTS_FILE_MAP.items():
            if gd.get(file_key) is not None:
                cfg["collegenet"][cfg_key] = gd[file_key]

    migrate_legacy_systems(cfg)

    cn = cfg["collegenet"]
    if not cn.get("base_url"):
        instance = (cn.get("instance") or "").strip()
        if instance:
            cn["base_url"] = COLLEGENET_URL_TEMPLATE.format(instance=instance)

    if not cfg["safety"].get("state_file"):
        cfg["safety"]["state_file"] = default_state_file()
    return cfg


def system_password_env(name: str) -> str:
    """
    Environment variable holding a BAS system's password.

        campus_bacnet  ->  BAS_SYS_CAMPUS_BACNET_PASSWORD

    Per-system rather than per-vendor, so a campus with two Niagara supervisors
    under different service accounts can keep them apart.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    return f"BAS_SYS_{slug}_PASSWORD"


# Legacy single-BAS password variables, still honored so an existing scheduled
# task keeps working after the upgrade. Keyed by driver name.
LEGACY_DRIVER_PASSWORD_ENV = {
    "niagara": "BAS_NIAGARA_PASSWORD",
    "webctrl": "BAS_WEBCTRL_PASSWORD",
    "ebo": "BAS_EBO_PASSWORD",
}


def load_credentials(cfg: dict) -> None:
    """
    Apply passwords from the environment and warn about anything still unset.

        BAS_25LIVE_PASSWORD                 25Live service account
        BAS_SYS_<SYSTEM>_PASSWORD           that BAS system's account
        BAS_NIAGARA_PASSWORD (and friends)  legacy per-driver fallback
        BAS_SMTP_PASSWORD                   alert email, read at send time

    A password written into config.yaml is honored but warned about — the file
    is gitignored, not encrypted, and tends to end up in a backup or a ticket.
    """
    cn_pw = os.environ.get("BAS_25LIVE_PASSWORD")
    if cn_pw:
        cfg["collegenet"]["password"] = cn_pw
    elif cfg["collegenet"].get("password") not in (None, "", PLACEHOLDER_PASSWORD):
        logging.warning("25Live password came from config.yaml — prefer the "
                        "BAS_25LIVE_PASSWORD environment variable.")
    if cfg["collegenet"].get("password") in (None, "", PLACEHOLDER_PASSWORD):
        logging.warning("25Live password is unset — set BAS_25LIVE_PASSWORD "
                        "before a live run.")

    for name, sys_cfg in (cfg.get("systems") or {}).items():
        if not isinstance(sys_cfg, dict):
            continue
        driver = sys_cfg.get("driver", "")
        env_var = system_password_env(name)
        value = os.environ.get(env_var)
        if not value:
            legacy_var = LEGACY_DRIVER_PASSWORD_ENV.get(driver)
            if legacy_var:
                value = os.environ.get(legacy_var)
        if value:
            sys_cfg["password"] = value
            continue
        existing = sys_cfg.get("password")
        if existing in (None, "", PLACEHOLDER_PASSWORD):
            # BACnet/IP has no credential of its own, so silence is correct
            # there; every other driver authenticates.
            if driver != "bacnet":
                logging.warning(
                    "System '%s' (%s) has no password — set %s before a live run.",
                    name, driver or "?", env_var)
        else:
            logging.warning(
                "System '%s' password came from config.yaml — prefer %s.",
                name, env_var)
