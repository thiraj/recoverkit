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
from tkinter import filedialog, font as tkfont, messagebox, ttk

import carve
import diskio
import recovery
import signatures
import verify
from exfat import ExfatVolume
from fat32 import Fat32Volume
from ntfs import NtfsVolume

APP_NAME = "RecoverKit"
VERSION = "1.0"

# Palette. Flat, high-contrast, one accent colour doing all the work - the
# house style of the developer tools people are used to looking at.
BG = "#ffffff"          # the working surface
SIDEBAR = "#f6f8fa"     # the settings rail down the left
PANEL = "#ffffff"
INK = "#16202c"         # primary text
MUTED = "#69778a"       # labels and secondary text
FAINT = "#8c97a5"
ACCENT = "#1d63ed"
ACCENT_DEEP = "#1550c8"  # hover
ACCENT_SOFT = "#e8f0fe"  # selection wash
GOOD = "#0f7b46"
WARN = "#9a5b06"
BAD = "#c0342c"
LINE = "#e3e8ee"
STRIPE = "#fafbfc"
WASH_WARN = "#fff8ec"   # a hint of caution, not a warning label
WASH_BAD = "#fdf1f0"
DISABLED = "#c3ccd8"


def _first_font(root, candidates, size, weight="normal"):
    """
    Pick the first font actually installed.

    Naming a font that is not present does not fail loudly - Tk quietly
    substitutes something, usually of a completely different weight. Asking
    the font system what exists keeps the window looking the same on the
    machines it is meant to run on.
    """
    try:
        available = set(tkfont.families(root))
    except Exception:
        available = set()
    for name in candidates:
        if name in available:
            return (name, size, weight) if weight != "normal" else (name, size)
    return ("TkDefaultFont", size, weight) if weight != "normal" \
        else ("TkDefaultFont", size)


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


def _round_rect(canvas, x1, y1, x2, y2, radius, **kw):
    """A rounded rectangle, which Tk has no primitive for."""
    radius = max(0, min(radius, (x2 - x1) // 2, (y2 - y1) // 2))
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kw)


class PillButton(tk.Canvas):
    """
    A flat rounded button.

    ttk cannot round a corner on any platform's native theme, and a square
    grey button is the single thing that makes an application look a decade
    old. Drawing it on a canvas costs about forty lines and is the difference
    between "a Python script" and "an application".

    Supports `widget["state"]` in both directions so the rest of the app can
    treat it exactly like a ttk.Button.
    """

    HEIGHT = 34
    RADIUS = 8

    def __init__(self, master, text, command=None, kind="primary",
                 width=None, font=None, **kw):
        self.kind = kind
        self.command = command
        self._state = "normal"
        self._text = text
        self._font = font or ("TkDefaultFont", 12)

        measure = tkfont.Font(font=self._font)
        self._width = width or measure.measure(text) + 34
        background = kw.pop("background", None) or (
            SIDEBAR if kind == "sidebar" else BG)

        super().__init__(master, width=self._width, height=self.HEIGHT,
                         highlightthickness=0, bd=0, background=background,
                         cursor="arrow", takefocus=1, **kw)
        self._draw()
        self.bind("<Button-1>", self._press)
        self.bind("<Enter>", lambda e: self._draw(hover=True))
        self.bind("<Leave>", lambda e: self._draw())
        # A hand-drawn button still has to behave like a button: reachable by
        # Tab, and pressed by Space or Return.
        self.bind("<FocusIn>", lambda e: self._draw(hover=True))
        self.bind("<FocusOut>", lambda e: self._draw())
        self.bind("<Return>", self._press)
        self.bind("<space>", self._press)

    # -- appearance ---------------------------------------------------------
    def _colours(self, hover):
        if self._state == "disabled":
            if self.kind == "primary":
                return DISABLED, "#ffffff", DISABLED
            return BG, DISABLED, LINE
        if self.kind == "primary":
            fill = ACCENT_DEEP if hover else ACCENT
            return fill, "#ffffff", fill
        if self.kind == "danger":
            return (BG, BAD, BAD)
        return ((ACCENT_SOFT if hover else BG), INK, LINE)

    def _draw(self, hover=False):
        self.delete("all")
        fill, ink, outline = self._colours(hover)
        _round_rect(self, 1, 1, self._width - 1, self.HEIGHT - 1, self.RADIUS,
                    fill=fill, outline=outline)
        self.create_text(self._width // 2, self.HEIGHT // 2, text=self._text,
                         fill=ink, font=self._font)

    # -- behaviour ----------------------------------------------------------
    def _press(self, _event):
        if self._state != "disabled" and self.command:
            self.command()

    def configure(self, **kw):
        if "state" in kw:
            self._state = str(kw.pop("state"))
            self._draw()
        if "text" in kw:
            self._text = kw.pop("text")
            self._draw()
        if kw:
            super().configure(**kw)

    config = configure

    def __setitem__(self, key, value):
        if key in ("state", "text"):
            self.configure(**{key: value})
        else:
            super().__setitem__(key, value)

    def __getitem__(self, key):
        if key == "state":
            return self._state
        if key == "text":
            return self._text
        return super().__getitem__(key)


class CheckBox(tk.Canvas):
    """
    A drawn checkbox.

    ttk's indicator is a sunken square with a tick in it - the single most
    dated-looking element in the whole window. This is a rounded box that
    fills with the accent colour, which is what every current application
    looks like.
    """

    BOX = 17

    def __init__(self, master, text, variable, command=None, font=None,
                 background=SIDEBAR, **kw):
        self.variable = variable
        self.command = command
        self._font = font or ("TkDefaultFont", 11)
        measure = tkfont.Font(font=self._font)
        width = self.BOX + 8 + measure.measure(text) + 4
        super().__init__(master, width=width, height=self.BOX + 6,
                         highlightthickness=0, bd=0, background=background,
                         takefocus=1, **kw)
        self._text = text
        self._draw()
        for sequence in ("<Button-1>", "<Return>", "<space>"):
            self.bind(sequence, self._toggle)
        self.bind("<Enter>", lambda e: self._draw(hover=True))
        self.bind("<Leave>", lambda e: self._draw())
        variable.trace_add("write", lambda *_: self._draw())

    def _draw(self, hover=False):
        self.delete("all")
        on = bool(self.variable.get())
        top = 3
        fill = ACCENT if on else PANEL
        edge = ACCENT if on else (ACCENT if hover else LINE)
        _round_rect(self, 1, top, self.BOX, top + self.BOX - 3, 5,
                    fill=fill, outline=edge)
        if on:
            x, y = 5, top + 8
            self.create_line(x, y, x + 3, y + 4, x + 9, y - 4,
                             fill="#ffffff", width=2, capstyle="round",
                             joinstyle="round")
        self.create_text(self.BOX + 8, top + 7, text=self._text, anchor="w",
                         fill=INK, font=self._font)

    def _toggle(self, _event=None):
        self.variable.set(not self.variable.get())
        self._draw()
        if self.command:
            self.command()


class ThinScrollbar(tk.Canvas):
    """
    A scrollbar with no arrow buttons and no trough.

    Stepper arrows at both ends have not been part of a current interface for
    fifteen years, and ttk draws them on every platform theme this app can
    reach. Implements `set` so it drops straight into `yscrollcommand`.
    """

    WIDTH = 10

    def __init__(self, master, command, background=PANEL, **kw):
        super().__init__(master, width=self.WIDTH, highlightthickness=0, bd=0,
                         background=background, **kw)
        self.command = command
        self._first, self._last = 0.0, 1.0
        self._grab = None
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "_grab", None))
        self.bind("<Enter>", lambda e: self._draw(hover=True))
        self.bind("<Leave>", lambda e: self._draw())

    def set(self, first, last):
        self._first, self._last = float(first), float(last)
        self._draw()

    def _draw(self, hover=False):
        self.delete("all")
        height = self.winfo_height()
        if height <= 1 or (self._first <= 0.0 and self._last >= 1.0):
            return                              # nothing to scroll: show nothing
        top = self._first * height
        bottom = max(self._last * height, top + 24)
        _round_rect(self, 2, top + 2, self.WIDTH - 2, bottom - 2, 4,
                    fill=(MUTED if hover else "#c9d1da"), outline="")

    def _press(self, event):
        height = max(self.winfo_height(), 1)
        top, bottom = self._first * height, self._last * height
        if top <= event.y <= bottom:
            self._grab = event.y - top
        else:                                   # jump to where they clicked
            self._grab = (bottom - top) / 2
            self._drag(event)

    def _drag(self, event):
        if self._grab is None:
            return
        height = max(self.winfo_height(), 1)
        self.command("moveto", max(0.0, min(1.0, (event.y - self._grab) / height)))


class Dropdown(tk.Canvas):
    """
    A flat dropdown with a drawn chevron and a styled popup list.

    ttk's Combobox arrow is a chunky raised square drawn by the platform
    theme, and cannot be restyled without replacing the theme element itself.
    Drawing the closed state and popping a plain window for the open state is
    less code than fighting it, and looks like software from this decade.

    Keeps the parts of the Combobox API this app uses - `["values"]`,
    `current(i)`, a text variable, and a <<ComboboxSelected>> event - so
    nothing else had to change.
    """

    HEIGHT = 34

    def __init__(self, master, textvariable, command=None, font=None,
                 background=SIDEBAR, **kw):
        self.variable = textvariable
        # A direct callback rather than only a virtual event: Tk delivers
        # virtual events through the event loop, so a caller cannot rely on
        # having been told anything by the time `_choose` returns.
        self.command = command
        self._values = []
        self._font = font or ("TkDefaultFont", 11)
        self._popup = None
        super().__init__(master, height=self.HEIGHT, highlightthickness=0,
                         bd=0, background=background, takefocus=1, **kw)
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", lambda e: self._open())
        self.bind("<Return>", lambda e: self._open())
        self.bind("<Enter>", lambda e: self._draw(hover=True))
        self.bind("<Leave>", lambda e: self._draw())
        textvariable.trace_add("write", lambda *_: self._draw())

    # -- closed state -------------------------------------------------------
    def _draw(self, hover=False):
        self.delete("all")
        width = self.winfo_width()
        if width <= 1:
            return
        _round_rect(self, 1, 1, width - 1, self.HEIGHT - 1, 8, fill=PANEL,
                    outline=ACCENT if hover else LINE)
        measure = tkfont.Font(font=self._font)
        text = self.variable.get() or "No drives found"
        room = width - 46
        while text and measure.measure(text) > room:
            text = text[:-2]
        if text != (self.variable.get() or "No drives found"):
            text += "\u2026"
        self.create_text(12, self.HEIGHT // 2, text=text, anchor="w",
                         fill=INK if self.variable.get() else FAINT,
                         font=self._font)
        x, y = width - 20, self.HEIGHT // 2 - 2
        self.create_line(x - 5, y, x, y + 5, x + 5, y, fill=MUTED, width=2,
                         capstyle="round", joinstyle="round")

    # -- open state ---------------------------------------------------------
    def _open(self):
        if self._popup or not self._values:
            return
        popup = tk.Toplevel(self)
        popup.overrideredirect(True)
        popup.configure(background=LINE)
        popup.geometry(f"+{self.winfo_rootx()}"
                       f"+{self.winfo_rooty() + self.HEIGHT + 2}")
        inner = tk.Frame(popup, background=PANEL)
        inner.pack(padx=1, pady=1)

        for value in self._values:
            row = tk.Label(inner, text=value, anchor="w", background=PANEL,
                           foreground=INK, font=self._font, padx=12, pady=7,
                           width=max(len(v) for v in self._values))
            row.pack(fill="x")
            row.bind("<Enter>", lambda e, r=row: r.configure(
                background=ACCENT_SOFT))
            row.bind("<Leave>", lambda e, r=row: r.configure(background=PANEL))
            row.bind("<Button-1>", lambda e, v=value: self._choose(v))

        self._popup = popup
        popup.bind("<Escape>", lambda e: self._close())
        popup.grab_set()
        popup.bind("<Button-1>", lambda e: None)
        self.winfo_toplevel().bind("<Button-1>", lambda e: self._close(),
                                   add="+")

    def _close(self):
        if self._popup:
            self._popup.grab_release()
            self._popup.destroy()
            self._popup = None

    def _choose(self, value):
        self.variable.set(value)
        self._close()
        self._draw()
        if self.command:
            self.command()
        self.event_generate("<<ComboboxSelected>>", when="now")

    # -- the slice of the Combobox API this app relies on --------------------
    def current(self, index=None):
        if index is None:
            values = list(self._values)
            return values.index(self.variable.get()) \
                if self.variable.get() in values else -1
        if 0 <= index < len(self._values):
            self.variable.set(self._values[index])
        return None

    def configure(self, **kw):
        if "values" in kw:
            self._values = list(kw.pop("values"))
            self._draw()
        if kw:
            super().configure(**kw)

    config = configure

    def __setitem__(self, key, value):
        if key == "values":
            self.configure(values=value)
        else:
            super().__setitem__(key, value)

    def __getitem__(self, key):
        if key == "values":
            return self._values
        return super().__getitem__(key)


class Segmented(tk.Frame):
    """
    A two-option segmented control, bound to a StringVar.

    Two radio buttons say "here are two settings". A segmented control says
    "this application is in one of two modes", which is what choosing between
    Undelete and Deep scan actually is.
    """

    def __init__(self, master, variable, options, command=None, **kw):
        super().__init__(master, background=SIDEBAR, **kw)
        self.variable = variable
        self.command = command
        self._buttons = {}
        font = _first_font(master, ["SF Pro Text", "Segoe UI", "Inter",
                                    "DejaVu Sans"], 11)
        for index, (value, label) in enumerate(options):
            button = PillButton(self, label, kind="ghost", font=font,
                                background=SIDEBAR,
                                command=lambda v=value: self._choose(v))
            button.grid(row=index, column=0, sticky="we", pady=(0, 6))
            self.columnconfigure(0, weight=1)
            self._buttons[value] = button
        self._paint()

    def _choose(self, value):
        self.variable.set(value)
        self._paint()
        if self.command:
            self.command()

    def _paint(self):
        chosen = self.variable.get()
        for value, button in self._buttons.items():
            button.kind = "primary" if value == chosen else "ghost"
            button._draw()


# What the Condition column says. Blank where we have no opinion - an
# unrecognised file type gets no verdict rather than a guess.
CONDITION_TEXT = {
    signatures.MATCH: "\u25cf  looks intact",
    signatures.MISMATCH: "\u25cb  content gone",
    signatures.BLANK: "\u25cb  space is empty",
    signatures.MOVED: "\u21a9  in the Trash, not deleted",
    signatures.IN_USE: "\u25d0  space reused, may work",
    signatures.UNKNOWN: "",
}


class App:
    def __init__(self, root):
        self.root = root
        root.title(f"{APP_NAME} {VERSION}")
        root.geometry("1180x720")
        root.minsize(1000, 600)
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
        self.status_var = tk.StringVar(value="Ready.")
        self.subtitle_var = tk.StringVar(
            value="Pick a drive and press Start scan.")

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

        self.font_body = _first_font(
            self.root, ["SF Pro Text", "Segoe UI", "Inter", "DejaVu Sans"], 12)
        self.font_small = _first_font(
            self.root, ["SF Pro Text", "Segoe UI", "Inter", "DejaVu Sans"], 11)
        self.font_title = _first_font(
            self.root, ["SF Pro Display", "Segoe UI Semibold", "Inter",
                        "DejaVu Sans"], 20, "bold")
        self.font_section = _first_font(
            self.root, ["SF Pro Text", "Segoe UI Semibold", "Inter",
                        "DejaVu Sans"], 10, "bold")
        self.font_mono = _first_font(
            self.root, ["SF Mono", "Menlo", "Consolas", "DejaVu Sans Mono"], 11)

        s.configure(".", background=BG, foreground=INK, font=self.font_body)
        s.configure("TFrame", background=BG)
        s.configure("Side.TFrame", background=SIDEBAR)
        s.configure("Card.TFrame", background=PANEL)

        s.configure("TLabel", background=BG, foreground=INK)
        s.configure("Side.TLabel", background=SIDEBAR, foreground=INK)
        s.configure("Title.TLabel", background=BG, foreground=INK,
                    font=self.font_title)
        s.configure("Sub.TLabel", background=BG, foreground=MUTED,
                    font=self.font_small)
        # Section headings in the rail: small, upper case, quiet.
        s.configure("Section.TLabel", background=SIDEBAR, foreground=FAINT,
                    font=self.font_section)
        s.configure("Field.TLabel", background=SIDEBAR, foreground=MUTED,
                    font=self.font_small)
        s.configure("Status.TLabel", background=BG, foreground=MUTED,
                    font=self.font_small)

        s.configure("TEntry", fieldbackground=PANEL, borderwidth=1,
                    relief="solid", padding=6)
        s.map("TEntry", bordercolor=[("focus", ACCENT), ("!focus", LINE)])
        s.configure("TCombobox", fieldbackground=PANEL, background=PANEL,
                    borderwidth=1, relief="solid", padding=5,
                    arrowsize=14)
        s.map("TCombobox", bordercolor=[("focus", ACCENT), ("!focus", LINE)],
              fieldbackground=[("readonly", PANEL)])

        s.configure("TCheckbutton", background=SIDEBAR, foreground=INK,
                    font=self.font_small)
        s.map("TCheckbutton", background=[("active", SIDEBAR)])
        s.configure("Main.TCheckbutton", background=BG, foreground=INK,
                    font=self.font_small)
        s.map("Main.TCheckbutton", background=[("active", BG)])

        s.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                    foreground=INK, rowheight=30, borderwidth=0,
                    font=self.font_body)
        s.configure("Treeview.Heading", background=BG, foreground=MUTED,
                    relief="flat", borderwidth=0, padding=(10, 9),
                    font=self.font_small)
        s.map("Treeview.Heading", background=[("active", STRIPE)])
        s.map("Treeview", background=[("selected", ACCENT_SOFT)],
              foreground=[("selected", INK)])

        s.configure("Thin.Horizontal.TProgressbar", troughcolor=LINE,
                    background=ACCENT, borderwidth=0, thickness=4)

    # -- the window ---------------------------------------------------------
    def _build(self):
        self.root.configure(bg=BG)
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_main()

        # Wired last, deliberately. Inserting the placeholder writes to this
        # variable, and a filter that runs while the window is still being
        # assembled reaches for widgets that do not exist yet. Tk swallows the
        # exception and carries on, so it fails silently.
        self.search_var.trace_add("write", lambda *_: self._apply_filter())

    def _build_sidebar(self):
        """
        The settings rail. Everything you choose before scanning lives here,
        which leaves the whole of the rest of the window for results - the
        part people actually spend their time reading.
        """
        rail = tk.Frame(self.root, background=SIDEBAR, width=278)
        rail.grid(row=0, column=0, sticky="nsw")
        rail.grid_propagate(False)
        rail.columnconfigure(0, weight=1)

        tk.Frame(self.root, background=LINE, width=1).grid(
            row=0, column=0, sticky="nse")

        pad = 20
        row = 0

        brand = tk.Frame(rail, background=SIDEBAR)
        brand.grid(row=row, column=0, sticky="we", padx=pad, pady=(22, 4))
        tk.Label(brand, text=APP_NAME, background=SIDEBAR, foreground=INK,
                 font=self.font_title).pack(side="left")
        tk.Label(brand, text=f"  {VERSION}", background=SIDEBAR,
                 foreground=FAINT, font=self.font_small).pack(side="left",
                                                              pady=(8, 0))
        row += 1
        tk.Label(rail, text="Read-only. Your files are never touched.",
                 background=SIDEBAR, foreground=MUTED, font=self.font_small,
                 wraplength=238, justify="left").grid(
            row=row, column=0, sticky="w", padx=pad, pady=(0, 18))
        row += 1

        # --- drive
        ttk.Label(rail, text="DRIVE", style="Section.TLabel").grid(
            row=row, column=0, sticky="w", padx=pad)
        row += 1
        self.volumes = []
        self.drive_var = tk.StringVar()
        self.drive_box = Dropdown(rail, textvariable=self.drive_var,
                                  command=self._show_drive_detail,
                                  font=self.font_small, background=SIDEBAR)
        self.drive_box.grid(row=row, column=0, sticky="we", padx=pad,
                            pady=(6, 2))
        row += 1
        # The device path lives under the box rather than inside it. A rail
        # this narrow truncates a long label without saying so, and the tail
        # is exactly the part that identifies the drive.
        self.drive_detail = tk.StringVar()
        tk.Label(rail, textvariable=self.drive_detail, background=SIDEBAR,
                 foreground=FAINT, font=self.font_mono, anchor="w",
                 justify="left", wraplength=238).grid(
            row=row, column=0, sticky="we", padx=pad, pady=(0, 6))
        row += 1
        PillButton(rail, "Refresh drives", kind="ghost",
                   font=self.font_small, background=SIDEBAR,
                   command=self._refresh_drives).grid(
            row=row, column=0, sticky="w", padx=pad, pady=(0, 18))
        row += 1

        # --- mode
        ttk.Label(rail, text="MODE", style="Section.TLabel").grid(
            row=row, column=0, sticky="w", padx=pad)
        row += 1
        self.mode_var = tk.StringVar(value="undelete")
        Segmented(rail, self.mode_var,
                  [("undelete", "Undelete  ·  keeps filenames"),
                   ("carve", "Deep scan  ·  any drive")],
                  command=self._toggle_mode).grid(
            row=row, column=0, sticky="we", padx=pad, pady=(8, 14))
        row += 1

        # --- file types, only relevant to deep scan
        self.types_row = tk.Frame(rail, background=SIDEBAR)
        self.types_row.grid(row=row, column=0, sticky="we", padx=pad,
                            pady=(0, 14))
        ttk.Label(self.types_row, text="FILE TYPES",
                  style="Section.TLabel").grid(row=0, column=0, columnspan=3,
                                               sticky="w", pady=(0, 6))
        self.type_vars = {}
        for i, ext in enumerate(sorted(carve.SIGNATURES)):
            var = tk.BooleanVar(value=ext in ("jpg", "png", "pdf"))
            self.type_vars[ext] = var
            CheckBox(self.types_row, ext, var, font=self.font_small,
                     background=SIDEBAR).grid(
                row=1 + i // 3, column=i % 3, sticky="w", padx=(0, 8))
        row += 1

        # --- destination
        ttk.Label(rail, text="RECOVER TO", style="Section.TLabel").grid(
            row=row, column=0, sticky="w", padx=pad)
        row += 1
        self.dest_var = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "Recovered"))
        self._hairline(rail, self.dest_var, self.font_small,
                       background=SIDEBAR).grid(
            row=row, column=0, sticky="we", padx=pad, pady=(6, 6))
        row += 1
        PillButton(rail, "Choose folder...", kind="ghost",
                   font=self.font_small, background=SIDEBAR,
                   command=self._pick_dest).grid(row=row, column=0,
                                                 sticky="w", padx=pad)
        row += 1

        rail.rowconfigure(row, weight=1)
        row += 1

        self.scan_btn = PillButton(rail, "Start scan", command=self._start,
                                   kind="primary", font=self.font_body,
                                   background=SIDEBAR, width=238)
        self.scan_btn.grid(row=row, column=0, padx=pad, pady=(10, 8))
        row += 1
        self.stop_btn = PillButton(rail, "Stop", command=self._stop,
                                   kind="ghost", font=self.font_small,
                                   background=SIDEBAR, width=238)
        self.stop_btn["state"] = "disabled"
        self.stop_btn.grid(row=row, column=0, padx=pad, pady=(0, 22))

        self._refresh_drives()
        self._toggle_mode()

    def _hairline(self, master, variable, font, background):
        """
        An entry with a one-pixel border in our own colour.

        ttk draws entry borders from the platform theme, which on every theme
        this app can reach means a sunken 3D bevel. A flat frame one pixel
        larger than the entry gives a hairline instead, and lights up on
        focus the way a current text field does.
        """
        frame = tk.Frame(master, background=LINE, highlightthickness=0)
        entry = tk.Entry(frame, textvariable=variable, font=font,
                         relief="flat", background=PANEL, foreground=INK,
                         insertbackground=INK, borderwidth=0,
                         highlightthickness=0)
        entry.pack(fill="both", expand=True, padx=1, pady=1, ipady=6, ipadx=8)
        entry.bind("<FocusIn>", lambda e: frame.configure(background=ACCENT),
                   add="+")
        entry.bind("<FocusOut>", lambda e: frame.configure(background=LINE),
                   add="+")
        frame.entry = entry
        return frame

    def _build_main(self):
        main = tk.Frame(self.root, background=BG)
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        # --- header: what this screen is, and the one action that matters
        header = tk.Frame(main, background=BG)
        header.grid(row=0, column=0, sticky="we", padx=26, pady=(24, 6))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Deleted files", style="Title.TLabel").grid(
            row=0, column=0, sticky="w")
        self.recover_btn = PillButton(header, "Recover selected",
                                      command=self._recover, kind="primary",
                                      font=self.font_body)
        self.recover_btn["state"] = "disabled"
        self.recover_btn.grid(row=0, column=1, sticky="e")
        ttk.Label(header, textvariable=self.subtitle_var,
                  style="Sub.TLabel").grid(row=1, column=0, sticky="w",
                                           pady=(2, 0))

        # --- filter bar
        bar = tk.Frame(main, background=BG)
        bar.grid(row=1, column=0, sticky="we", padx=26, pady=(14, 10))
        bar.columnconfigure(0, weight=1)
        self.search_var = tk.StringVar()
        wrap = self._hairline(bar, self.search_var, self.font_body,
                              background=BG)
        wrap.grid(row=0, column=0, sticky="we")
        entry = wrap.entry
        entry.configure(foreground=FAINT)
        entry.bind("<Escape>", lambda e: self.search_var.set(""))
        # Tk has no placeholder, so it is one written in and taken back out
        # again. Kept in a variable the filter knows to ignore.
        self._placeholder = "Search by file name or folder"
        self._placeholder_on = True
        entry.insert(0, self._placeholder)

        def focus_in(_event):
            if self._placeholder_on:
                entry.delete(0, "end")
                entry.configure(foreground=INK)
                self._placeholder_on = False

        def focus_out(_event):
            if not entry.get():
                self._placeholder_on = True
                entry.configure(foreground=FAINT)
                entry.insert(0, self._placeholder)

        entry.bind("<FocusIn>", focus_in)
        entry.bind("<FocusOut>", focus_out)
        self.search_entry = entry
        self.only_good = tk.BooleanVar(value=False)
        CheckBox(bar, "Only likely recoverable", self.only_good,
                 command=self._apply_filter, font=self.font_small,
                 background=BG).grid(row=0, column=1, padx=(14, 0))
        PillButton(bar, "Select all", kind="ghost", font=self.font_small,
                   command=self._select_all).grid(row=0, column=2,
                                                  padx=(10, 0))

        # --- results
        table = tk.Frame(main, background=PANEL, highlightthickness=1,
                         highlightbackground=LINE)
        table.grid(row=2, column=0, sticky="nsew", padx=26)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)

        cols = ("name", "folder", "size", "deleted", "chance", "condition")
        self.tree = ttk.Treeview(table, columns=cols, show="headings",
                                 selectmode="extended")
        headings = {"name": "File name", "folder": "Original folder",
                    "size": "Size", "deleted": "Deleted",
                    "chance": "Chance", "condition": "Condition"}
        widths = {"name": 250, "folder": 240, "size": 90,
                  "deleted": 130, "chance": 80, "condition": 170}
        for c in cols:
            self.tree.heading(c, text=headings[c],
                              command=lambda col=c: self._sort(col))
            self.tree.column(c, width=widths[c],
                             anchor="e" if c in ("size", "chance") else "w")
        self.tree.grid(row=0, column=0, sticky="nsew")

        vs = ThinScrollbar(table, command=self.tree.yview, background=PANEL)
        vs.grid(row=0, column=1, sticky="ns", padx=(0, 3), pady=3)
        self.tree.configure(yscrollcommand=vs.set)

        # Colouring a whole row in red or green shouts. A faint wash carries
        # the same meaning without making the filename - the thing people are
        # actually reading - hard to read.
        self.tree.tag_configure("good", foreground=INK)
        self.tree.tag_configure("partial", foreground=INK, background=WASH_WARN)
        self.tree.tag_configure("gone", foreground=MUTED, background=WASH_BAD)
        self.tree.tag_configure("stripe", background=STRIPE)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_buttons())

        # Shown over the table whenever there is nothing in it. A blank white
        # rectangle tells the user nothing about whether the app is working,
        # still thinking, or finished and empty-handed.
        self.empty = tk.Frame(table, background=PANEL)
        self.empty_title = tk.StringVar()
        self.empty_hint = tk.StringVar()
        tk.Label(self.empty, textvariable=self.empty_title, background=PANEL,
                 foreground=MUTED, font=self.font_body).pack()
        tk.Label(self.empty, textvariable=self.empty_hint, background=PANEL,
                 foreground=FAINT, font=self.font_small, wraplength=420,
                 justify="center").pack(pady=(6, 0))
        self._set_empty("Nothing scanned yet",
                        "Choose a drive on the left, then press Start scan.")
        self._show_empty(True)          # the window opens with nothing in it

        # --- status strip
        status = tk.Frame(main, background=BG)
        status.grid(row=3, column=0, sticky="we", padx=26, pady=(10, 18))
        status.columnconfigure(0, weight=1)
        ttk.Label(status, textvariable=self.status_var,
                  style="Status.TLabel").grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(
            status, length=220, mode="determinate",
            style="Thin.Horizontal.TProgressbar")
        self.progress.grid(row=0, column=1, sticky="e")
        self.progress.grid_remove()          # only while something is running

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
        labels = [self._short_label(v[0]) for v in self.volumes]
        self.drive_box["values"] = labels

        if previous in labels:
            self.drive_box.current(labels.index(previous))
        elif labels:
            # Removable drives sort first - cards and sticks are what people
            # are usually here to recover, so start on one.
            self.drive_box.current(0)
        else:
            self.drive_var.set("")
        self._show_drive_detail()
        return labels

    @staticmethod
    def _short_label(label):
        """Name and facts, without the device path - that goes underneath."""
        parts = label.split(diskio.PART)
        return diskio.PART.join(parts[:2]) if len(parts) > 2 else label

    def _show_drive_detail(self):
        """Show the raw device path for whichever drive is selected."""
        chosen = self.drive_var.get()
        for label, path in self.volumes:
            if self._short_label(label) == chosen:
                self.drive_detail.set(path)
                return
        self.drive_detail.set("")

    def _source_path(self):
        name = self.drive_var.get()
        for display, path in self.volumes:
            if self._short_label(display) == name:
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
        self._refresh_drives()
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
        self.scan_btn["text"] = "Scanning..."
        self.stop_btn["state"] = "normal"
        self.recover_btn["state"] = "disabled"
        self.progress["value"] = 0
        self.progress.grid()

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
            pass
        try:
            return Fat32Volume(disk)
        except ValueError:
            raise ValueError(
                "This drive isn't formatted in a way we can undelete from. "
                "Undelete works on Windows drives (NTFS) and on memory cards, "
                "camera cards and USB sticks (exFAT and FAT32).")

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
        if getattr(self, "_placeholder_on", False):
            term = ""
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
            # Banded rows: the eye loses its place tracking a filename across
            # six columns of a long list otherwise.
            tags = (tag, "stripe") if i % 2 else (tag,)
            self.tree.insert("", "end", iid=str(i), tags=tags, values=(
                f.name,
                f.path or "",
                human_size(f.size),
                human_date(getattr(f, "deleted_at", None)),
                f"{chance}%",
                CONDITION_TEXT.get(getattr(f, "content_check", None), ""),
            ))

        shown = len(self.visible)
        total = len(self.results)
        if shown:
            self._show_empty(False)
        elif total:
            self._set_empty("Nothing matches that search",
                            "Try part of a file name, or clear the box to see "
                            "everything found.")
            self._show_empty(True)
        elif self.scanning:
            self._set_empty("Scanning...", "Files will appear here as they "
                                           "are found.")
            self._show_empty(True)
        else:
            self._set_empty("Nothing found",
                            "Nothing recoverable turned up on this drive. "
                            "Deep scan looks for file contents instead of "
                            "file records, and finds different things.")
            self._show_empty(True)
        extra = "  ·  showing the first 20,000" if shown > 20000 else ""
        if total:
            self.subtitle_var.set(
                f"{shown:,} of {total:,} found{extra}"
                if shown != total else f"{total:,} found{extra}")
        else:
            self.subtitle_var.set("Pick a drive and press Start scan.")
        self.status_var.set(
            f"{shown:,} of {total:,} deleted items shown{extra}"
            if total else "Ready.")
        self._update_buttons()

    def _set_empty(self, title, hint):
        self.empty_title.set(title)
        self.empty_hint.set(hint)

    def _show_empty(self, showing):
        if showing:
            self.empty.place(relx=0.5, rely=0.45, anchor="center")
        else:
            self.empty.place_forget()

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

                target = recovery.write(dest, f.path, f.name, data)
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

    _safe_name = staticmethod(recovery.safe_name)
    _unique = staticmethod(recovery.unique_path)

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
                    self.scan_btn["text"] = "Start scan"
                    self.stop_btn["state"] = "disabled"
                    self.progress["value"] = 100
                    self.progress.grid_remove()
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
