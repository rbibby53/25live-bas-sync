#!/usr/bin/env python3
# 25Live -> BAS Schedule Sync — Room/Building Mapping Editor
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
25Live → BAS sync which 25Live spaces map to which BAS schedules.

Use this instead of hand-editing YAML. It edits the SAME file `main.py` reads,
so the sync picks up changes on its next run. It does not touch 25Live or any
building automation system.

Each room and building names the `system` its schedule lives on (a key from
`systems:` in config.yaml) and the `target` address within that system. The
System dropdown is populated from config.yaml, so a mixed campus — some
buildings on BACnet, some on a Niagara supervisor — is a pick from a list
rather than something to remember.

Run:
    python editor.py                 # opens the mapping next to this script
    python editor.py path\\to\\map.yaml

Tkinter ships with Python, so there are no extra dependencies. On Windows the
python.org installer includes it by default.

The YAML read/write helpers (load_mapping / dump_mapping) are deliberately kept
free of any GUI code so they can be unit-tested headlessly (see Test.py).
"""

import os
import sys
from pathlib import Path

import yaml

DEFAULT_MAP_FILE = Path(__file__).parent / "space_mapping.yaml"

# Written to the top of the file on save so a hand-editor knows the format.
FILE_HEADER = """\
# 25Live → BAS schedule cross-reference.
#
# This file is managed by editor.py (the Room Mapping Editor) but is plain YAML
# and safe to hand-edit. See README.md for the full field reference.
#
#   buildings:  each building's roll-up schedule, defined once.
#   spaces:     the rooms; each room names the `building` it belongs to, and
#               every room in a building is automatically unioned into that
#               building's occupancy schedule (any room occupied -> building on).
#
#   system:     which BAS this schedule lives on — a key from `systems:` in
#               config.yaml. Omit to use `default_system`. Rooms inherit their
#               building's system.
#   target:     the schedule's address within that system. Its syntax depends
#               on the driver:
#                 bacnet   "12001:5"          device instance : schedule instance
#                                             (add "@10.4.2.30" to pin the address)
#                 niagara  "Bldg/Rm101_Occ"   ORD under schedule_base_path
#                 rest     whatever your API path template expects
"""

# Field order we emit so the file reads cleanly and diffs stay stable.
BUILDING_KEY_ORDER = ["id", "name", "system", "target",
                      "pre_condition_minutes", "post_buffer_minutes", "space_id"]
ROOM_KEY_ORDER = ["space_id", "space_name", "building", "system", "target",
                  "pre_condition_minutes", "post_buffer_minutes",
                  "merge_gap_minutes", "note"]

# Pre-1.0 key name. Read and migrated to `target` on load, so an existing map
# opens, edits and saves without anyone having to do a find-and-replace.
LEGACY_TARGET_KEY = "niagara_path"

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

def load_mapping(path) -> tuple[list[dict], list[dict]]:
    """
    Read space_mapping.yaml into (buildings, rooms) lists of plain dicts.
    A missing or empty file yields two empty lists (fresh start).
    """
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return [], []
    with open(p, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    buildings = [_migrate_row(b) for b in (data.get("buildings", []) or [])]
    rooms = [_migrate_row(r) for r in (data.get("spaces", []) or [])]
    return buildings, rooms


def _migrate_row(row: dict) -> dict:
    """Rename a pre-1.0 `niagara_path:` to `target:`, preserving field order.

    Done on load rather than on save so the editor only ever deals in one key
    name, and an old map upgrades the first time someone saves it."""
    if not isinstance(row, dict) or LEGACY_TARGET_KEY not in row:
        return dict(row) if isinstance(row, dict) else row
    out = {}
    for key, value in row.items():
        if key == LEGACY_TARGET_KEY:
            out.setdefault("target", value)
        else:
            out[key] = value
    return out


def _ordered(row: dict, key_order: list[str]) -> dict:
    """Return row's present keys in a stable order (others appended)."""
    out = {k: row[k] for k in key_order if k in row and row[k] not in (None, "")}
    for k, v in row.items():
        if k not in out and v not in (None, ""):
            out[k] = v
    return out


def dump_mapping(buildings: list[dict], rooms: list[dict]) -> str:
    """Serialize (buildings, rooms) back to YAML text with the header."""
    payload = {
        "buildings": [_ordered(b, BUILDING_KEY_ORDER) for b in buildings],
        "spaces": [_ordered(r, ROOM_KEY_ORDER) for r in rooms],
    }
    body = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False,
                          allow_unicode=True)
    return FILE_HEADER + "\n" + body


def save_mapping(path, buildings: list[dict], rooms: list[dict]) -> None:
    """Write the mapping, keeping a single .bak of the previous version."""
    p = Path(path)
    if p.exists():
        backup = p.with_suffix(p.suffix + ".bak")
        backup.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    p.write_text(dump_mapping(buildings, rooms), encoding="utf-8")


def unknown_building_refs(buildings: list[dict], rooms: list[dict]) -> list[str]:
    """space_ids of rooms whose `building` doesn't match any defined building."""
    known = {str(b.get("id")) for b in buildings}
    bad = []
    for r in rooms:
        b = r.get("building")
        if b is not None and str(b) not in known:
            bad.append(str(r.get("space_id")))
    return bad


def configured_systems(config_path=None) -> list:
    """
    System names from config.yaml, for the System dropdown.

    Read directly rather than through main.py so the editor still opens (and
    still edits the map) when config.yaml is missing or malformed — the mapping
    editor should not be blocked by a connection-settings problem.
    """
    path = Path(config_path or os.environ.get("BAS_CONFIG")
                or Path(__file__).parent / "config.yaml")
    try:
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return []
    names = list((data.get("systems") or {}).keys())
    # A pre-1.0 config has a bare `niagara:` block instead of `systems:`.
    if not names and isinstance(data.get("niagara"), dict):
        names = ["niagara"]
    return sorted(str(n) for n in names)


def load_defaults(path) -> dict:
    """Read defaults.yaml, filling any missing key with its built-in fallback so
    the form always shows real numbers."""
    data = {}
    p = Path(path)
    if p.exists() and p.stat().st_size:
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    return {key: int(data.get(key, fb)) for key, _label, fb in DEFAULTS_FIELDS}


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
# GUI  (imported lazily so the helpers above stay usable without a display)
# ─────────────────────────────────────────────────────────────────────────────

def run_gui(map_path: Path) -> int:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog

    # ── A generic modal form built from a field spec. Adding a field is one
    #    line in the spec list, which keeps Room/Building forms easy to extend.
    class FormDialog(tk.Toplevel):
        # field spec: (key, label, kind, options)
        #   kind: "text" | "int" | "combo";  options: list for "combo"
        def __init__(self, parent, title, fields, values, validate):
            super().__init__(parent)
            self.title(title)
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
                else:
                    w = ttk.Entry(self, textvariable=var, width=36)
                w.grid(row=i, column=1, sticky="w", padx=8, pady=4)
                if i == 0:
                    w.focus_set()

            btns = ttk.Frame(self)
            btns.grid(row=len(fields), column=0, columnspan=2, pady=(8, 10))
            ttk.Button(btns, text="OK", command=self._ok).pack(
                side="left", padx=5)
            ttk.Button(btns, text="Cancel", command=self.destroy).pack(
                side="left", padx=5)
            self.bind("<Return>", lambda e: self._ok())
            self.bind("<Escape>", lambda e: self.destroy())

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
    # Rooms inherit their building's system; buildings fall back to
    # config.yaml's `default_system`. Both are stored as "no system key".
    INHERIT_LABEL = "(inherit)"
    DEFAULT_LABEL = "(default)"

    def _strip_sentinels(values: dict) -> None:
        """Drop the placeholder system labels — absent means inherit."""
        if values.get("system") in (None, "", INHERIT_LABEL, DEFAULT_LABEL):
            values.pop("system", None)

    class EditorApp(tk.Tk):
        def __init__(self, path: Path):
            super().__init__()
            self.path = path
            self.buildings: list[dict] = []
            self.rooms: list[dict] = []
            self.defaults_path = DEFAULT_DEFAULTS_FILE
            self.defaults = load_defaults(self.defaults_path)
            # Populates the System dropdowns. Empty when config.yaml is absent,
            # in which case the field falls back to free text so the map can
            # still be built before the connection settings exist.
            self.systems = configured_systems()
            self.dirty = False

            self.title("25Live → BAS — Room Mapping Editor")
            self.geometry("840x520")
            self._build_menu()
            self._build_tabs()
            self._load(path)

        # ── data ──
        def _load(self, path: Path):
            try:
                self.buildings, self.rooms = load_mapping(path)
            except Exception as exc:
                messagebox.showerror("Load failed", f"Could not read\n{path}\n\n{exc}")
                self.buildings, self.rooms = [], []
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

            bad = unknown_building_refs(self.buildings, self.rooms)
            if bad and not messagebox.askyesno(
                    "Unknown building",
                    "These rooms reference a building that doesn't exist and "
                    f"won't roll up:\n\n  {', '.join(bad)}\n\nSave anyway?"):
                return False
            try:
                save_mapping(self.path, self.buildings, self.rooms)
                save_defaults(self.defaults_path, new_defaults)
            except Exception as exc:
                messagebox.showerror("Save failed", str(exc))
                return False
            self.defaults = new_defaults
            self.dirty = False
            self._update_title()
            messagebox.showinfo(
                "Saved", f"Saved {len(self.rooms)} rooms, {len(self.buildings)} "
                f"buildings, and global defaults.")
            return True

        def _mark_dirty(self):
            self.dirty = True
            self._update_title()

        def _update_title(self):
            star = "*" if self.dirty else ""
            self.title(f"{star}25Live → BAS — Room Mapping Editor  [{self.path.name}]")

        # ── menu ──
        def _build_menu(self):
            from tkinter import Menu
            bar = Menu(self)
            filem = Menu(bar, tearoff=0)
            filem.add_command(label="Open…", command=self._on_open)
            filem.add_command(label="Save", command=self._save, accelerator="Ctrl+S")
            filem.add_separator()
            filem.add_command(label="Exit", command=self._on_close)
            bar.add_cascade(label="File", menu=filem)

            toolm = Menu(bar, tearoff=0)
            toolm.add_command(label="Test 25Live connection",
                              command=self._test_25live)
            toolm.add_command(label="Test BAS connections",
                              command=self._test_systems)
            toolm.add_separator()
            toolm.add_command(label="Preview (dry run)…", command=self._preview)
            bar.add_cascade(label="Tools", menu=toolm)

            self.config(menu=bar)
            self.bind_all("<Control-s>", lambda e: self._save())
            self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── tools (use the sync engine in main.py; need config.yaml) ──
        def _runtime_config(self):
            """The same config the nightly sync would use, secrets included."""
            from bassync import config as bas_config
            cfg_path = (os.environ.get("BAS_CONFIG")
                        or str(Path(__file__).parent / "config.yaml"))
            defaults_path = (os.environ.get("BAS_DEFAULTS")
                             or str(Path(__file__).parent / "defaults.yaml"))
            cfg = bas_config.load_config(cfg_path, defaults_path)
            bas_config.load_credentials(cfg)
            return bas_config, cfg

        def _test_25live(self):
            from zoneinfo import ZoneInfo
            try:
                _cfgmod, cfg = self._runtime_config()
                if not cfg["collegenet"].get("base_url"):
                    messagebox.showwarning(
                        "Test 25Live",
                        "No 25Live instance/base_url in config.yaml.\n"
                        "Copy config.example.yaml to config.yaml and set it.")
                    return
                from bassync.collegenet import CollegeNetClient
                cn = CollegeNetClient(cfg["collegenet"],
                                      ZoneInfo(cfg["timezone"]), cfg.get("retry"))
                try:
                    ok, detail = cn.check_connection()
                finally:
                    cn.close()
                (messagebox.showinfo if ok else messagebox.showerror)(
                    "Test 25Live", f"{'Connected' if ok else 'FAILED'}\n\n{detail}")
            except Exception as exc:
                messagebox.showerror("Test 25Live", f"Error: {exc}")

        def _test_systems(self):
            """Health-check every system in config.yaml, one line each.

            Reports them all rather than stopping at the first failure — on a
            mixed campus "BACnet is fine, the supervisor is down" is the useful
            answer, not "something is broken"."""
            from zoneinfo import ZoneInfo
            try:
                _cfgmod, cfg = self._runtime_config()
                from bassync.drivers import DriverError, build_driver
            except Exception as exc:
                messagebox.showerror("Test BAS connections", f"Error: {exc}")
                return

            systems = cfg.get("systems") or {}
            if not systems:
                messagebox.showwarning(
                    "Test BAS connections",
                    "No `systems:` block in config.yaml.\n"
                    "Copy config.example.yaml to config.yaml and define at "
                    "least one BAS system.")
                return

            tz = ZoneInfo(cfg["timezone"])
            lines, all_ok = [], True
            for name in sorted(systems):
                try:
                    driver = build_driver(name, systems[name], tz, cfg.get("retry"))
                except DriverError as exc:
                    lines.append(f"FAIL  {name}: {exc}")
                    all_ok = False
                    continue
                try:
                    driver.connect()
                    ok, detail = driver.health_check()
                except Exception as exc:
                    ok, detail = False, f"{type(exc).__name__}: {exc}"
                finally:
                    driver.close()
                all_ok = all_ok and ok
                lines.append(f"{'OK  ' if ok else 'FAIL'}  {name} "
                             f"({systems[name].get('driver', '?')}): {detail}")
            self._show_text("Test BAS connections", "\n".join(lines))
            if not all_ok:
                messagebox.showwarning(
                    "Test BAS connections",
                    "At least one system is unreachable — see the details window.")

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
                _cfgmod, cfg = self._runtime_config()
                cfg["space_map_file"] = str(self.path)
                buf = io.StringIO()
                handler = _logging.StreamHandler(buf)
                handler.setFormatter(_logging.Formatter("%(levelname)-7s %(message)s"))
                root = _logging.getLogger()
                root.addHandler(handler)
                prev = root.level
                root.setLevel(_logging.INFO)
                try:
                    from bassync.sync import run_sync
                    run_sync(cfg, dry_run=True)
                finally:
                    root.removeHandler(handler)
                    root.setLevel(prev)
                self._show_text("Preview (dry run)", buf.getvalue() or "(no output)")
            except Exception as exc:
                messagebox.showerror("Preview", f"Error: {exc}")

        def _show_text(self, title: str, text: str):
            win = tk.Toplevel(self)
            win.title(title)
            win.geometry("720x460")
            win.transient(self)
            txt = tk.Text(win, wrap="none")
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

        # ── tabs / tables ──
        def _build_tabs(self):
            nb = ttk.Notebook(self)
            nb.pack(fill="both", expand=True, padx=8, pady=8)

            # Rooms tab
            rooms_tab = ttk.Frame(nb)
            nb.add(rooms_tab, text="Rooms")
            self.rooms_tree = self._make_table(
                rooms_tab,
                columns=[("space_id", "Space ID", 75),
                         ("space_name", "Name", 175),
                         ("building", "Building", 115),
                         ("system", "System", 110),
                         ("target", "Target", 190),
                         ("pre_condition_minutes", "Pre", 45),
                         ("post_buffer_minutes", "Post", 45),
                         ("merge_gap_minutes", "Gap", 45)],
                on_add=self._room_add, on_edit=self._room_edit,
                on_delete=self._room_delete)

            # Buildings tab
            bld_tab = ttk.Frame(nb)
            nb.add(bld_tab, text="Buildings")
            self.bld_tree = self._make_table(
                bld_tab,
                columns=[("id", "ID", 130),
                         ("name", "Name", 180),
                         ("system", "System", 110),
                         ("target", "Target", 190),
                         ("pre_condition_minutes", "Pre", 45),
                         ("post_buffer_minutes", "Post", 45),
                         ("space_id", "Bookable space_id", 120)],
                on_add=self._bld_add, on_edit=self._bld_edit,
                on_delete=self._bld_delete)

            # Defaults tab
            def_tab = ttk.Frame(nb)
            nb.add(def_tab, text="Defaults")
            ttk.Label(
                def_tab, wraplength=780, justify="left",
                text="Global scheduling defaults. Rooms and buildings can override "
                     "run-up/run-down in the other tabs (precedence: room > "
                     "building > these globals). Saved to defaults.yaml."
            ).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(12, 10))
            self._defaults_vars = {}
            for i, (key, label, fb) in enumerate(DEFAULTS_FIELDS, start=1):
                ttk.Label(def_tab, text=label).grid(
                    row=i, column=0, sticky="e", padx=10, pady=6)
                var = tk.StringVar(value=str(self.defaults.get(key, fb)))
                var.trace_add("write", lambda *a: self._mark_dirty())
                self._defaults_vars[key] = var
                ttk.Entry(def_tab, textvariable=var, width=12).grid(
                    row=i, column=1, sticky="w", padx=10, pady=6)

        def _make_table(self, parent, columns, on_add, on_edit, on_delete):
            keys = [c[0] for c in columns]
            tree = ttk.Treeview(parent, columns=keys, show="headings",
                                selectmode="browse")
            for key, heading, width in columns:
                tree.heading(key, text=heading)
                tree.column(key, width=width, anchor="w")
            tree.pack(fill="both", expand=True, side="top", padx=4, pady=4)
            tree.bind("<Double-1>", lambda e: on_edit())

            bar = ttk.Frame(parent)
            bar.pack(fill="x", padx=4, pady=(0, 6))
            ttk.Button(bar, text="Add", command=on_add).pack(side="left", padx=3)
            ttk.Button(bar, text="Edit", command=on_edit).pack(side="left", padx=3)
            ttk.Button(bar, text="Delete", command=on_delete).pack(side="left", padx=3)
            return tree

        def _refresh_all(self):
            self._refresh_rooms()
            self._refresh_buildings()

        def _refresh_rooms(self):
            self.rooms_tree.delete(*self.rooms_tree.get_children())
            for i, r in enumerate(self.rooms):
                self.rooms_tree.insert("", "end", iid=str(i), values=(
                    r.get("space_id", ""), r.get("space_name", ""),
                    r.get("building", "—"), r.get("system", "(inherit)"),
                    r.get("target", ""),
                    r.get("pre_condition_minutes", ""),
                    r.get("post_buffer_minutes", ""),
                    r.get("merge_gap_minutes", "")))

        def _refresh_buildings(self):
            self.bld_tree.delete(*self.bld_tree.get_children())
            for i, b in enumerate(self.buildings):
                self.bld_tree.insert("", "end", iid=str(i), values=(
                    b.get("id", ""), b.get("name", ""),
                    b.get("system", "(default)"), b.get("target", ""),
                    b.get("pre_condition_minutes", ""),
                    b.get("post_buffer_minutes", ""),
                    b.get("space_id", "")))

        @staticmethod
        def _selected_index(tree):
            sel = tree.selection()
            return int(sel[0]) if sel else None

        def _building_choices(self) -> list[str]:
            return [NONE_LABEL] + [str(b.get("id")) for b in self.buildings]

        def _system_choices(self, inherit_label: str) -> list[str]:
            """Systems from config.yaml, with an 'inherit' option first.

            A free-text Entry is used instead when config.yaml defines none,
            so the map can be written before the connection settings exist."""
            return [inherit_label] + self.systems

        def _system_field(self, label: str, inherit_label: str):
            if not self.systems:
                return ("system", label + " (from config.yaml)", "text", None)
            return ("system", label, "combo", self._system_choices(inherit_label))

        # ── room actions ──
        def _room_fields(self):
            return [
                ("space_id", "25Live Space ID *", "int", None),
                ("space_name", "Name", "text", None),
                ("building", "Building", "combo", self._building_choices()),
                self._system_field("BAS System", INHERIT_LABEL),
                ("target", "Target *", "text", None),
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
                if not values.get("target"):
                    return ("Target is required — the schedule's address in "
                            "its BAS (e.g. \"12001:5\" for BACnet, "
                            "\"Bldg/Rm101_Occ\" for Niagara).")
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
            _strip_sentinels(out)
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

        # ── building actions ──
        def _bld_fields(self):
            return [
                ("id", "Building ID *", "text", None),
                ("name", "Name", "text", None),
                self._system_field("BAS System", DEFAULT_LABEL),
                ("target", "Target *", "text", None),
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
                if not values.get("target"):
                    return ("Target is required — the schedule's address in "
                            "its BAS (e.g. \"12001:5\" for BACnet, "
                            "\"Bldg/Rm101_Occ\" for Niagara).")
                return None
            return _v

        def _bld_dialog(self, values, original_index):
            dlg = FormDialog(self, "Building", self._bld_fields(), values,
                             self._bld_validate(original_index))
            self.wait_window(dlg)
            if dlg.result is not None:
                _strip_sentinels(dlg.result)
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
                # If the id changed, repoint rooms that referenced the old id.
                new_id = str(out.get("id"))
                if new_id != old_id:
                    for r in self.rooms:
                        if str(r.get("building")) == old_id:
                            r["building"] = new_id
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

    app = EditorApp(map_path)
    app.mainloop()
    return 0


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_MAP_FILE
    return run_gui(path)


if __name__ == "__main__":
    raise SystemExit(main())
