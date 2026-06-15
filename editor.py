#!/usr/bin/env python3
# 25Live -> Niagara Schedule Sync — Room/Building Mapping Editor
# Copyright (C) 2026 Ryan Bibby and contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed WITHOUT ANY WARRANTY; see the GNU General
# Public License for more details <https://www.gnu.org/licenses/>.
"""
Room / Building Mapping Editor  (GUI)
=====================================
A small desktop editor for `space_mapping.yaml` — the file that tells the
25Live → Niagara sync which 25Live spaces map to which Niagara schedules.

Use this instead of hand-editing YAML. It edits the SAME file `main.py` reads,
so the sync picks up changes on its next run. It does not touch 25Live or
Niagara.

Run:
    python editor.py                 # opens the mapping next to this script
    python editor.py path\\to\\map.yaml

Tkinter ships with Python, so there are no extra dependencies. On Windows the
python.org installer includes it by default.

The YAML read/write helpers (load_mapping / dump_mapping) are deliberately kept
free of any GUI code so they can be unit-tested headlessly (see Test.py).
"""


import copy
import os
import sys
from pathlib import Path

import yaml

DEFAULT_MAP_FILE = Path(__file__).parent / "space_mapping.yaml"

# Written to the top of the file on save so a hand-editor knows the format.
FILE_HEADER = """\
# 25Live → Niagara N4 schedule cross-reference.
#
# This file is managed by editor.py (the Room Mapping Editor) but is plain YAML
# and safe to hand-edit. See README.md for the full field reference.
#
#   buildings:  each building's roll-up schedule, defined once.
#   spaces:     the rooms; each room names the `building` it belongs to, and
#               every room in a building is automatically unioned into that
#               building's occupancy schedule (any room occupied -> building on).
"""

# Field order we emit so the file reads cleanly and diffs stay stable.
BUILDING_KEY_ORDER = ["id", "name", "niagara_path",
                      "pre_condition_minutes", "post_buffer_minutes", "space_id"]
ROOM_KEY_ORDER = ["space_id", "space_name", "building", "floor", "niagara_path",
                  "pre_condition_minutes", "post_buffer_minutes",
                  "merge_gap_minutes", "note"]
FLOOR_KEY_ORDER = ["building", "level", "niagara_path"]

DEFAULT_DEFAULTS_FILE = Path(__file__).parent / "defaults.yaml"

# Global scheduling defaults shown on the "Defaults" tab:
# (yaml key, label, built-in fallback).
DEFAULTS_FIELDS = [
    ("pre_condition_minutes", "Run-up (pre-condition) minutes", 30),
    ("post_buffer_minutes",   "Run-down (post-buffer) minutes", 15),
    ("merge_gap_minutes",     "Merge-gap minutes", 5),
    ("lookahead_days",        "Lookahead days", 7),
]

DEFAULTS_HEADER = """\
# Global scheduling defaults. Edit here or via the editor's "Defaults" tab.
# Rooms/buildings may override pre/post in space_mapping.yaml
# (precedence: room > building > these globals).
"""


# ─────────────────────────────────────────────────────────────────────────────
# YAML load / dump (no GUI — unit-testable)
# ─────────────────────────────────────────────────────────────────────────────

def load_mapping(path) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Read space_mapping.yaml into (buildings, floors, rooms) lists of plain dicts.
    A missing or empty file yields three empty lists (fresh start).
    """
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return [], [], []
    with open(p, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    buildings = list(data.get("buildings", []) or [])
    floors = list(data.get("floors", []) or [])
    rooms = list(data.get("spaces", []) or [])
    return buildings, floors, rooms


def _ordered(row: dict, key_order: list[str]) -> dict:
    """Return row's present keys in a stable order (others appended)."""
    out = {k: row[k] for k in key_order if k in row and row[k] not in (None, "")}
    for k, v in row.items():
        if k not in out and v not in (None, ""):
            out[k] = v
    return out


def dump_mapping(buildings: list[dict], floors: list[dict],
                 rooms: list[dict]) -> str:
    """Serialize (buildings, floors, rooms) back to YAML text with the header.
    The floors: section is only emitted when non-empty."""
    payload = {"buildings": [_ordered(b, BUILDING_KEY_ORDER) for b in buildings]}
    if floors:
        payload["floors"] = [_ordered(f, FLOOR_KEY_ORDER) for f in floors]
    payload["spaces"] = [_ordered(r, ROOM_KEY_ORDER) for r in rooms]
    body = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False,
                          allow_unicode=True)
    return FILE_HEADER + "\n" + body


def save_mapping(path, buildings: list[dict], floors: list[dict],
                 rooms: list[dict]) -> None:
    """Write the mapping, keeping a single .bak of the previous version."""
    p = Path(path)
    if p.exists():
        backup = p.with_suffix(p.suffix + ".bak")
        backup.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    p.write_text(dump_mapping(buildings, floors, rooms), encoding="utf-8")


def unknown_building_refs(buildings: list[dict], rooms: list[dict]) -> list[str]:
    """space_ids of rooms whose `building` doesn't match any defined building."""
    known = {str(b.get("id")) for b in buildings}
    bad = []
    for r in rooms:
        b = r.get("building")
        if b is not None and str(b) not in known:
            bad.append(str(r.get("space_id")))
    return bad


def load_defaults(path) -> dict:
    """Read defaults.yaml, filling any missing or invalid key with its built-in
    fallback so the form always shows real numbers. A malformed, unreadable, or
    non-mapping file degrades to the built-in defaults rather than stopping the
    editor from opening."""
    data = {}
    p = Path(path)
    if p.exists() and p.stat().st_size:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh)
            if isinstance(loaded, dict):
                data = loaded
        except (yaml.YAMLError, OSError):
            data = {}

    def _as_int(value, fallback):
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    return {key: _as_int(data.get(key, fb), fb)
            for key, _label, fb in DEFAULTS_FIELDS}


def dump_defaults(values: dict) -> str:
    """Serialize the global defaults back to YAML text with the header."""
    payload = {key: int(values.get(key, fb)) for key, _label, fb in DEFAULTS_FIELDS}
    body = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    return DEFAULTS_HEADER + "\n" + body


def save_defaults(path, values: dict) -> None:
    """Write defaults.yaml, keeping a single .bak of the previous version."""
    p = Path(path)
    if p.exists():
        backup = p.with_suffix(p.suffix + ".bak")
        backup.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    p.write_text(dump_defaults(values), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# config.yaml (connection settings) — load / apply / save (no GUI, testable)
#
# The Connection tab edits a curated set of connection fields. Passwords are
# deliberately NOT part of the form — they come from environment variables only
# (BAS_25LIVE_PASSWORD / BAS_NIAGARA_PASSWORD). Any sections already in
# config.yaml that the form doesn't expose (retry, alerts, …) are preserved
# untouched on save.
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG_FILE = Path(__file__).parent / "config.yaml"

CONFIG_HEADER = """\
# 25Live → Niagara connection settings. Managed by editor.py (Connection tab)
# but safe to hand-edit. Passwords are NOT stored here — set them as environment
# variables: BAS_25LIVE_PASSWORD and BAS_NIAGARA_PASSWORD. See
# config.example.yaml and README.md for the full reference.
"""

# (path-in-config, label, kind, hint).  kind: "text" | "int" | "bool".
CONFIG_SECTIONS = [
    ("25Live (CollegeNET)", [
        (("collegenet", "instance"), "Instance name", "text",
         "CollegeNET-hosted instance; the base URL is derived from it"),
        (("collegenet", "base_url"), "Base URL", "text",
         "self-hosted only; blank = derive from the instance"),
        (("collegenet", "username"), "Username", "text",
         "a LOCAL 25Live account (not SSO)"),
        (("collegenet", "include_states"), "Include states", "text",
         "other states already in the file are preserved"),
    ]),
    ("Niagara station", [
        (("niagara", "host"), "Host", "text", "station hostname or IP"),
        (("niagara", "port"), "Port", "int", "often 443 or 8443"),
        (("niagara", "https"), "Use HTTPS", "bool", ""),
        (("niagara", "username"), "Username", "text", ""),
        (("niagara", "verify_tls"), "Verify TLS", "combo",
         "true, false, or a path to a CA bundle"),
        (("niagara", "schedule_base_path"), "Schedule base ORD", "text",
         "default slot:/Schedules"),
        (("niagara", "heartbeat_path"), "Heartbeat ORD", "text",
         "optional; blank to disable"),
    ]),
    ("General", [
        (("timezone",), "Timezone", "combo", "IANA name, e.g. America/New_York"),
        (("log_file",), "Log file", "text", "blank = logs/25live_sync.log"),
    ]),
]

# 25Live event states this project documents (numeric -> label), offered as
# checkboxes on the Connection tab. Any other states already in config.yaml are
# preserved untouched.
INCLUDE_STATE_LABELS = [(2, "Confirmed"), (4, "Tentative")]


def _config_fields():
    """Flatten CONFIG_SECTIONS to a list of (path, label, kind, hint)."""
    return [f for _section, fields in CONFIG_SECTIONS for f in fields]


def _config_default_bool(path_t) -> bool:
    """Default for a checkbox field when config.yaml doesn't set it."""
    return path_t == ("niagara", "https")        # HTTPS on by default


def _dig(node, path):
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _set_path(node, path, value):
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[path[-1]] = value


def _del_path(node, path):
    stack = []
    cur = node
    for key in path[:-1]:
        if not isinstance(cur, dict) or key not in cur:
            return
        stack.append((cur, key))
        cur = cur[key]
    if isinstance(cur, dict):
        cur.pop(path[-1], None)
    for parent, key in reversed(stack):        # prune now-empty parents
        if isinstance(parent.get(key), dict) and not parent[key]:
            del parent[key]


def read_config_raw(path) -> dict:
    """Load config.yaml as a plain dict (or {} if missing/empty/malformed), so
    sections the form doesn't expose survive a round-trip through the editor."""
    p = Path(path)
    if not (p.exists() and p.stat().st_size):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (yaml.YAMLError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config_form(path) -> dict:
    """Read config.yaml into a flat {dotted_path: value} for the Connection tab.
    bool fields come back as bools; everything else as strings ('' if unset)."""
    raw = read_config_raw(path)
    out = {}
    for path_t, _label, kind, _hint in _config_fields():
        val = _dig(raw, path_t)
        fid = ".".join(path_t)
        if kind == "bool":
            out[fid] = bool(val) if val is not None else _config_default_bool(path_t)
        elif isinstance(val, bool):
            out[fid] = "true" if val else "false"     # e.g. verify_tls: false
        elif isinstance(val, list):
            out[fid] = ", ".join(str(x) for x in val)
        else:
            out[fid] = "" if val is None else str(val)
    return out


def _coerce_config_value(s: str, kind: str, path_t):
    if path_t == ("collegenet", "include_states"):
        return [int(x) for x in s.replace(",", " ").split()]
    if path_t == ("niagara", "verify_tls"):
        low = s.lower()
        if low in ("true", "yes", "1"):
            return True
        if low in ("false", "no", "0"):
            return False
        return s                               # otherwise a CA-bundle path
    if kind == "int":
        return int(s)
    return s


def apply_config_form(raw: dict, form: dict) -> dict:
    """Return a deep copy of `raw` with the Connection-tab fields applied. Blank
    text/int fields remove the key (so main.py's defaults apply); bool fields are
    always written. Raises ValueError on a non-numeric int / state value."""
    out = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    for path_t, label, kind, _hint in _config_fields():
        fid = ".".join(path_t)
        raw_val = form.get(fid)
        if kind == "bool":
            _set_path(out, path_t, bool(raw_val))
            continue
        s = ("" if raw_val is None else str(raw_val)).strip()
        if s == "":
            _del_path(out, path_t)
            continue
        try:
            _set_path(out, path_t, _coerce_config_value(s, kind, path_t))
        except ValueError:
            raise ValueError(f"'{label}' — could not read {s!r}")
    return out


def save_config(path, config: dict) -> None:
    """Write config.yaml, keeping a single .bak of the previous version."""
    p = Path(path)
    if p.exists():
        backup = p.with_suffix(p.suffix + ".bak")
        backup.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    body = yaml.safe_dump(config, sort_keys=False, default_flow_style=False,
                          allow_unicode=True)
    p.write_text(CONFIG_HEADER + "\n" + body, encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Theme (auto light/dark to match the OS)
# ─────────────────────────────────────────────────────────────────────────────

LIGHT_PALETTE = {
    "bg": "#ffffff", "surface": "#f4f6f9", "stripe": "#f4f6f9",
    "header": "#e9edf2", "text": "#1b1f24", "muted": "#6b7280",
    "border": "#d7dbe0", "select": "#cfe2ff", "select_fg": "#0b1f44",
    "accent": "#2563eb", "accent_hi": "#1d4ed8", "accent_fg": "#ffffff",
    "entry_bg": "#ffffff",
}
DARK_PALETTE = {
    "bg": "#1e1f22", "surface": "#26282c", "stripe": "#27292e",
    "header": "#2f3136", "text": "#e6e7e9", "muted": "#9aa0a6",
    "border": "#3a3d42", "select": "#2f4a73", "select_fg": "#eaf1ff",
    "accent": "#3b82f6", "accent_hi": "#2563eb", "accent_fg": "#ffffff",
    "entry_bg": "#26282c",
}


def _detect_dark_mode() -> bool:
    """Best-effort OS dark-mode detection (macOS / Windows / GNOME). Returns
    False — light — on any platform we can't read or on any error."""
    import subprocess
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                               capture_output=True, text=True, timeout=2)
            return r.returncode == 0 and "dark" in r.stdout.lower()
        if sys.platform.startswith("win"):
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            ) as key:
                return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
        r = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, timeout=2)
        return "dark" in r.stdout.lower()
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# GUI  (imported lazily so the helpers above stay usable without a display)
# ─────────────────────────────────────────────────────────────────────────────

def run_gui(map_path: Path) -> int:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog, font as tkfont

    # ── Theme. The palette is chosen once from the OS appearance, then applied
    #    to every widget class via ttk's "clam" engine (the native aqua/win
    #    themes ignore most colour options, so we always render with clam). ──
    PAD = 10
    PALETTE = DARK_PALETTE if _detect_dark_mode() else LIGHT_PALETTE

    def _apply_style(root: tk.Misc) -> None:
        p = PALETTE
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        base = tkfont.nametofont("TkDefaultFont")
        base.configure(size=max(int(base.cget("size") or 10), 10))
        root.option_add("*Font", base)
        heading_font = (base.cget("family"), base.cget("size"), "bold")

        root.configure(background=p["bg"])
        style.configure(".", background=p["bg"], foreground=p["text"],
                        fieldbackground=p["entry_bg"], bordercolor=p["border"],
                        lightcolor=p["surface"], darkcolor=p["surface"],
                        troughcolor=p["surface"], insertcolor=p["text"])
        style.configure("TFrame", background=p["bg"])
        style.configure("TLabel", background=p["bg"], foreground=p["text"])
        style.configure("Hint.TLabel", background=p["bg"], foreground=p["muted"])
        style.configure("Section.TLabel", background=p["bg"], foreground=p["text"],
                        font=heading_font)
        style.configure("Status.TLabel", background=p["surface"],
                        foreground=p["muted"], padding=(10, 5))
        style.configure("TButton", padding=(11, 5), background=p["surface"],
                        foreground=p["text"], bordercolor=p["border"])
        style.map("TButton", background=[("active", p["header"]),
                                         ("pressed", p["header"])])
        style.configure("Toolbutton", padding=(11, 5), background=p["surface"],
                        foreground=p["text"])
        style.map("Toolbutton", background=[("active", p["header"])])
        style.configure("Accent.TButton", padding=(14, 5), background=p["accent"],
                        foreground=p["accent_fg"], bordercolor=p["accent"])
        style.map("Accent.TButton", background=[("active", p["accent_hi"]),
                                                ("disabled", p["muted"])])
        style.configure("TEntry", fieldbackground=p["entry_bg"],
                        foreground=p["text"], insertcolor=p["text"],
                        bordercolor=p["border"])
        style.map("TEntry", fieldbackground=[("readonly", p["surface"])])
        style.configure("TCombobox", fieldbackground=p["entry_bg"],
                        foreground=p["text"], background=p["surface"],
                        bordercolor=p["border"], arrowcolor=p["text"])
        style.map("TCombobox", fieldbackground=[("readonly", p["entry_bg"])],
                  foreground=[("readonly", p["text"])])
        style.configure("TCheckbutton", background=p["bg"], foreground=p["text"])
        style.map("TCheckbutton", background=[("active", p["bg"])])
        style.configure("TNotebook", background=p["bg"], bordercolor=p["border"])
        style.configure("TNotebook.Tab", padding=(14, 7), background=p["surface"],
                        foreground=p["muted"])
        style.map("TNotebook.Tab", background=[("selected", p["bg"])],
                  foreground=[("selected", p["text"])])
        style.configure("TSeparator", background=p["border"])
        style.configure("Vertical.TScrollbar", background=p["surface"],
                        troughcolor=p["bg"], bordercolor=p["border"],
                        arrowcolor=p["muted"])
        style.configure("Treeview", rowheight=27, background=p["bg"],
                        fieldbackground=p["bg"], foreground=p["text"],
                        borderwidth=0)
        style.map("Treeview", background=[("selected", p["select"])],
                  foreground=[("selected", p["select_fg"])])
        style.configure("Treeview.Heading", font=heading_font,
                        background=p["header"], foreground=p["text"],
                        relief="flat", padding=(6, 5))
        style.map("Treeview.Heading", background=[("active", p["header"])])

    # ── A generic modal form built from a field spec. Adding a field is one
    #    line in the spec list, which keeps Room/Building forms easy to extend.
    class FormDialog(tk.Toplevel):
        # field spec: (key, label, kind, options)
        #   kind: "text" | "int" | "combo";  options: list for "combo"
        def __init__(self, parent, title, fields, values, validate):
            super().__init__(parent)
            self.title(title)
            self.configure(background=PALETTE["bg"])
            self.resizable(False, False)
            self.transient(parent)
            self.grab_set()
            self._fields = fields
            self._validate = validate
            self._vars = {}
            self.result = None

            for i, (key, label, kind, options) in enumerate(fields):
                ttk.Label(self, text=label).grid(
                    row=i, column=0, sticky="e", padx=8, pady=4)
                var = tk.StringVar(value="" if values.get(key) is None
                                   else str(values.get(key)))
                self._vars[key] = var
                if kind == "combo":
                    w = ttk.Combobox(self, textvariable=var, values=options,
                                     state="readonly", width=34)
                elif options:
                    # A text/int field with suggestions -> editable dropdown:
                    # pick a known value or type your own (still validated).
                    w = ttk.Combobox(self, textvariable=var, values=options,
                                     width=34)
                else:
                    w = ttk.Entry(self, textvariable=var, width=36)
                w.grid(row=i, column=1, sticky="w", padx=8, pady=4)
                if i == 0:
                    w.focus_set()

            btns = ttk.Frame(self)
            btns.grid(row=len(fields), column=0, columnspan=2, pady=(12, 12))
            ttk.Button(btns, text="OK", style="Accent.TButton",
                       command=self._ok).pack(side="left", padx=5)
            ttk.Button(btns, text="Cancel", command=self.destroy).pack(
                side="left", padx=5)
            self.bind("<Return>", lambda e: self._ok())
            self.bind("<Escape>", lambda e: self.destroy())
            self.update_idletasks()
            self._center_over(parent)

        def _center_over(self, parent):
            try:
                px, py = parent.winfo_rootx(), parent.winfo_rooty()
                pw, ph = parent.winfo_width(), parent.winfo_height()
                w, h = self.winfo_width(), self.winfo_height()
                self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 3}")
            except tk.TclError:
                pass

        def _collect(self) -> dict:
            out = {}
            for key, _label, kind, _opts in self._fields:
                raw = self._vars[key].get().strip()
                if raw == "":
                    continue
                if kind == "int":
                    out[key] = int(raw)   # validated already
                else:
                    out[key] = raw
            return out

        def _ok(self):
            # Type-check int fields before anything else.
            for key, label, kind, _opts in self._fields:
                raw = self._vars[key].get().strip()
                if kind == "int" and raw != "":
                    try:
                        int(raw)
                    except ValueError:
                        messagebox.showerror(
                            "Invalid value",
                            f"'{label}' must be a whole number.", parent=self)
                        return
            values = self._collect()
            err = self._validate(values)
            if err:
                messagebox.showerror("Invalid entry", err, parent=self)
                return
            self.result = values
            self.destroy()

    NONE_LABEL = "(none)"

    class EditorApp(tk.Tk):
        def __init__(self, path: Path):
            super().__init__()
            self.path = path
            self.buildings: list[dict] = []
            self.floors: list[dict] = []
            self.rooms: list[dict] = []
            self.defaults_path = DEFAULT_DEFAULTS_FILE
            self.defaults = load_defaults(self.defaults_path)
            self.config_path = Path(os.environ.get("BAS_CONFIG")
                                    or DEFAULT_CONFIG_FILE)
            self.dirty = False
            self._sort_state: dict = {}     # (table_key, column) -> ascending?

            _apply_style(self)
            self.title("25Live → Niagara — Room Mapping Editor")
            self.geometry("960x600")
            self.minsize(780, 480)
            self._build_menu()
            self._build_statusbar()
            self._build_tabs()
            self._load(path)
            self._center_on_screen()

        def _center_on_screen(self):
            self.update_idletasks()
            w, h = self.winfo_width(), self.winfo_height()
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            self.geometry(f"+{max((sw - w) // 3, 0)}+{max((sh - h) // 4, 0)}")

        # ── data ──
        def _load(self, path: Path):
            try:
                self.buildings, self.floors, self.rooms = load_mapping(path)
            except Exception as exc:
                messagebox.showerror("Load failed", f"Could not read\n{path}\n\n{exc}")
                self.buildings, self.floors, self.rooms = [], [], []
            self.path = path
            self.dirty = False
            self._refresh_all()
            self._update_title()

        def _save(self) -> bool:
            # Collect + validate the Defaults tab first (whole-number fields).
            new_defaults = {}
            for key, label, fb in DEFAULTS_FIELDS:
                raw = self._defaults_vars[key].get().strip()
                try:
                    new_defaults[key] = int(raw) if raw != "" else fb
                except ValueError:
                    messagebox.showerror(
                        "Defaults", f"'{label}' must be a whole number.")
                    return False

            # Collect + validate the Connection tab. Re-read the file so any
            # sections we don't expose (retry, alerts, …) are preserved.
            config_form = {fid: var.get()
                           for fid, (var, _kind, _path) in self._config_vars.items()}
            states = sorted({s for s, v in self._include_vars.items() if v.get()}
                            | set(self._include_extra))
            config_form["collegenet.include_states"] = ", ".join(str(s) for s in states)
            try:
                new_config = apply_config_form(read_config_raw(self.config_path),
                                               config_form)
            except ValueError as exc:
                messagebox.showerror("Connection", f"Invalid setting — {exc}")
                return False

            bad = unknown_building_refs(self.buildings, self.rooms)
            if bad and not messagebox.askyesno(
                    "Unknown building",
                    "These rooms reference a building that doesn't exist and "
                    f"won't roll up:\n\n  {', '.join(bad)}\n\nSave anyway?"):
                return False
            try:
                save_mapping(self.path, self.buildings, self.floors, self.rooms)
                save_defaults(self.defaults_path, new_defaults)
                save_config(self.config_path, new_config)
            except Exception as exc:
                messagebox.showerror("Save failed", str(exc))
                return False
            self.defaults = new_defaults
            self.dirty = False
            self._update_title()
            self._update_status()
            messagebox.showinfo(
                "Saved", f"Saved {len(self.rooms)} rooms, {len(self.buildings)} "
                f"buildings, {len(self.floors)} floors, connection settings, and "
                "global defaults.")
            return True

        def _mark_dirty(self):
            self.dirty = True
            self._update_title()
            self._update_status()

        def _update_title(self):
            star = "*" if self.dirty else ""
            self.title(f"{star}25Live → Niagara — Room Mapping Editor  [{self.path.name}]")

        # ── menu ──
        def _menu(self, parent):
            """A tk.Menu themed to the active palette (matters on Windows/Linux;
            macOS uses the native menu bar and ignores these colours)."""
            p = PALETTE
            return tk.Menu(parent, tearoff=0, background=p["surface"],
                           foreground=p["text"], activebackground=p["accent"],
                           activeforeground=p["accent_fg"],
                           borderwidth=0)

        def _build_menu(self):
            from tkinter import Menu
            bar = Menu(self)
            filem = self._menu(bar)
            filem.add_command(label="Open…", command=self._on_open)
            filem.add_command(label="Save", command=self._save, accelerator="Ctrl+S")
            filem.add_separator()
            filem.add_command(label="Exit", command=self._on_close)
            bar.add_cascade(label="File", menu=filem)

            toolm = self._menu(bar)
            toolm.add_command(label="Test 25Live connection",
                              command=self._test_25live)
            toolm.add_command(label="Test Niagara connection",
                              command=self._test_niagara)
            toolm.add_separator()
            toolm.add_command(label="Preview (dry run)…", command=self._preview)
            bar.add_cascade(label="Tools", menu=toolm)

            self.config(menu=bar)
            self.bind_all("<Control-s>", lambda e: self._save())
            self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── tools (use the sync engine in main.py; need config.yaml) ──
        def _runtime_config(self):
            import main
            cfg = main.load_config(str(self.config_path))
            main.load_credentials(cfg)
            return main, cfg

        def _test_25live(self):
            from zoneinfo import ZoneInfo
            try:
                main, cfg = self._runtime_config()
                if not cfg["collegenet"].get("base_url"):
                    messagebox.showwarning(
                        "Test 25Live",
                        "No 25Live instance/base_url in config.yaml.\n"
                        "Copy config.example.yaml to config.yaml and set it.")
                    return
                cn = main.CollegeNetClient(cfg["collegenet"],
                                           ZoneInfo(cfg["timezone"]), cfg.get("retry"))
                ok, detail = cn.check_connection()
                (messagebox.showinfo if ok else messagebox.showerror)(
                    "Test 25Live", f"{'Connected' if ok else 'FAILED'}\n\n{detail}")
            except Exception as exc:
                messagebox.showerror("Test 25Live", f"Error: {exc}")

        def _test_niagara(self):
            from zoneinfo import ZoneInfo
            try:
                main, cfg = self._runtime_config()
                n4 = main.NiagaraClient(cfg["niagara"],
                                        ZoneInfo(cfg["timezone"]), cfg.get("retry"))
                ok = n4.health_check()
                (messagebox.showinfo if ok else messagebox.showerror)(
                    "Test Niagara",
                    "Reachable (HTTP 200 from /about)." if ok
                    else "Unreachable — check niagara host/port/TLS in config.yaml.")
            except Exception as exc:
                messagebox.showerror("Test Niagara", f"Error: {exc}")

        def _preview(self):
            import io
            import logging as _logging
            if self.dirty and messagebox.askyesno(
                    "Preview",
                    "Preview uses the SAVED file, but you have unsaved changes.\n"
                    "Save first?"):
                if not self._save():
                    return
            try:
                main, cfg = self._runtime_config()
                cfg["space_map_file"] = str(self.path)
                buf = io.StringIO()
                handler = _logging.StreamHandler(buf)
                handler.setFormatter(_logging.Formatter("%(levelname)-7s %(message)s"))
                root = _logging.getLogger()
                root.addHandler(handler)
                prev = root.level
                root.setLevel(_logging.INFO)
                try:
                    main.run_sync(cfg, dry_run=True)
                finally:
                    root.removeHandler(handler)
                    root.setLevel(prev)
                self._show_text("Preview (dry run)", buf.getvalue() or "(no output)")
            except Exception as exc:
                messagebox.showerror("Preview", f"Error: {exc}")

        def _show_text(self, title: str, text: str):
            p = PALETTE
            win = tk.Toplevel(self)
            win.title(title)
            win.geometry("720x460")
            win.transient(self)
            win.configure(background=p["bg"])
            txt = tk.Text(win, wrap="none", background=p["entry_bg"],
                          foreground=p["text"], insertbackground=p["text"],
                          borderwidth=0, highlightthickness=0,
                          padx=10, pady=8)
            txt.insert("1.0", text)
            txt.config(state="disabled")
            txt.pack(fill="both", expand=True)
            ttk.Button(win, text="Close", command=win.destroy).pack(pady=6)

        def _on_open(self):
            if not self._confirm_discard():
                return
            chosen = filedialog.askopenfilename(
                title="Open mapping", filetypes=[("YAML", "*.yaml *.yml"), ("All", "*.*")])
            if chosen:
                self._load(Path(chosen))

        def _on_close(self):
            if self._confirm_discard():
                self.destroy()

        def _confirm_discard(self) -> bool:
            if not self.dirty:
                return True
            ans = messagebox.askyesnocancel(
                "Unsaved changes", "Save changes before continuing?")
            if ans is None:
                return False
            if ans:
                return self._save()
            return True

        # ── status bar ──
        def _build_statusbar(self):
            self._status = tk.StringVar(value="")
            bar = ttk.Frame(self)
            bar.pack(fill="x", side="bottom")
            ttk.Separator(bar, orient="horizontal").pack(fill="x")
            ttk.Label(bar, textvariable=self._status,
                      style="Status.TLabel").pack(side="left")

        def _update_status(self):
            if not hasattr(self, "_status"):
                return
            unsaved = "   •   unsaved changes" if self.dirty else ""
            self._status.set(
                f"{len(self.rooms)} rooms    ·    {len(self.buildings)} buildings"
                f"    ·    {len(self.floors)} floors    ·    {self.path.name}{unsaved}")

        # ── tabs / tables ──
        def _build_tabs(self):
            self.nb = ttk.Notebook(self)
            self.nb.pack(fill="both", expand=True, padx=PAD, pady=(PAD, 4))
            self._tabs: dict = {}   # name -> tab frame (for live count labels)

            self.rooms_tree, self._room_filter = self._make_table(
                self._new_tab("Rooms"), "rooms",
                columns=[("space_id", "Space ID", 80),
                         ("space_name", "Name", 190),
                         ("building", "Building", 130),
                         ("floor", "Floor", 55),
                         ("niagara_path", "Niagara Path", 220),
                         ("pre_condition_minutes", "Pre", 50),
                         ("post_buffer_minutes", "Post", 50),
                         ("merge_gap_minutes", "Gap", 50)],
                on_add=self._room_add, on_edit=self._room_edit,
                on_delete=self._room_delete, on_duplicate=self._room_duplicate,
                refresh=self._refresh_rooms)

            self.bld_tree, self._bld_filter = self._make_table(
                self._new_tab("Buildings"), "buildings",
                columns=[("id", "ID", 150),
                         ("name", "Name", 200),
                         ("niagara_path", "Niagara Path", 230),
                         ("pre_condition_minutes", "Pre", 50),
                         ("post_buffer_minutes", "Post", 50),
                         ("space_id", "Bookable ID", 110)],
                on_add=self._bld_add, on_edit=self._bld_edit,
                on_delete=self._bld_delete, on_duplicate=self._bld_duplicate,
                refresh=self._refresh_buildings)

            self.flr_tree, self._flr_filter = self._make_table(
                self._new_tab("Floors"), "floors",
                columns=[("building", "Building", 200),
                         ("level", "Floor #", 80),
                         ("niagara_path", "Hallway Niagara Path", 340)],
                on_add=self._floor_add, on_edit=self._floor_edit,
                on_delete=self._floor_delete, on_duplicate=self._floor_duplicate,
                refresh=self._refresh_floors)

            self._build_connection_tab()
            self._build_defaults_tab()

        def _new_tab(self, name: str) -> "ttk.Frame":
            tab = ttk.Frame(self.nb)
            self.nb.add(tab, text=name)
            self._tabs[name] = tab
            return tab

        def _set_tab_count(self, name: str, shown: int, total: int):
            tab = self._tabs.get(name)
            if tab is None:
                return
            label = f"{name} ({total})" if shown == total else f"{name} ({shown}/{total})"
            self.nb.tab(tab, text=label)

        def _build_connection_tab(self):
            tab = self._new_tab("Connection")
            outer = ttk.Frame(tab, padding=PAD)
            outer.pack(fill="both", expand=True)
            outer.columnconfigure(2, weight=1)
            ttk.Label(
                outer, wraplength=860, justify="left", style="Hint.TLabel",
                text="Connection settings (config.yaml). Passwords are NOT stored "
                     "here — set BAS_25LIVE_PASSWORD and BAS_NIAGARA_PASSWORD as "
                     "environment variables. Saved together with everything else "
                     "on Save (Ctrl+S)."
            ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

            self._config_vars = {}        # fid -> (var, kind, path_tuple)
            form = load_config_form(self.config_path)
            raw = read_config_raw(self.config_path)
            r = 1
            for section, fields in CONFIG_SECTIONS:
                ttk.Label(outer, text=section, style="Section.TLabel").grid(
                    row=r, column=0, columnspan=3, sticky="w", pady=(12, 4))
                r += 1
                for path_t, label, kind, hint in fields:
                    fid = ".".join(path_t)
                    ttk.Label(outer, text=label + ":").grid(
                        row=r, column=0, sticky="e", padx=(0, 10), pady=4)
                    if path_t == ("collegenet", "include_states"):
                        self._build_include_states(outer, r, _dig(raw, path_t))
                    elif kind == "bool":
                        var = tk.BooleanVar(value=bool(form.get(fid)))
                        var.trace_add("write", lambda *a: self._mark_dirty())
                        ttk.Checkbutton(outer, variable=var).grid(
                            row=r, column=1, sticky="w", pady=4)
                        self._config_vars[fid] = (var, kind, path_t)
                    elif kind == "combo":
                        var = tk.StringVar(value="" if form.get(fid) is None
                                           else str(form.get(fid)))
                        var.trace_add("write", lambda *a: self._mark_dirty())
                        ttk.Combobox(outer, textvariable=var, width=32,
                                     values=self._config_choices(path_t)).grid(
                            row=r, column=1, sticky="w", pady=4)
                        self._config_vars[fid] = (var, kind, path_t)
                    else:
                        var = tk.StringVar(value="" if form.get(fid) is None
                                           else str(form.get(fid)))
                        var.trace_add("write", lambda *a: self._mark_dirty())
                        ttk.Entry(outer, textvariable=var, width=34).grid(
                            row=r, column=1, sticky="w", pady=4)
                        self._config_vars[fid] = (var, kind, path_t)
                    if hint:
                        ttk.Label(outer, text=hint, style="Hint.TLabel").grid(
                            row=r, column=2, sticky="w", padx=(10, 0), pady=4)
                    r += 1

        def _build_include_states(self, parent, row, current):
            # Checkboxes for the documented states; any others in the file are
            # kept aside and re-written verbatim on save.
            current = [int(x) for x in current] if isinstance(current, list) else []
            if not current:
                current = [2]                     # effective default = Confirmed
            known = {s for s, _ in INCLUDE_STATE_LABELS}
            self._include_extra = [s for s in current if s not in known]
            self._include_vars = {}
            box = ttk.Frame(parent)
            box.grid(row=row, column=1, sticky="w", pady=4)
            for state, lbl in INCLUDE_STATE_LABELS:
                v = tk.BooleanVar(value=state in current)
                v.trace_add("write", lambda *a: self._mark_dirty())
                self._include_vars[state] = v
                ttk.Checkbutton(box, text=f"{lbl} ({state})", variable=v).pack(
                    side="left", padx=(0, 12))

        def _config_choices(self, path_t):
            if path_t == ("niagara", "verify_tls"):
                return ["false", "true"]
            if path_t == ("timezone",):
                try:
                    from zoneinfo import available_timezones
                    return sorted(available_timezones())
                except Exception:
                    return []
            return []

        def _build_defaults_tab(self):
            tab = self._new_tab("Defaults")
            card = ttk.Frame(tab, padding=PAD)
            card.pack(fill="x", anchor="n", padx=PAD, pady=PAD)
            ttk.Label(
                card, wraplength=820, justify="left", style="Hint.TLabel",
                text="Global scheduling defaults, applied whenever a room or "
                     "building doesn't set its own. Precedence: room > building > "
                     "these globals. Saved to defaults.yaml."
            ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))
            units = {"pre_condition_minutes": "minutes",
                     "post_buffer_minutes": "minutes",
                     "merge_gap_minutes": "minutes", "lookahead_days": "days"}
            self._defaults_vars = {}
            for i, (key, label, fb) in enumerate(DEFAULTS_FIELDS, start=1):
                ttk.Label(card, text=label + ":").grid(
                    row=i, column=0, sticky="e", padx=(0, 10), pady=7)
                cell = ttk.Frame(card)
                cell.grid(row=i, column=1, sticky="w", pady=7)
                var = tk.StringVar(value=str(self.defaults.get(key, fb)))
                var.trace_add("write", lambda *a: self._mark_dirty())
                self._defaults_vars[key] = var
                ttk.Entry(cell, textvariable=var, width=8).pack(side="left")
                ttk.Label(cell, text=units.get(key, ""),
                          style="Hint.TLabel").pack(side="left", padx=(6, 0))

        def _make_table(self, parent, table_key, columns, *, on_add, on_edit,
                        on_delete, on_duplicate, refresh):
            # Search box — filters the table live as you type. Worth its weight
            # once a campus has dozens of rooms.
            top = ttk.Frame(parent)
            top.pack(fill="x", padx=PAD, pady=(PAD, 6))
            ttk.Label(top, text="Search").pack(side="left")
            filter_var = tk.StringVar()
            entry = ttk.Entry(top, textvariable=filter_var)
            entry.pack(side="left", fill="x", expand=True, padx=(8, 0))
            filter_var.trace_add("write", lambda *a: refresh())
            ttk.Button(top, text="Clear", style="Toolbutton",
                       command=lambda: filter_var.set("")).pack(side="left", padx=(6, 0))

            # Treeview + vertical scrollbar.
            mid = ttk.Frame(parent)
            mid.pack(fill="both", expand=True, padx=PAD)
            keys = [c[0] for c in columns]
            tree = ttk.Treeview(mid, columns=keys, show="headings",
                                selectmode="browse")
            vsb = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=vsb.set)
            for key, heading, width in columns:
                tree.heading(key, text=heading,
                             command=lambda k=key: self._sort_by(table_key, k, refresh))
                tree.column(key, width=width, stretch=(key == "niagara_path"),
                            anchor="center" if width <= 80 else "w")
            tree.grid(row=0, column=0, sticky="nsew")
            vsb.grid(row=0, column=1, sticky="ns")
            mid.rowconfigure(0, weight=1)
            mid.columnconfigure(0, weight=1)
            tree.tag_configure("stripe", background=PALETTE["stripe"])

            # Button bar.
            bar = ttk.Frame(parent)
            bar.pack(fill="x", padx=PAD, pady=(8, PAD))
            ttk.Button(bar, text="Add", style="Accent.TButton",
                       command=on_add).pack(side="left")
            ttk.Button(bar, text="Edit", style="Toolbutton",
                       command=on_edit).pack(side="left", padx=(6, 0))
            ttk.Button(bar, text="Duplicate", style="Toolbutton",
                       command=on_duplicate).pack(side="left", padx=(6, 0))
            ttk.Button(bar, text="Delete", style="Toolbutton",
                       command=on_delete).pack(side="left", padx=(6, 0))
            ttk.Label(bar, text="double-click or Enter to edit · Del to delete",
                      style="Hint.TLabel").pack(side="right")

            # Keyboard + double-click.
            tree.bind("<Double-1>", lambda e: on_edit())
            tree.bind("<Return>", lambda e: on_edit())
            tree.bind("<Delete>", lambda e: on_delete())
            tree.bind("<BackSpace>", lambda e: on_delete())

            # Right-click context menu (Button-2/Ctrl-click cover macOS).
            menu = self._menu(tree)
            menu.add_command(label="Edit", command=on_edit)
            menu.add_command(label="Duplicate", command=on_duplicate)
            menu.add_separator()
            menu.add_command(label="Delete", command=on_delete)

            def _popup(event):
                row = tree.identify_row(event.y)
                if row:
                    tree.selection_set(row)
                    menu.tk_popup(event.x_root, event.y_root)
            for seq in ("<Button-3>", "<Button-2>", "<Control-Button-1>"):
                tree.bind(seq, _popup)
            return tree, filter_var

        # ── sorting (click a column header to toggle asc/desc) ──
        def _sort_by(self, table_key, col, refresh):
            rows = {"rooms": self.rooms, "buildings": self.buildings,
                    "floors": self.floors}[table_key]
            ascending = not self._sort_state.get((table_key, col), False)
            self._sort_state[(table_key, col)] = ascending

            def sort_key(item):
                v = item.get(col)
                if v is None or v == "":
                    return (1, "")                  # blanks sort last
                try:
                    return (0, f"{int(v):020d}")    # numeric columns sort by value
                except (TypeError, ValueError):
                    return (0, str(v).lower())
            rows.sort(key=sort_key, reverse=not ascending)
            refresh()
            self._mark_dirty()

        # ── rendering ──
        def _render_rows(self, tree, rows, value_fn, filter_var) -> int:
            """Render rows with the active search filter and zebra striping.
            Each row's iid stays equal to its index in the underlying list, so
            Edit/Delete keep working even while the view is filtered."""
            query = filter_var.get().strip().lower()
            tree.delete(*tree.get_children())
            shown = 0
            for i, item in enumerate(rows):
                values = value_fn(item)
                if query and not any(query in str(v).lower() for v in values):
                    continue
                tags = ("stripe",) if shown % 2 else ()
                tree.insert("", "end", iid=str(i), values=values, tags=tags)
                shown += 1
            return shown

        def _refresh_all(self):
            self._refresh_rooms()
            self._refresh_buildings()
            self._refresh_floors()
            self._update_status()

        def _refresh_rooms(self):
            shown = self._render_rows(
                self.rooms_tree, self.rooms,
                lambda r: (r.get("space_id", ""), r.get("space_name", ""),
                           r.get("building", "—"), r.get("floor", ""),
                           r.get("niagara_path", ""),
                           r.get("pre_condition_minutes", ""),
                           r.get("post_buffer_minutes", ""),
                           r.get("merge_gap_minutes", "")),
                self._room_filter)
            self._set_tab_count("Rooms", shown, len(self.rooms))
            self._update_status()

        def _refresh_floors(self):
            shown = self._render_rows(
                self.flr_tree, self.floors,
                lambda f: (f.get("building", ""), f.get("level", ""),
                           f.get("niagara_path", "")),
                self._flr_filter)
            self._set_tab_count("Floors", shown, len(self.floors))
            self._update_status()

        def _refresh_buildings(self):
            shown = self._render_rows(
                self.bld_tree, self.buildings,
                lambda b: (b.get("id", ""), b.get("name", ""),
                           b.get("niagara_path", ""),
                           b.get("pre_condition_minutes", ""),
                           b.get("post_buffer_minutes", ""),
                           b.get("space_id", "")),
                self._bld_filter)
            self._set_tab_count("Buildings", shown, len(self.buildings))
            self._update_status()

        @staticmethod
        def _selected_index(tree):
            sel = tree.selection()
            return int(sel[0]) if sel else None

        def _building_choices(self) -> list[str]:
            return [NONE_LABEL] + [str(b.get("id")) for b in self.buildings]

        def _floor_choices(self) -> list[str]:
            """Distinct floor numbers already defined, for the room form's
            editable Floor dropdown (you can still type a new one)."""
            levels = {str(f.get("level")) for f in self.floors
                      if f.get("level") not in (None, "")}

            def _k(s):
                try:
                    return (0, int(s))
                except ValueError:
                    return (1, s)
            return sorted(levels, key=_k)

        # ── room actions ──
        def _room_fields(self):
            return [
                ("space_id", "25Live Space ID *", "int", None),
                ("space_name", "Name", "text", None),
                ("building", "Building", "combo", self._building_choices()),
                ("floor", "Floor # (per-floor hallway)", "int", self._floor_choices()),
                ("niagara_path", "Niagara Path *", "text", None),
                ("pre_condition_minutes", "Pre-condition minutes", "int", None),
                ("post_buffer_minutes", "Post-buffer minutes", "int", None),
                ("merge_gap_minutes", "Merge-gap minutes", "int", None),
                ("note", "Note", "text", None),
            ]

        def _room_validate(self, original_index):
            existing_ids = {str(r.get("space_id")) for j, r in enumerate(self.rooms)
                            if j != original_index}

            def _v(values: dict):
                if "space_id" not in values:
                    return "Space ID is required."
                if str(values["space_id"]) in existing_ids:
                    return f"Space ID {values['space_id']} is already used by another room."
                if not values.get("niagara_path"):
                    return "Niagara Path is required."
                return None
            return _v

        def _room_dialog(self, values, original_index):
            dlg = FormDialog(self, "Room", self._room_fields(), values,
                             self._room_validate(original_index))
            self.wait_window(dlg)
            if dlg.result is None:
                return None
            out = dlg.result
            # "(none)" building -> no building key
            if out.get("building") in (None, NONE_LABEL):
                out.pop("building", None)
            return out

        def _room_add(self):
            out = self._room_dialog({}, original_index=None)
            if out is not None:
                self.rooms.append(out)
                self._refresh_rooms()
                self._mark_dirty()

        def _room_edit(self):
            idx = self._selected_index(self.rooms_tree)
            if idx is None:
                return
            out = self._room_dialog(dict(self.rooms[idx]), original_index=idx)
            if out is not None:
                self.rooms[idx] = out
                self._refresh_rooms()
                self._mark_dirty()

        def _room_delete(self):
            idx = self._selected_index(self.rooms_tree)
            if idx is None:
                return
            r = self.rooms[idx]
            if messagebox.askyesno("Delete room",
                                   f"Delete room {r.get('space_id')} "
                                   f"({r.get('space_name', '')})?"):
                del self.rooms[idx]
                self._refresh_rooms()
                self._mark_dirty()

        def _room_duplicate(self):
            idx = self._selected_index(self.rooms_tree)
            if idx is None:
                return
            clone = dict(self.rooms[idx])
            clone.pop("space_id", None)     # force a fresh, unique Space ID
            out = self._room_dialog(clone, original_index=None)
            if out is not None:
                self.rooms.append(out)
                self._refresh_rooms()
                self._mark_dirty()

        # ── building actions ──
        def _bld_fields(self):
            return [
                ("id", "Building ID *", "text", None),
                ("name", "Name", "text", None),
                ("niagara_path", "Niagara Path *", "text", None),
                ("pre_condition_minutes", "Pre-condition minutes (rooms)", "int", None),
                ("post_buffer_minutes", "Post-buffer minutes (rooms)", "int", None),
                ("space_id", "Bookable 25Live space_id", "int", None),
            ]

        def _bld_validate(self, original_index):
            existing_ids = {str(b.get("id")) for j, b in enumerate(self.buildings)
                            if j != original_index}

            def _v(values: dict):
                if not values.get("id"):
                    return "Building ID is required."
                if str(values["id"]) in existing_ids:
                    return f"Building ID '{values['id']}' is already in use."
                if not values.get("niagara_path"):
                    return "Niagara Path is required."
                return None
            return _v

        def _bld_dialog(self, values, original_index):
            dlg = FormDialog(self, "Building", self._bld_fields(), values,
                             self._bld_validate(original_index))
            self.wait_window(dlg)
            return dlg.result

        def _bld_add(self):
            out = self._bld_dialog({}, original_index=None)
            if out is not None:
                self.buildings.append(out)
                self._refresh_buildings()
                self._mark_dirty()

        def _bld_edit(self):
            idx = self._selected_index(self.bld_tree)
            if idx is None:
                return
            old_id = str(self.buildings[idx].get("id"))
            out = self._bld_dialog(dict(self.buildings[idx]), original_index=idx)
            if out is not None:
                # If the id changed, repoint rooms and floors referencing it.
                new_id = str(out.get("id"))
                if new_id != old_id:
                    for r in self.rooms:
                        if str(r.get("building")) == old_id:
                            r["building"] = new_id
                    for f in self.floors:
                        if str(f.get("building")) == old_id:
                            f["building"] = new_id
                self.buildings[idx] = out
                self._refresh_all()
                self._mark_dirty()

        def _bld_delete(self):
            idx = self._selected_index(self.bld_tree)
            if idx is None:
                return
            b = self.buildings[idx]
            users = [str(r.get("space_id")) for r in self.rooms
                     if str(r.get("building")) == str(b.get("id"))]
            msg = f"Delete building '{b.get('id')}'?"
            if users:
                msg += ("\n\nThese rooms reference it and will be left without a "
                        f"building:\n  {', '.join(users)}")
            if messagebox.askyesno("Delete building", msg):
                del self.buildings[idx]
                self._refresh_buildings()
                self._mark_dirty()

        def _bld_duplicate(self):
            idx = self._selected_index(self.bld_tree)
            if idx is None:
                return
            clone = dict(self.buildings[idx])
            clone.pop("id", None)           # force a fresh, unique Building ID
            out = self._bld_dialog(clone, original_index=None)
            if out is not None:
                self.buildings.append(out)
                self._refresh_buildings()
                self._mark_dirty()

        # ── floor actions (per-floor hallway schedules) ──
        def _floor_fields(self):
            building_ids = [str(b.get("id")) for b in self.buildings]
            return [
                ("building", "Building *", "combo", building_ids),
                ("level", "Floor # *", "int", None),
                ("niagara_path", "Hallway Niagara Path *", "text", None),
            ]

        def _floor_validate(self, original_index):
            existing = {(str(f.get("building")), str(f.get("level")))
                        for j, f in enumerate(self.floors) if j != original_index}

            def _v(values: dict):
                if not values.get("building"):
                    return "Building is required."
                if "level" not in values:
                    return "Floor # is required."
                if not values.get("niagara_path"):
                    return "Hallway Niagara Path is required."
                if (str(values["building"]), str(values["level"])) in existing:
                    return (f"Floor {values['level']} of '{values['building']}' "
                            "is already defined.")
                return None
            return _v

        def _floor_dialog(self, values, original_index):
            dlg = FormDialog(self, "Floor", self._floor_fields(), values,
                             self._floor_validate(original_index))
            self.wait_window(dlg)
            return dlg.result

        def _floor_add(self):
            out = self._floor_dialog({}, original_index=None)
            if out is not None:
                self.floors.append(out)
                self._refresh_floors()
                self._mark_dirty()

        def _floor_edit(self):
            idx = self._selected_index(self.flr_tree)
            if idx is None:
                return
            out = self._floor_dialog(dict(self.floors[idx]), original_index=idx)
            if out is not None:
                self.floors[idx] = out
                self._refresh_floors()
                self._mark_dirty()

        def _floor_delete(self):
            idx = self._selected_index(self.flr_tree)
            if idx is None:
                return
            f = self.floors[idx]
            if messagebox.askyesno(
                    "Delete floor",
                    f"Delete floor {f.get('level')} of '{f.get('building')}'?"):
                del self.floors[idx]
                self._refresh_floors()
                self._mark_dirty()

        def _floor_duplicate(self):
            idx = self._selected_index(self.flr_tree)
            if idx is None:
                return
            clone = dict(self.floors[idx])
            clone.pop("level", None)        # pick a new floor number
            out = self._floor_dialog(clone, original_index=None)
            if out is not None:
                self.floors.append(out)
                self._refresh_floors()
                self._mark_dirty()

    app = EditorApp(map_path)
    app.mainloop()
    return 0


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_MAP_FILE
    return run_gui(path)


if __name__ == "__main__":
    raise SystemExit(main())
