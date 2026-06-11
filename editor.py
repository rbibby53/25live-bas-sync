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
BUILDING_KEY_ORDER = ["id", "name", "niagara_path", "space_id"]
ROOM_KEY_ORDER = ["space_id", "space_name", "building", "niagara_path",
                  "pre_condition_minutes", "post_buffer_minutes",
                  "merge_gap_minutes", "note"]


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
    buildings = list(data.get("buildings", []) or [])
    rooms = list(data.get("spaces", []) or [])
    return buildings, rooms


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

    class EditorApp(tk.Tk):
        def __init__(self, path: Path):
            super().__init__()
            self.path = path
            self.buildings: list[dict] = []
            self.rooms: list[dict] = []
            self.dirty = False

            self.title("25Live → Niagara — Room Mapping Editor")
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
            bad = unknown_building_refs(self.buildings, self.rooms)
            if bad and not messagebox.askyesno(
                    "Unknown building",
                    "These rooms reference a building that doesn't exist and "
                    f"won't roll up:\n\n  {', '.join(bad)}\n\nSave anyway?"):
                return False
            try:
                save_mapping(self.path, self.buildings, self.rooms)
            except Exception as exc:
                messagebox.showerror("Save failed", str(exc))
                return False
            self.dirty = False
            self._update_title()
            messagebox.showinfo("Saved", f"Saved {len(self.rooms)} rooms and "
                                f"{len(self.buildings)} buildings to\n{self.path}")
            return True

        def _mark_dirty(self):
            self.dirty = True
            self._update_title()

        def _update_title(self):
            star = "*" if self.dirty else ""
            self.title(f"{star}25Live → Niagara — Room Mapping Editor  [{self.path.name}]")

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
            cfg_path = (os.environ.get("BAS_CONFIG")
                        or str(Path(__file__).parent / "config.yaml"))
            cfg = main.load_config(cfg_path)
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
                columns=[("space_id", "Space ID", 80),
                         ("space_name", "Name", 200),
                         ("building", "Building", 140),
                         ("niagara_path", "Niagara Path", 210),
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
                columns=[("id", "ID", 160),
                         ("name", "Name", 240),
                         ("niagara_path", "Niagara Path", 240),
                         ("space_id", "Bookable space_id", 140)],
                on_add=self._bld_add, on_edit=self._bld_edit,
                on_delete=self._bld_delete)

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
                    r.get("building", "—"), r.get("niagara_path", ""),
                    r.get("pre_condition_minutes", ""),
                    r.get("post_buffer_minutes", ""),
                    r.get("merge_gap_minutes", "")))

        def _refresh_buildings(self):
            self.bld_tree.delete(*self.bld_tree.get_children())
            for i, b in enumerate(self.buildings):
                self.bld_tree.insert("", "end", iid=str(i), values=(
                    b.get("id", ""), b.get("name", ""),
                    b.get("niagara_path", ""), b.get("space_id", "")))

        @staticmethod
        def _selected_index(tree):
            sel = tree.selection()
            return int(sel[0]) if sel else None

        def _building_choices(self) -> list[str]:
            return [NONE_LABEL] + [str(b.get("id")) for b in self.buildings]

        # ── room actions ──
        def _room_fields(self):
            return [
                ("space_id", "25Live Space ID *", "int", None),
                ("space_name", "Name", "text", None),
                ("building", "Building", "combo", self._building_choices()),
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

        # ── building actions ──
        def _bld_fields(self):
            return [
                ("id", "Building ID *", "text", None),
                ("name", "Name", "text", None),
                ("niagara_path", "Niagara Path *", "text", None),
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
