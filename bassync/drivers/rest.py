# 25Live -> BAS Schedule Sync — generic templated REST driver
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
A REST driver whose request shapes live in config.yaml instead of in Python.

Why this exists
---------------
For Automated Logic WebCTRL and Schneider EcoStruxure Building Operation the
recommended path is the `bacnet` driver: both are BTL-listed, both expose
standard Schedule objects, and BACnet's schedule contract is a published
standard rather than a per-version API surface.

But sometimes BACnet is not on the table — the schedule is not exported, the
network team will not open BACnet between VLANs, or you want the bookings to
appear as native objects in the vendor's own tool. Rather than ship guessed-at
endpoints for every vendor and version, this driver lets you describe your
site's actual API in YAML. Nothing here claims to know your endpoints; you
paste them in from your vendor's API documentation and `--validate` /
`--dry-run` prove them before anything goes live.

Configuration
-------------
    systems:
      ebo_west:
        driver: rest
        base_url: "https://ebo.example.edu"
        username: "svc-scheduler"
        verify_tls: true
        auth:
          mode: bearer            # basic | bearer | none
          login_path: "/api/rest/v1/login"
          login_payload: {"username": "{username}", "password": "{password}"}
          token_json_path: "token"      # dotted path to the token in the reply
          header: "Authorization"
          header_template: "Bearer {token}"
        health:
          method: GET
          path: "/api/rest/v1/status"
        exists:
          method: GET
          path: "/api/rest/v1/objects?path={target}"
        clear:
          method: DELETE
          path: "/api/rest/v1/schedules/{target}/exceptions"
        write:
          method: POST
          path: "/api/rest/v1/schedules/{target}/exceptions"
          # One request per occupancy window. Placeholders below.
          payload: {"start": "{start}", "end": "{end}", "value": true}

Placeholders available in paths and payloads
--------------------------------------------
    {target}        the space's target string (URL-encoded in paths)
    {target_raw}    the target, not encoded
    {username}      the system's username
    {password}      the system's password (login payload only)
    {token}         the bearer token from the login step
    {start} {end}   ISO-8601 window bounds, with timezone offset
    {start_local} {end_local}   ISO-8601 without offset (naive local time)
    {date}          the window's local start date, YYYY-MM-DD
    {start_time} {end_time}     local HH:MM:SS
    {value}         "true"
    {index} {count} 0-based window index and total, for APIs that want them

If `write.batch_payload` is set instead of `payload`, ONE request is sent with
`{windows}` replaced by a JSON array of all windows — for APIs that replace a
whole exception list at once. Prefer it when your API offers it: it makes the
write atomic, so a half-applied schedule is impossible.
"""

import json
import logging
from typing import Optional
from urllib.parse import quote

import requests

from .base import DriverError, ScheduleWriter
from ..httputil import mount_retries

HTTP_TIMEOUT = 30


class RestScheduleWriter(ScheduleWriter):
    """Drives a vendor REST API described entirely in config.yaml."""

    name = "rest"
    description = ("Generic REST driver — you supply the endpoints and payloads "
                   "in config.yaml (WebCTRL, EcoStruxure, in-house middleware).")

    def __init__(self, system_name: str, cfg: dict, tz, retry=None):
        super().__init__(system_name, cfg, tz, retry)
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        if not self.base_url:
            raise DriverError(
                f"System '{system_name}': the rest driver needs `base_url`.")
        self.username = cfg.get("username", "")
        self.password = cfg.get("password", "")
        self.auth_cfg = cfg.get("auth") or {"mode": "basic"}
        self.health_cfg = cfg.get("health") or {}
        self.exists_cfg = cfg.get("exists") or {}
        self.clear_cfg = cfg.get("clear") or {}
        self.write_cfg = cfg.get("write") or {}
        if not self.write_cfg.get("path"):
            raise DriverError(
                f"System '{system_name}': the rest driver needs a `write.path`. "
                "See bassync/drivers/rest.py for the full template reference.")

        self._token: Optional[str] = None
        self.session = requests.Session()
        self.session.verify = cfg.get("verify_tls", True)
        if not self.session.verify:
            logging.warning("System '%s': TLS verification is DISABLED.", system_name)
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        if self.auth_cfg.get("mode", "basic") == "basic":
            self.session.auth = (self.username, self.password)
        self.session.headers.setdefault("Accept", "application/json")
        # Retry reads and the idempotent clear; never the write.
        mount_retries(self.session, retry, allowed_methods=["GET", "DELETE"])

    def close(self) -> None:
        self.session.close()

    # ── auth ─────────────────────────────────────────────────────────────────

    def connect(self) -> None:
        if self.auth_cfg.get("mode") != "bearer" or self._token:
            return
        login_path = self.auth_cfg.get("login_path")
        if not login_path:
            raise DriverError(
                f"System '{self.system_name}': auth.mode is 'bearer' but no "
                "auth.login_path is configured.")
        payload = _fill(self.auth_cfg.get("login_payload") or {},
                        {"username": self.username, "password": self.password})
        try:
            r = self.session.post(self.base_url + login_path, json=payload,
                                  timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            raise DriverError(f"Login to {self.system_name} failed: {exc}") from exc
        if r.status_code >= 400:
            raise DriverError(
                f"Login to {self.system_name} failed: HTTP {r.status_code} "
                f"{r.text[:200]}")
        token = _dig(_json_or_text(r), self.auth_cfg.get("token_json_path", "token"))
        if not token:
            raise DriverError(
                f"Login to {self.system_name} returned no token at "
                f"'{self.auth_cfg.get('token_json_path', 'token')}'. Check "
                "auth.token_json_path against an actual login response.")
        self._token = str(token)
        header = self.auth_cfg.get("header", "Authorization")
        template = self.auth_cfg.get("header_template", "Bearer {token}")
        self.session.headers[header] = template.format(token=self._token)

    # ── requests ─────────────────────────────────────────────────────────────

    def _request(self, spec: dict, ctx: dict, body=None):
        method = (spec.get("method") or "GET").upper()
        url = self.base_url + _fill_str(spec["path"], ctx)
        payload = body if body is not None else spec.get("payload")
        if payload is not None:
            payload = _fill(payload, ctx)
        return self.session.request(method, url, json=payload,
                                    timeout=spec.get("timeout", HTTP_TIMEOUT))

    def _context(self, target: str, extra: Optional[dict] = None) -> dict:
        ctx = {
            "target": quote(target, safe=""),
            "target_raw": target,
            "username": self.username,
            "token": self._token or "",
        }
        if extra:
            ctx.update(extra)
        return ctx

    # ── pre-flight ───────────────────────────────────────────────────────────

    def health_check(self) -> tuple[bool, str]:
        if not self.health_cfg.get("path"):
            return True, "no health.path configured — not checked"
        try:
            self.connect()
            r = self._request(self.health_cfg, self._context(""))
        except DriverError as exc:
            return False, str(exc)
        except requests.RequestException as exc:
            return False, str(exc)
        ok = r.status_code < 400
        return ok, f"{self.base_url}{self.health_cfg['path']}: HTTP {r.status_code}"

    def target_exists(self, target: str) -> tuple[bool, str]:
        if not self.exists_cfg.get("path"):
            return True, "no exists.path configured — not checked"
        try:
            r = self._request(self.exists_cfg, self._context(target))
        except requests.RequestException as exc:
            return False, str(exc)
        return (r.status_code < 400), f"HTTP {r.status_code}"

    # ── writing ──────────────────────────────────────────────────────────────

    def write_schedule(self, target: str, windows: list) -> None:
        self.connect()
        if self.clear_cfg.get("path"):
            r = self._request(self.clear_cfg, self._context(target))
            if r.status_code >= 400 and r.status_code != 404:
                raise DriverError(
                    f"Failed clearing {target}: HTTP {r.status_code} {r.text[:200]}")

        if self.write_cfg.get("batch_payload") is not None:
            self._write_batch(target, windows)
        else:
            for index, win in enumerate(windows):
                ctx = self._context(target, _window_context(win, index, len(windows)))
                r = self._request(self.write_cfg, ctx)
                if r.status_code >= 400:
                    raise DriverError(
                        f"Write failed {target} window {index + 1}/{len(windows)}: "
                        f"HTTP {r.status_code} {r.text[:200]}")
        logging.info("Wrote %d window(s) to %s", len(windows), target)

    def _write_batch(self, target: str, windows: list) -> None:
        """Single request carrying every window — atomic where the API allows."""
        items = [_window_context(w, i, len(windows)) for i, w in enumerate(windows)]
        template = self.write_cfg["batch_payload"]
        rendered = json.dumps(template)
        # {windows} is substituted as raw JSON, so it must not be quoted in the
        # template the way scalar placeholders are.
        rendered = rendered.replace('"{windows}"', json.dumps(items))
        body = _fill(json.loads(rendered), self._context(target))
        r = self._request(self.write_cfg, self._context(target), body=body)
        if r.status_code >= 400:
            raise DriverError(
                f"Batch write failed {target}: HTTP {r.status_code} {r.text[:200]}")


def _window_context(win, index: int, count: int) -> dict:
    return {
        "start": win.start.isoformat(),
        "end": win.end.isoformat(),
        "start_local": win.start.replace(tzinfo=None).isoformat(),
        "end_local": win.end.replace(tzinfo=None).isoformat(),
        "date": win.start.date().isoformat(),
        "start_time": win.start.strftime("%H:%M:%S"),
        "end_time": win.end.strftime("%H:%M:%S"),
        "value": "true",
        "index": index,
        "count": count,
    }


def _fill_str(text: str, ctx: dict) -> str:
    """Substitute {placeholders}, leaving unknown ones untouched rather than
    raising — a stray brace in a vendor path should not kill the run."""
    out = text
    for key, value in ctx.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def _fill(obj, ctx: dict):
    """Recursively substitute placeholders through a JSON-ish structure."""
    if isinstance(obj, str):
        return _fill_str(obj, ctx)
    if isinstance(obj, dict):
        return {k: _fill(v, ctx) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, ctx) for v in obj]
    return obj


def _json_or_text(response):
    try:
        return response.json()
    except ValueError:
        return {"_text": response.text}


def _dig(data, dotted: str):
    """Follow a dotted path into nested dicts: 'result.token' -> data['result']['token']."""
    node = data
    for part in (dotted or "").split("."):
        if not part:
            continue
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node
