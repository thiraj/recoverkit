#!/usr/bin/env python3
"""
RecoverKit - a free, open-source file recovery tool for Windows, macOS and Linux.

Run it:
    Windows:      right-click > Run as administrator, then  python app.py
    macOS/Linux:  sudo python3 app.py

Everything is read-only against the drive being scanned. See SAFETY.md.
"""

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import carve
import diskio
import signatures
import verify
from exfat import ExfatVolume
from ntfs import NtfsVolume

APP_NAME = "RecoverKit"
VERSION = "1.0"

# Palette - kept deliberately calm and legible.
BG = "#f6f7f9"
PANEL = "#ffffff"
INK = "#1c1f23"
MUTED = "#6b7280"
ACCENT = "#2563eb"
GOOD = "#15803d"
WARN = "#b45309"
BAD = "#b91c1c"
LINE = "#e2e5ea"


def human_size(n):
    if n is None:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def human_date(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


# What the Condition column says. Blank where we have no opinion - an
# unrecognised file type gets no verdict rather than a guess.
CONDITION_TEXT = {
    signatures.MATCH: "looks intact",
    signatures.MISMATCH: "content gone",
    signatures.BLANK: "space is empty",
    signatures.MOVED: "in the Trash - not deleted",
    signatures.IN_USE: "space reused - may work",
    signatures.UNKNOWN: "",
}


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"{APP_NAME} {VERSION}")
        root.geometry("1040x680")
        root.minsize(880, 560)
        root.configure(bg=BG)

        self.results = []          # every file found
        self.visible = []          # after the search filter
        self.events = queue.Queue()
        self.stop_flag = threading.Event()
        self.scanning = False
        self.volume = None
        self.disk = None
        self.sort_column = None
        self.sort_reverse = False

        self._style()
        self._build()
        self._check_privileges()
        self.root.after(120, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI
    def _style(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass

        s.configure(".", background=BG, foreground=INK,
                    font=("Segoe UI", 10) if sys.platform == "win32"
                    else ("SF Pro Text", 12) if sys.platform == "darwin"
                    else ("DejaVu Sans", 10))
        s.configure("Card.TFrame", background=PANEL, relief="flat")
        s.configure("Header.TLabel", background=BG, foreground=INK,
                    font=("Segoe UI Semibold", 17) if sys.platform == "win32"
                    else ("SF Pro Display", 19))
        s.configure("Sub.TLabel", background=BG, foreground=MUTED)
        s.configure("Field.TLabel", background=PANEL, foreground=MUTED)
        s.configure("Accent.TButton", background=ACCENT, foreground="white",
                    borderwidth=0, padding=(16, 8))
        s.map("Accent.TButton", background=[("active", "#1d4ed8"),
                                            ("disabled", "#9db4e8")])
        s.configure("TButton", padding=(12, 6))
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                    rowheight=26, borderwidth=0)
        s.configure("Treeview.Heading", background="#eef0f4", relief="flat",
                    padding=(8, 6))
        s.map("Treeview", background=[("selected", "#dbeafe")],
              foreground=[("selected", INK)])

    def _build(self):
        outer = ttk.Frame(self.root, padding=(18, 14))
        outer.pack(fill="both", expand=True)
        outer.rowconfigure(2, weight=1)
        outer.columnconfigure(0, weight=1)

        # --- header
        head = ttk.Frame(outer)
        head.grid(row=0, column=0, sticky="we", pady=(0, 12))
        ttk.Label(head, text=APP_NAME, style="Header.TLabel").pack(side="left")
        ttk.Label(head, text="   Read-only recovery. Your existing files are "
                             "never touched.",
                  style="Sub.TLabel").pack(side="left", padx=(6, 0))

        # --- settings card
        card = ttk.Frame(outer, style="Card.TFrame", padding=14)
        card.grid(row=1, column=0, sticky="we", pady=(0, 12))
        card.columnconfigure(1, weight=1)
        card.columnconfigure(4, weight=1)

        ttk.Label(card, text="Drive to scan", style="Field.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        drive_row = ttk.Frame(card, style="Card.TFrame")
        drive_row.grid(row=0, column=1, sticky="w")
        self.volumes = []
        self.drive_var = tk.StringVar()
        self.drive_box = ttk.Combobox(
            drive_row, textvariable=self.drive_var, state="readonly", width=76)
        self.drive_box.pack(side="left")
        ttk.Button(drive_row, text="Refresh", width=8,
                   command=self._refresh_drives).pack(side="left", padx=(6, 0))
        self._refresh_drives()

        ttk.Label(card, text="Mode", style="Field.TLabel").grid(
            row=0, column=2, sticky="w", padx=(20, 8))
        self.mode_var = tk.StringVar(value="undelete")
        mode = ttk.Frame(card, style="Card.TFrame")
        mode.grid(row=0, column=3, sticky="w")
        ttk.Radiobutton(mode, text="Undelete (keeps filenames)",
                        variable=self.mode_var, value="undelete",
                        command=self._toggle_mode).pack(side="left")
        ttk.Radiobutton(mode, text="Deep scan (any drive)",
                        variable=self.mode_var, value="carve",
                        command=self._toggle_mode).pack(side="left", padx=(14, 0))

        ttk.Label(card, text="Recover to", style="Field.TLabel").grid(
            row=1, column=0, sticky="w", pady=(12, 0), padx=(0, 8))
        self.dest_var = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "Recovered"))
        ttk.Entry(card, textvariable=self.dest_var).grid(
            row=1, column=1, columnspan=3, sticky="we", pady=(12, 0))
        ttk.Button(card, text="Browse...", command=self._pick_dest).grid(
            row=1, column=4, sticky="w", padx=(8, 0), pady=(12, 0))

        self.types_row = ttk.Frame(card, style="Card.TFrame")
        self.types_row.grid(row=2, column=0, columnspan=5, sticky="w",
                            pady=(12, 0))
        ttk.Label(self.types_row, text="File types",
                  style="Field.TLabel").pack(side="left", padx=(0, 10))
        self.type_vars = {}
        for ext in carve.SIGNATURES:
            var = tk.BooleanVar(value=ext in ("jpg", "png", "pdf", "docx"))
            self.type_vars[ext] = var
            ttk.Checkbutton(self.types_row, text=ext, variable=var).pack(
                side="left", padx=(0, 8))
        self.types_row.grid_remove()

        actions = ttk.Frame(card, style="Card.TFrame")
        actions.grid(row=3, column=0, columnspan=5, sticky="w", pady=(14, 0))
        self.scan_btn = ttk.Button(actions, text="Start scan",
                                   style="Accent.TButton", command=self._start)
        self.scan_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Stop", command=self._stop,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))

        # --- results
        results = ttk.Frame(outer, style="Card.TFrame", padding=(12, 10))
        results.grid(row=2, column=0, sticky="nsew")
        results.rowconfigure(1, weight=1)
        results.columnconfigure(0, weight=1)

        bar = ttk.Frame(results, style="Card.TFrame")
        bar.grid(row=0, column=0, sticky="we", pady=(0, 8))
        bar.columnconfigure(1, weight=1)

        ttk.Label(bar, text="Search", style="Field.TLabel").grid(
            row=0, column=0, padx=(0, 8))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        entry = ttk.Entry(bar, textvariable=self.search_var)
        entry.grid(row=0, column=1, sticky="we")
        entry.bind("<Escape>", lambda e: self.search_var.set(""))

        self.only_good = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Only likely recoverable",
                        variable=self.only_good,
                        command=self._apply_filter).grid(
            row=0, column=2, padx=(12, 0))
        ttk.Button(bar, text="Select all shown",
                   command=self._select_all).grid(row=0, column=3, padx=(12, 0))
        self.recover_btn = ttk.Button(bar, text="Recover selected",
                                      style="Accent.TButton",
                                      command=self._recover, state="disabled")
        self.recover_btn.grid(row=0, column=4, padx=(8, 0))

        cols = ("name", "folder", "size", "deleted", "chance", "condition")
        self.tree = ttk.Treeview(results, columns=cols, show="headings",
                                 selectmode="extended")
        headings = {"name": "File name", "folder": "Original folder",
                    "size": "Size", "deleted": "Deleted",
                    "chance": "Recovery chance", "condition": "Condition"}
        widths = {"name": 280, "folder": 300, "size": 90,
                  "deleted": 130, "chance": 120, "condition": 150}
        for c in cols:
            self.tree.heading(c, text=headings[c],
                              command=lambda col=c: self._sort(col))
            self.tree.column(c, width=widths[c],
                             anchor="e" if c in ("size", "chance") else "w")
        self.tree.grid(row=1, column=0, sticky="nsew")

        vs = ttk.Scrollbar(results, orient="vertical", command=self.tree.yview)
        vs.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vs.set)

        self.tree.tag_configure("good", foreground=GOOD)
        self.tree.tag_configure("partial", foreground=WARN)
        self.tree.tag_configure("gone", foreground=BAD)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_buttons())

        # --- status
        status = ttk.Frame(outer)
        status.grid(row=3, column=0, sticky="we", pady=(10, 0))
        status.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(status, textvariable=self.status_var,
                  style="Sub.TLabel").grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(status, length=240, mode="determinate")
        self.progress.grid(row=0, column=1, sticky="e")

    # ------------------------------------------------------------- helpers
    def _check_privileges(self):
        elevated = True
        try:
            if sys.platform == "win32":
                import ctypes
                elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
            else:
                elevated = os.geteuid() == 0
        except Exception:
            pass
        if not elevated:
            self.status_var.set(
                "Not running with admin rights - scanning will fail. "
                + ("Restart as Administrator." if sys.platform == "win32"
                   else "Restart with sudo."))

    def _toggle_mode(self):
        if self.mode_var.get() == "carve":
            self.types_row.grid()
        else:
            self.types_row.grid_remove()

    def _pick_dest(self):
        d = filedialog.askdirectory(title="Choose where to save recovered files")
        if d:
            self.dest_var.set(d)

    def _refresh_drives(self):
        """
        Re-read the list of drives.

        Unplugging and replugging a stick gives it a new device path, so a
        list built when the window opened goes stale the moment someone
        swaps a card. Keeps the current selection if that exact drive is
        still there, otherwise falls back to the first entry.
        """
        previous = self.drive_var.get()
        diskio.refresh()
        self.volumes = diskio.list_volumes()
        labels = [v[0] for v in self.volumes]
        self.drive_box["values"] = labels

        if previous in labels:
            self.drive_box.current(labels.index(previous))
        elif labels:
            # Removable drives sort first - cards and sticks are what people
            # are usually here to recover, so start on one.
            self.drive_box.current(0)
        else:
            self.drive_var.set("")
        return labels

    def _source_path(self):
        name = self.drive_var.get()
        for display, path in self.volumes:
            if display == name:
                return path
        return name

    def _confirm_drive_still_there(self, source):
        """
        Make sure the chosen drive is still the drive it was when we listed it.

        This is a safety check, not a convenience. Device paths are reused:
        unplug a stick and plug in another and the second one can land on the
        path the first one had. Scanning a stale path means reading a drive
        the user did not choose - and the same-drive check that protects the
        recovery folder would be comparing against the wrong device too.
        """
        fresh = self._refresh_drives()
        if any(path == source for _, path in self.volumes):
            return True

        messagebox.showerror(
            "That drive has gone",
            "The drive you picked isn't there any more - it was unplugged, "
            "ejected, or has been given a new name by the system.\n\n"
            "The list has been refreshed. Pick the drive again and start the "
            "scan.")
        return False

    def _guard_destination(self, source):
        """Refuse to write anywhere near the drive we're reading."""
        dest = os.path.abspath(self.dest_var.get())
        if diskio.same_physical_drive(source, dest):
            messagebox.showerror(
                "Choose a different drive",
                "The recovery folder is on the same drive you're scanning.\n\n"
                "Writing there would overwrite the very data you're trying to "
                "recover. Pick a folder on another drive - an external disk or "
                "USB stick is ideal.")
            return None
        try:
            os.makedirs(dest, exist_ok=True)
            probe = os.path.join(dest, ".recoverkit_write_test")
            with open(probe, "wb") as fh:
                fh.write(b"ok")
            os.remove(probe)
        except OSError as e:
            messagebox.showerror("Can't write there", f"{dest}\n\n{e}")
            return None
        return dest

    # --------------------------------------------------------------- scan
    def _start(self):
        if self.scanning:
            return
        source = self._source_path()
        if not source:
            messagebox.showerror("Pick a drive", "Choose a drive to scan.")
            return
        if not self._confirm_drive_still_there(source):
            return
        if not self._guard_destination(source):
            return
        if self.mode_var.get() == "carve" and not any(
                v.get() for v in self.type_vars.values()):
            messagebox.showerror("Pick file types",
                                 "Select at least one file type to look for.")
            return

        self.results.clear()
        self.tree.delete(*self.tree.get_children())
        self.stop_flag.clear()
        self.scanning = True
        self.scan_btn["state"] = "disabled"
        self.stop_btn["state"] = "normal"
        self.recover_btn["state"] = "disabled"
        self.progress["value"] = 0

        threading.Thread(target=self._worker, args=(source,),
                         daemon=True).start()

    def _stop(self):
        self.stop_flag.set()
        self.status_var.set("Stopping...")

    @staticmethod
    def _open_volume(disk):
        """
        Pick the undelete engine that matches the drive.

        Both engines read the volume's own records, so the filename and folder
        come back with the file. If neither format is recognised the caller
        falls back to suggesting Deep scan.
        """
        try:
            return NtfsVolume(disk)
        except ValueError:
            pass
        try:
            return ExfatVolume(disk)
        except ValueError:
            raise ValueError(
                "This drive isn't formatted in a way we can undelete from. "
                "Undelete works on Windows drives (NTFS) and on memory cards "
                "and USB sticks (exFAT).")

    def _worker(self, source):
        try:
            if self.disk is not None:
                self.disk.close()
                self.disk = None
            disk = diskio.ReadOnlyDisk(source)
            self.disk = disk

            if self.mode_var.get() == "undelete":
                self.events.put(("status",
                                 f"Reading the file table on {source}..."))
                vol = self._open_volume(disk)
                self.volume = vol
                found = vol.scan(
                    progress=lambda d, t: self.events.put(("progress", (d, t))),
                    should_stop=self.stop_flag.is_set)
                self.events.put(("batch", found))
            else:
                self.volume = None
                types = [t for t, v in self.type_vars.items() if v.get()]
                size = disk.size()
                where = f"{human_size(size)} " if size else ""
                self.events.put((
                    "status",
                    f"Deep scanning {where}of {source} for "
                    f"{', '.join(types)}..."))
                batch = []
                for f in carve.scan(
                        disk, types,
                        progress=lambda d, t: self.events.put(("progress", (d, t))),
                        should_stop=self.stop_flag.is_set):
                    batch.append(f)
                    if len(batch) >= 40:
                        self.events.put(("batch", batch))
                        batch = []
                if batch:
                    self.events.put(("batch", batch))

        except ValueError as e:
            self.events.put(("error", str(e) +
                             "\n\nTry Deep scan mode instead - it works on any "
                             "drive, though it can't recover filenames."))
        except (PermissionError, FileNotFoundError) as e:
            self.events.put(("error", str(e)))
        except Exception as e:
            self.events.put(("error", f"Unexpected problem: {e}"))
        finally:
            self.events.put(("done", None))

    # ------------------------------------------------------------ results
    def _add(self, files):
        self.results.extend(files)
        self._apply_filter()

    def _apply_filter(self):
        term = self.search_var.get().strip().lower()
        only_good = self.only_good.get()

        self.visible = []
        for f in self.results:
            if only_good and (f.chance or 0) < 60:
                continue
            if term:
                haystack = f"{f.name} {f.path or ''}".lower()
                if term not in haystack:
                    continue
            self.visible.append(f)

        if self.sort_column:
            self._do_sort()

        self.tree.delete(*self.tree.get_children())
        for i, f in enumerate(self.visible[:20000]):
            chance = f.chance if f.chance is not None else 100
            tag = "good" if chance >= 80 else "partial" if chance >= 40 else "gone"
            self.tree.insert("", "end", iid=str(i), tags=(tag,), values=(
                f.name,
                f.path or "",
                human_size(f.size),
                human_date(getattr(f, "deleted_at", None)),
                f"{chance}%",
                CONDITION_TEXT.get(getattr(f, "content_check", None), ""),
            ))

        shown = len(self.visible)
        total = len(self.results)
        extra = "  (showing first 20,000)" if shown > 20000 else ""
        self.status_var.set(
            f"{shown:,} of {total:,} deleted items shown{extra}"
            if total else "Ready.")
        self._update_buttons()

    def _sort(self, column):
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column, self.sort_reverse = column, False
        self._apply_filter()

    def _do_sort(self):
        keys = {
            "name": lambda f: f.name.lower(),
            "folder": lambda f: (f.path or "").lower(),
            "size": lambda f: f.size or 0,
            "deleted": lambda f: getattr(f, "deleted_at", None) or 0,
            "chance": lambda f: f.chance or 0,
        }
        key = keys.get(self.sort_column)
        if not key:
            return
        try:
            self.visible.sort(key=key, reverse=self.sort_reverse)
        except TypeError:
            pass

    def _select_all(self):
        self.tree.selection_set(self.tree.get_children())

    def _update_buttons(self):
        # Deliberately not gated on the scan being finished. Deep scan streams
        # results in as it goes, and those carry their own data - making
        # someone watch a long scan finish before they can save a file that is
        # already in hand is just a locked door. Anything that would need to
        # read the drive again is turned away in _recover instead, where we
        # know which files were picked.
        self.recover_btn["state"] = ("normal" if self.tree.selection()
                                     else "disabled")

    # ----------------------------------------------------------- recovery
    def _recover(self):
        picks = [self.visible[int(i)] for i in self.tree.selection()]
        if not picks:
            return
        dest = self._guard_destination(self._source_path())
        if not dest:
            return

        gone = [f for f in picks
                if getattr(f, "content_check", None) in
                (signatures.MISMATCH, signatures.BLANK)]
        if gone:
            names = "\n".join(f"    {f.name}" for f in gone[:8])
            if len(gone) > 8:
                names += f"\n    ...and {len(gone) - 8} more"
            if not messagebox.askyesno(
                    "These won't open",
                    f"{len(gone)} of the files you picked are gone. The space "
                    f"they used has been written over by something else since "
                    f"they were deleted:\n\n{names}\n\n"
                    f"You can still save them, but they will not open in "
                    f"anything. Save them anyway?",
                    default="no"):
                return

        ok = fail = 0
        skipped = []
        written = []
        for f in picks:
            try:
                if self.volume:
                    data = self.volume.read_file(f)
                elif self.disk is not None and hasattr(f, "offset"):
                    data = carve.read_file(self.disk, f)   # deep-scan result
                else:
                    skipped.append(f.name)
                    continue

                if not data:
                    skipped.append(f.name)
                    continue

                folder = dest
                if f.path and f.path not in ("\\", "(no folder - carved)"):
                    safe = f.path.replace("\\", os.sep).replace(":", "")
                    folder = os.path.join(dest, safe.lstrip(os.sep))
                    os.makedirs(folder, exist_ok=True)

                target = os.path.join(folder, self._safe_name(f.name))
                target = self._unique(target)
                with open(target, "wb") as out:
                    out.write(data)
                written.append(target)
                ok += 1
            except Exception:
                fail += 1

        reports = self._verify_recovered(written)

        msg = f"Recovered {ok} file(s) to:\n{dest}"
        if fail:
            msg += f"\n\n{fail} could not be read (data overwritten)."
        if skipped:
            msg += f"\n\n{len(skipped)} had no recoverable content left."
        if reports:
            msg += "\n\n" + verify.summarise([r for _, r in reports])
        else:
            msg += ("\n\nOpen them to check - recovery can't guarantee a "
                    "file is intact.")
        messagebox.showinfo("Done", msg)
        self._offer_trim(reports)

    @staticmethod
    def _verify_recovered(written):
        """
        Look inside each recovered file and see whether it is actually whole.

        The score that got it this far only ever saw the file's first bytes.
        That is no help with a video whose header is perfect and whose index
        went missing a hundred megabytes later.
        """
        reports = []
        for path in written:
            try:
                if verify.can_check(os.path.splitext(path)[1]):
                    reports.append((path, verify.inspect_file(path)))
            except OSError:
                continue
        return reports

    def _offer_trim(self, reports):
        """Trim the ones whose own structure says where they really end."""
        fixable = [(path, r) for path, r in reports if r.repairable]
        if not fixable:
            return

        names = "\n".join(f"    {os.path.basename(p)}" for p, _ in fixable[:8])
        if len(fixable) > 8:
            names += f"\n    ...and {len(fixable) - 8} more"
        if not messagebox.askyesno(
                "Some files have extra data on the end",
                f"{len(fixable)} recovered file(s) have leftover data stuck "
                f"on the end. Each one says internally where it really "
                f"finishes, so the extra can be cut off exactly:\n\n{names}\n\n"
                f"Save tidied-up copies alongside them? The files you already "
                f"have will be left exactly as they are."):
            return

        done = 0
        for path, report in fixable:
            try:
                if verify.trim_copy(path, report):
                    done += 1
            except OSError:
                pass
        messagebox.showinfo(
            "Tidied up",
            f"Saved {done} tidied copy(ies), each named \"(trimmed)\".\n\n"
            f"Your original recovered files are untouched.")

    @staticmethod
    def _safe_name(name):
        bad = '<>:"/\\|?*'
        cleaned = "".join("_" if c in bad or ord(c) < 32 else c for c in name)
        return cleaned.strip() or "recovered_file"

    @staticmethod
    def _unique(path):
        if not os.path.exists(path):
            return path
        stem, ext = os.path.splitext(path)
        i = 2
        while os.path.exists(f"{stem} ({i}){ext}"):
            i += 1
        return f"{stem} ({i}){ext}"

    # ------------------------------------------------------------- events
    def _drain(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "batch":
                    self._add(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "progress":
                    done, total = payload
                    if total:
                        self.progress["value"] = min(100, done * 100 / total)
                    elif done:
                        # The drive would not say how big it is, so there is
                        # no percentage to show. Report real movement rather
                        # than a made-up fraction.
                        self.status_var.set(
                            f"Deep scanning... {human_size(done)} read so far")
                elif kind == "error":
                    messagebox.showerror("Scan failed", payload)
                elif kind == "done":
                    self.scanning = False
                    self.scan_btn["state"] = "normal"
                    self.stop_btn["state"] = "disabled"
                    self.progress["value"] = 100
                    self._update_buttons()
                    if self.results:
                        self.status_var.set(
                            f"Found {len(self.results):,} deleted items. "
                            "Search by name, then select and recover.")
                    else:
                        self.status_var.set("Scan finished - nothing found.")
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _on_close(self):
        self.stop_flag.set()
        if self.disk:
            self.disk.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
