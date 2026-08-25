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
import time
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

# Palette. One deep green doing all the work, everything else a grey. A
# recovery tool is read for hours by someone having a bad day; the colour is
# there to mark what is safe and what is gone, not to decorate.
BG = "#ffffff"          # the working surface
SIDEBAR = "#f7f8f8"     # the settings rail down the left
PANEL = "#ffffff"
INK = "#1b2129"         # primary text
MUTED = "#697585"       # labels and secondary text
FAINT = "#98a2ae"
ACCENT = "#14532d"      # the one strong colour
ACCENT_DEEP = "#0e3b20"  # hover
ACCENT_SOFT = "#eef6f1"  # selected rows, the read-only badge
ACCENT_EDGE = "#cde3d5"  # the border that goes with the wash
GOOD = "#1e8a4c"
WARN = "#8a5b08"
BAD = "#a13029"
LINE = "#e6e9ec"
STRIPE = "#f6f7f8"      # table heading band and footer strip
DISABLED = "#c3ccd8"

# The chance pill: background, text, dot. A word carries better than a
# percentage - "Poor" is understood instantly and "38%" is not - but the word
# is still the score, mapped, never a friendlier version of it.
PILL_STYLES = {
    "excellent": ("#e6f4ea", "#14532d", "#1e8a4c"),
    "good": ("#edf6ef", "#256b40", "#3a9a63"),
    "fair": ("#fdf3e3", "#8a5b08", "#d99a1a"),
    "poor": ("#fdeceb", "#a13029", "#d94a41"),
    "grey": ("#f0f2f4", "#5f6b76", "#9aa5b1"),
}

# Metrics. Every control in the window is built from these numbers, and that
# is the whole trick to a window looking designed rather than assembled: one
# height for anything you can click or type into, one corner radius, one
# gutter, one rail width. Nothing gets to pick its own.
CONTROL_H = 38          # buttons, dropdown, text fields - all identical
RADIUS = 9              # corner radius on every rounded thing
GUTTER = 24             # the margin the whole layout is aligned to
RAIL_W = 270            # the settings rail
FIELD_PAD = 12          # text inset inside a field, so text clears the curve
ACTION_W = 172          # the two buttons over the results table, matched
ROW_H = 45              # one row of results
HEAD_H = 38             # the table's heading band
FOOT_H = 34             # the strip along the bottom of the table

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
    # "25 Aug 14:02" rather than "2026-08-25 14:02": in a column of deletions
    # from the last few days, the year is noise and the day is the answer.
    return dt.strftime("%d %b %H:%M") if dt else ""


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

    HEIGHT = CONTROL_H
    RADIUS = RADIUS
    MIN_WIDTH = 112         # so a two-word button is never a stub

    def __init__(self, master, text, command=None, kind="primary",
                 width=None, font=None, **kw):
        self.kind = kind
        self.command = command
        self._state = "normal"
        self._text = text
        self._font = font or ("TkDefaultFont", 12)

        measure = tkfont.Font(font=self._font)
        self._width = width or max(measure.measure(text) + 40,
                                   self.MIN_WIDTH)
        background = kw.pop("background", None) or (
            SIDEBAR if kind == "sidebar" else BG)

        super().__init__(master, width=self._width, height=self.HEIGHT,
                         highlightthickness=0, bd=0, background=background,
                         cursor="arrow", takefocus=1, **kw)
        self._draw()
        # Redrawn at whatever width the layout actually gives it. Without
        # this a button stretched by `sticky="we"` painted its pill at the
        # width its own label needed and left a gap to the cell edge, which
        # is what made a column of buttons look ragged.
        self.bind("<Configure>", lambda e: self._draw())
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
        width = self.winfo_width() if self.winfo_width() > 1 else self._width
        _round_rect(self, 1, 1, width - 1, self.HEIGHT - 1, self.RADIUS,
                    fill=fill, outline=outline)
        self.create_text(width // 2, self.HEIGHT // 2, text=self._text,
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

    BOX = 18

    def __init__(self, master, text, variable, command=None, font=None,
                 background=SIDEBAR, **kw):
        self.variable = variable
        self.command = command
        self._font = font or ("TkDefaultFont", 11)
        measure = tkfont.Font(font=self._font)
        width = self.BOX + 10 + measure.measure(text) + 4
        super().__init__(master, width=width, height=self.BOX + 8,
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
        top = 4
        fill = ACCENT if on else PANEL
        edge = ACCENT if on else (ACCENT if hover else LINE)
        _round_rect(self, 1, top, self.BOX, top + self.BOX - 1, 6,
                    fill=fill, outline=edge)
        if on:
            x, y = 5, top + 9
            self.create_line(x, y, x + 3, y + 4, x + 9, y - 5,
                             fill="#ffffff", width=2, capstyle="round",
                             joinstyle="round")
        self.create_text(self.BOX + 10, top + 8, text=self._text, anchor="w",
                         fill=INK, font=self._font)

    def _toggle(self, _event=None):
        self.variable.set(not self.variable.get())
        self._draw()
        if self.command:
            self.command()


class LinkButton(tk.Label):
    """
    A button that looks like a line of text.

    "Refresh drives" and "Choose folder..." are things you do occasionally
    and they sit next to the control they act on. Drawing them as full
    buttons gives them the same weight as "Start scan", which is the one
    thing on this screen that matters.
    """

    def __init__(self, master, text, command, font=None, background=SIDEBAR,
                 **kw):
        super().__init__(master, text=text, background=background,
                         foreground=MUTED, font=font, cursor="arrow", **kw)
        self.command = command
        self.bind("<Button-1>", lambda e: self.command())
        self.bind("<Enter>", lambda e: self.configure(foreground=ACCENT))
        self.bind("<Leave>", lambda e: self.configure(foreground=MUTED))

    def invoke(self):
        self.command()


class Badge(tk.Canvas):
    """
    The read-only promise, stated once at the top of the rail.

    This is the single most important thing the program does, and the place
    it belongs is where someone looks first - not buried in a menu or an
    about box.
    """

    def __init__(self, master, text, font, background=SIDEBAR, **kw):
        self._text = text
        self._font = font
        super().__init__(master, highlightthickness=0, bd=0,
                         background=background, **kw)
        self.bind("<Configure>", lambda e: self._draw())

    def _draw(self):
        self.delete("all")
        width = self.winfo_width()
        if width <= 1:
            return
        measure = tkfont.Font(font=self._font)
        lines = self._wrap(measure, width - 52)
        height = max(len(lines) * (measure.metrics("linespace") + 2) + 22, 46)
        self.configure(height=height)
        _round_rect(self, 1, 1, width - 1, height - 1, RADIUS,
                    fill=ACCENT_SOFT, outline=ACCENT_EDGE)
        self._shield(16, height // 2)
        y = (height - len(lines) * (measure.metrics("linespace") + 2)) // 2 + 1
        for line in lines:
            self.create_text(38, y, text=line, anchor="nw", fill=ACCENT,
                             font=self._font)
            y += measure.metrics("linespace") + 2

    def _wrap(self, measure, room):
        lines, current = [], ""
        for word in self._text.split():
            trial = f"{current} {word}".strip()
            if current and measure.measure(trial) > room:
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)
        return lines

    def _shield(self, x, y):
        """A shield with a tick in it, drawn rather than shipped as a file."""
        self.create_polygon(x, y - 9, x + 7, y - 6, x + 7, y + 1,
                            x, y + 9, x - 7, y + 1, x - 7, y - 6,
                            fill="", outline=ACCENT, width=1.4, smooth=False)
        self.create_line(x - 3, y, x - 1, y + 3, x + 4, y - 4, fill=ACCENT,
                         width=1.6, capstyle="round", joinstyle="round")


class ModeCard(tk.Canvas):
    """
    One choice of scan mode, with the sentence that explains it.

    Undelete and Deep scan are not two settings of one control - they are two
    different programs with different results, and the difference is worth a
    line of explanation each. A radio button with a label cannot carry that;
    a card can.
    """

    HEIGHT = 62

    def __init__(self, master, title, blurb, chosen=False, command=None,
                 title_font=None, blurb_font=None, background=SIDEBAR, **kw):
        self._title = title
        self._blurb = blurb
        self._chosen = chosen
        self._title_font = title_font
        self._blurb_font = blurb_font
        self.command = command
        super().__init__(master, height=self.HEIGHT, highlightthickness=0,
                         bd=0, background=background, takefocus=1, **kw)
        self.bind("<Configure>", lambda e: self._draw())
        for sequence in ("<Button-1>", "<Return>", "<space>"):
            self.bind(sequence, lambda e: self.command and self.command())
        self.bind("<Enter>", lambda e: self._draw(hover=True))
        self.bind("<Leave>", lambda e: self._draw())

    def set_chosen(self, chosen):
        self._chosen = chosen
        self._draw()

    def _draw(self, hover=False):
        self.delete("all")
        width = self.winfo_width()
        if width <= 1:
            return
        edge = ACCENT_EDGE if self._chosen else (ACCENT_EDGE if hover else LINE)
        _round_rect(self, 1, 1, width - 1, self.HEIGHT - 1, RADIUS,
                    fill=ACCENT_SOFT if self._chosen else PANEL, outline=edge)
        self.create_text(FIELD_PAD + 2, 20, text=self._title, anchor="w",
                         fill=INK, font=self._title_font)
        self.create_text(FIELD_PAD + 2, 41, text=self._blurb, anchor="w",
                         fill=MUTED, font=self._blurb_font)
        x, y = width - 22, self.HEIGHT // 2
        if self._chosen:
            self.create_oval(x - 5, y - 5, x + 5, y + 5, fill=ACCENT,
                             outline=ACCENT)
        else:
            self.create_oval(x - 6, y - 6, x + 6, y + 6, fill=PANEL,
                             outline="#c8cfd6")


def file_icon(canvas, kind, x, y, colour=MUTED):
    """
    A small line drawing for a file type, drawn on `canvas` centred at x, y.

    Fourteen pixels of line art rather than an emoji: emoji come out in full
    colour on macOS and as a hollow box on some Linux setups, and neither is
    what this table wants beside a filename.
    """
    if kind == "image":
        canvas.create_rectangle(x - 7, y - 6, x + 7, y + 6, outline=colour)
        canvas.create_line(x - 5, y + 4, x - 1, y - 1, x + 2, y + 2,
                           x + 5, y - 2, x + 6, y + 4, fill=colour)
        canvas.create_oval(x + 1, y - 5, x + 4, y - 2, outline=colour)
    elif kind == "video":
        canvas.create_rectangle(x - 7, y - 5, x + 7, y + 5, outline=colour)
        canvas.create_polygon(x - 2, y - 3, x + 3, y, x - 2, y + 3,
                              fill=colour, outline=colour)
    elif kind == "audio":
        canvas.create_rectangle(x - 2, y - 7, x + 2, y + 1, outline=colour)
        canvas.create_arc(x - 5, y - 3, x + 5, y + 5, start=200, extent=140,
                          style="arc", outline=colour)
        canvas.create_line(x, y + 5, x, y + 7, fill=colour)
    elif kind == "sheet":
        canvas.create_rectangle(x - 7, y - 6, x + 7, y + 6, outline=colour)
        canvas.create_line(x - 7, y - 2, x + 7, y - 2, fill=colour)
        canvas.create_line(x - 2, y - 6, x - 2, y + 6, fill=colour)
    elif kind == "archive":
        canvas.create_rectangle(x - 6, y - 6, x + 6, y + 6, outline=colour)
        canvas.create_line(x, y - 6, x, y - 3, fill=colour)
        canvas.create_line(x, y - 1, x, y + 2, fill=colour)
    else:                                    # a plain document
        canvas.create_polygon(x - 5, y - 7, x + 2, y - 7, x + 5, y - 4,
                              x + 5, y + 7, x - 5, y + 7,
                              fill="", outline=colour)
        canvas.create_line(x - 2, y - 1, x + 2, y - 1, fill=colour)
        canvas.create_line(x - 2, y + 2, x + 2, y + 2, fill=colour)


ICON_KINDS = {
    "image": ("jpg", "jpeg", "png", "gif", "bmp", "heic", "cr2", "nef", "arw",
              "tif", "tiff", "webp", "raw", "dng"),
    "video": ("mp4", "mov", "m4v", "avi", "mkv", "3gp", "mts", "wmv"),
    "audio": ("mp3", "m4a", "wav", "aac", "flac", "aiff", "ogg"),
    "sheet": ("xlsx", "xls", "csv", "numbers", "ods"),
    "archive": ("zip", "rar", "7z", "gz", "tar", "sparsebundle", "dmg"),
}


def icon_for(name):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    for kind, extensions in ICON_KINDS.items():
        if ext in extensions:
            return kind
    return "doc"


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

    HEIGHT = CONTROL_H
    RADIUS = RADIUS

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
        # Bound once for the life of the widget, not once per open. Tk keeps
        # every `add="+"` binding forever, so binding on each open left a
        # stale handler behind: the next click on the box ran the widget
        # binding that opens the popup and then the leftover toplevel binding
        # that closes it, and the drive list stopped opening at all after the
        # first drive had been picked.
        top = self.winfo_toplevel()
        top.bind("<Button-1>", self._click_elsewhere, add="+")
        top.bind("<Escape>", lambda e: self._close(), add="+")
        top.bind("<Configure>", self._window_moved, add="+")

    # -- closed state -------------------------------------------------------
    def _draw(self, hover=False):
        self.delete("all")
        width = self.winfo_width()
        if width <= 1:
            return
        _round_rect(self, 1, 1, width - 1, self.HEIGHT - 1, self.RADIUS,
                    fill=PANEL, outline=ACCENT if hover else LINE)
        measure = tkfont.Font(font=self._font)
        text = self.variable.get() or "No drives found"
        room = width - 48
        while text and measure.measure(text) > room:
            text = text[:-2]
        if text != (self.variable.get() or "No drives found"):
            text += "\u2026"
        self.create_text(FIELD_PAD, self.HEIGHT // 2, text=text, anchor="w",
                         fill=INK if self.variable.get() else FAINT,
                         font=self._font)
        x, y = width - 19, self.HEIGHT // 2 - 2
        self.create_line(x - 4, y, x, y + 4, x + 4, y, fill=MUTED,
                         width=1.6, capstyle="round", joinstyle="round")

    # -- open state ---------------------------------------------------------
    def _open(self):
        if self._popup or not self._values:
            return
        popup = tk.Toplevel(self)
        popup.overrideredirect(True)
        popup.configure(background=LINE)
        # As wide as the box it drops from, so the list reads as the box
        # opening rather than as a separate window landing on top of it.
        popup.geometry(f"{max(self.winfo_width(), 160)}x1"
                       f"+{self.winfo_rootx()}"
                       f"+{self.winfo_rooty() + self.HEIGHT + 4}")
        inner = tk.Frame(popup, background=PANEL)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        chosen = self.variable.get()
        for value in self._values:
            row = tk.Label(inner, text=("  " if value != chosen else "\u2713 ")
                           + value, anchor="w", background=PANEL,
                           foreground=INK, font=self._font,
                           padx=FIELD_PAD - 4, pady=8)
            row.pack(fill="x")
            row.bind("<Enter>", lambda e, r=row: r.configure(
                background=ACCENT_SOFT))
            row.bind("<Leave>", lambda e, r=row: r.configure(background=PANEL))
            row.bind("<Button-1>", lambda e, v=value: self._choose(v))

        popup.update_idletasks()
        popup.geometry(f"{max(self.winfo_width(), 160)}"
                       f"x{inner.winfo_reqheight() + 2}")
        popup.lift()
        self._popup = popup

    # No grab while the list is open. A local grab makes Tk discard every
    # click outside the popup, so clicking anywhere else in the window did
    # nothing at all - the list stayed up and the app looked frozen. Letting
    # the clicks through and closing on them is what a menu does anyway.
    def _click_elsewhere(self, event):
        """Close the open list when the click lands anywhere but this box."""
        # The click that opens the popup travels on to the toplevel too;
        # acting on that one would make the list flash and vanish.
        if self._popup and event.widget is not self:
            self._close()

    def _window_moved(self, event):
        # The popup is its own window at fixed screen coordinates, so it
        # would sit where the list used to be if the window moved or resized.
        if self._popup and event.widget is self.winfo_toplevel():
            self._close()

    def _close(self):
        if self._popup:
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


class Field(tk.Canvas):
    """
    A text field with rounded corners and a hairline border.

    Tk's Entry is a rectangle and cannot be anything else, and the platform
    themes draw it with a sunken 3D bevel - the other thing, along with the
    square grey button, that dates a window instantly. Drawing the field on a
    canvas and dropping a borderless Entry into the middle of it gives the
    rounded, one-pixel-bordered box the rest of the window is built from, at
    exactly the same height as every button.

    Exposes `.entry` so the placeholder and key bindings stay where they were.
    """

    HEIGHT = CONTROL_H
    RADIUS = RADIUS

    def __init__(self, master, textvariable, font, background=BG, icon=None,
                 **kw):
        super().__init__(master, height=self.HEIGHT, highlightthickness=0,
                         bd=0, background=background, **kw)
        self._focused = False
        self._icon = icon
        self._text_left = FIELD_PAD + (22 if icon else 0)
        self.entry = tk.Entry(self, textvariable=textvariable, font=font,
                              relief="flat", background=PANEL, foreground=INK,
                              insertbackground=INK, borderwidth=0,
                              highlightthickness=0)
        self._slot = self.create_window(self._text_left, self.HEIGHT // 2,
                                        window=self.entry, anchor="w",
                                        height=self.HEIGHT - 12)
        self.bind("<Configure>", lambda e: self._draw())
        # The canvas is bigger than the entry inside it, so the curved ends
        # are clickable too. Clicking a text field anywhere should put the
        # cursor in it.
        self.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.entry.bind("<FocusIn>", self._focus(True), add="+")
        self.entry.bind("<FocusOut>", self._focus(False), add="+")

    def _focus(self, on):
        def handler(_event):
            self._focused = on
            self._draw()
        return handler

    def _draw(self):
        width = self.winfo_width()
        if width <= 1:
            return
        # Only the border, by tag. `delete("all")` takes the embedded Entry
        # with it - a window is a canvas item like any other - and the field
        # redraws as an empty box with the text still sitting in the variable.
        self.delete("box")
        _round_rect(self, 1, 1, width - 1, self.HEIGHT - 1, self.RADIUS,
                    fill=PANEL, outline=ACCENT if self._focused else LINE,
                    tags="box")
        if self._icon == "folder":
            self._folder(FIELD_PAD + 7, self.HEIGHT // 2)
        self.tag_lower("box")
        self.itemconfigure(self._slot,
                           width=max(width - self._text_left - FIELD_PAD, 10))
        self.coords(self._slot, self._text_left, self.HEIGHT // 2)

    def _folder(self, x, y):
        self.create_polygon(x - 7, y + 5, x - 7, y - 5, x - 2, y - 5,
                            x, y - 3, x + 7, y - 3, x + 7, y + 5,
                            fill="", outline=FAINT, width=1.2, tags="box")


class InfoBox(tk.Canvas):
    """
    The chosen drive's device path, with what the drive is beside it.

    The path is the part that identifies a drive beyond doubt - two cards can
    both be called NO NAME - so it gets its own box in monospace rather than
    being truncated into the dropdown above it.
    """

    HEIGHT = CONTROL_H

    def __init__(self, master, path_var, meta_var, mono, small,
                 background=SIDEBAR, **kw):
        super().__init__(master, height=self.HEIGHT, highlightthickness=0,
                         bd=0, background=background, **kw)
        self.path_var, self.meta_var = path_var, meta_var
        self._mono, self._small = mono, small
        self.bind("<Configure>", lambda e: self._draw())
        for var in (path_var, meta_var):
            var.trace_add("write", lambda *_: self._draw())

    def _draw(self):
        self.delete("all")
        width = self.winfo_width()
        if width <= 1:
            return
        _round_rect(self, 1, 1, width - 1, self.HEIGHT - 1, RADIUS,
                    fill=PANEL, outline=LINE)
        meta = self.meta_var.get()
        room = width - FIELD_PAD * 2
        if meta:
            measure = tkfont.Font(font=self._small)
            meta = ResultTable._fit(meta, width // 2 - FIELD_PAD, self._small)
            self.create_text(width - FIELD_PAD, self.HEIGHT // 2, text=meta,
                             anchor="e", fill=FAINT, font=self._small)
            room -= measure.measure(meta) + 10
        self.create_text(FIELD_PAD, self.HEIGHT // 2,
                         text=ResultTable._fit(self.path_var.get(), room,
                                               self._mono),
                         anchor="w", fill=MUTED, font=self._mono)


class ResultTable(tk.Frame):
    """
    The results, drawn rather than tabulated.

    ttk's Treeview colours a whole row or nothing, so the chance of getting a
    file back could only ever be a word in a column - and a wash of red
    across the filename is exactly what makes a list like this hard to read.
    Drawing the rows gives each one a pill, an icon and a checkbox, and none
    of it costs the reader legibility of the thing they came for: the name.

    Only the rows on screen are drawn, so a deep scan that turns up eighty
    thousand files scrolls at the same speed as one that turns up eight.

    Keeps the slice of the Treeview API the window already used - the rows
    are addressed by index as strings - so selection and recovery did not
    have to learn a new vocabulary.
    """

    COLUMNS = (
        # key, heading, width (0 = share what is left), alignment
        ("check", "", 46, "w"),
        ("name", "FILE NAME", 0, "w"),
        ("folder", "ORIGINAL FOLDER", 0, "w"),
        ("size", "SIZE", 96, "e"),
        ("deleted", "DELETED", 124, "w"),
        ("chance", "CHANCE", 132, "e"),
    )

    def __init__(self, master, fonts, on_select=None, on_sort=None, **kw):
        super().__init__(master, background=PANEL, highlightthickness=1,
                         highlightbackground=LINE, **kw)
        self.fonts = fonts
        self.on_select = on_select
        self.on_sort = on_sort
        self._rows = []
        self._selected = set()
        self._offset = 0.0
        self._sort_key = None
        self._sort_reverse = False

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self.head = tk.Canvas(self, height=HEAD_H, highlightthickness=0, bd=0,
                              background=STRIPE)
        self.head.grid(row=0, column=0, columnspan=2, sticky="we")
        self.head.bind("<Configure>", lambda e: self._draw_head())
        self.head.bind("<Button-1>", self._head_click)

        self.body = tk.Canvas(self, highlightthickness=0, bd=0,
                              background=PANEL)
        self.body.grid(row=1, column=0, sticky="nsew")
        self.body.bind("<Configure>", lambda e: self.redraw())
        self.body.bind("<Button-1>", self._body_click)
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.body.bind(sequence, self._wheel)

        self.bar = ThinScrollbar(self, command=self.yview, background=PANEL)
        self.bar.grid(row=1, column=1, sticky="ns", padx=(0, 4), pady=4)

        self.foot = tk.Frame(self, background=STRIPE, height=FOOT_H)
        self.foot.grid(row=2, column=0, columnspan=2, sticky="we")
        self.foot.grid_propagate(False)
        self.foot.columnconfigure(0, weight=1)
        tk.Frame(self.foot, background=LINE, height=1).grid(
            row=0, column=0, columnspan=2, sticky="we")
        self.count_var = tk.StringVar()
        self.promise_var = tk.StringVar(
            value="read-only  \u00b7  source drive untouched")
        tk.Label(self.foot, textvariable=self.count_var, background=STRIPE,
                 foreground=MUTED, font=fonts["small"]).grid(
            row=1, column=0, sticky="w", padx=FIELD_PAD + 2)
        tk.Label(self.foot, textvariable=self.promise_var, background=STRIPE,
                 foreground=FAINT, font=fonts["small"]).grid(
            row=1, column=1, sticky="e", padx=FIELD_PAD + 2)

    # -- geometry -----------------------------------------------------------
    def _layout(self, width):
        """Left edge and width of every column, for a table this wide."""
        fixed = sum(c[2] for c in self.COLUMNS)
        share = max(width - fixed - 8, 240)
        flexible = [c for c in self.COLUMNS if c[2] == 0]
        # The filename gets more of the spare room than the folder: it is
        # what people search by and what they read down the list.
        weights = {"name": 0.56, "folder": 0.44}
        spans, x = {}, 0
        for key, _, fixed_width, align in self.COLUMNS:
            span = fixed_width or int(share * weights.get(key, 1 / len(flexible)))
            spans[key] = (x, span, align)
            x += span
        return spans

    # -- the heading band ---------------------------------------------------
    def _draw_head(self):
        self.head.delete("all")
        width = self.head.winfo_width()
        if width <= 1:
            return
        self.head.create_line(0, HEAD_H - 1, width, HEAD_H - 1, fill=LINE)
        spans = self._layout(width)
        for key, title, _, align in self.COLUMNS:
            if not title:
                continue
            x, span, _ = spans[key]
            arrow = ""
            if key == self._sort_key:
                arrow = "  \u2193" if self._sort_reverse else "  \u2191"
            if align == "e":
                self.head.create_text(x + span - FIELD_PAD, HEAD_H // 2,
                                      text=title + arrow, anchor="e",
                                      fill=MUTED, font=self.fonts["section"])
            else:
                self.head.create_text(x + FIELD_PAD, HEAD_H // 2,
                                      text=title + arrow, anchor="w",
                                      fill=MUTED, font=self.fonts["section"])

    def _head_click(self, event):
        if not self.on_sort:
            return
        spans = self._layout(self.head.winfo_width())
        for key, title, _, _ in self.COLUMNS:
            if not title:
                continue
            x, span, _ = spans[key]
            if x <= event.x < x + span:
                self.on_sort(key)
                return

    def mark_sorted(self, key, reverse):
        self._sort_key, self._sort_reverse = key, reverse
        self._draw_head()

    # -- rows ---------------------------------------------------------------
    def set_rows(self, rows, keep_selection=False):
        """`rows` is a list of dicts, one per line, already formatted."""
        self._rows = rows
        if not keep_selection:
            self._selected = set()
        else:
            self._selected = {i for i in self._selected if i < len(rows)}
        self._offset = min(self._offset, max(0, len(rows) - 1))
        self.redraw()
        self._announce()

    def redraw(self):
        self.body.delete("all")
        width, height = self.body.winfo_width(), self.body.winfo_height()
        if width <= 1 or height <= 1:
            return
        spans = self._layout(width)
        first = int(self._offset)
        y = -int((self._offset - first) * ROW_H)
        index = first
        while y < height and index < len(self._rows):
            self._draw_row(index, y, width, spans)
            y += ROW_H
            index += 1
        self._sync_bar()

    def _draw_row(self, index, y, width, spans):
        row = self._rows[index]
        chosen = index in self._selected
        c = self.body
        if chosen:
            c.create_rectangle(0, y, width, y + ROW_H, fill=ACCENT_SOFT,
                               outline="")
        c.create_line(0, y + ROW_H - 1, width, y + ROW_H - 1, fill=LINE)
        middle = y + ROW_H // 2

        # the tick box
        x = spans["check"][0] + 17
        _round_rect(c, x - 8, middle - 8, x + 9, middle + 9, 5,
                    fill=ACCENT if chosen else PANEL,
                    outline=ACCENT if chosen else "#c8cfd6")
        if chosen:
            c.create_line(x - 4, middle, x - 1, middle + 4, x + 5, middle - 4,
                          fill="#ffffff", width=2, capstyle="round",
                          joinstyle="round")

        # name, with the icon for its type
        x, span, _ = spans["name"]
        file_icon(c, row["icon"], x + FIELD_PAD + 7, middle, FAINT)
        c.create_text(x + FIELD_PAD + 24, middle,
                      text=self._fit(row["name"], span - FIELD_PAD - 32,
                                     self.fonts["body"]),
                      anchor="w", fill=INK, font=self.fonts["body"])

        # the folder it came from, in mono - a path reads as a path
        x, span, _ = spans["folder"]
        c.create_text(x + FIELD_PAD, middle,
                      text=self._fit(row["folder"], span - FIELD_PAD * 2,
                                     self.fonts["mono"]),
                      anchor="w", fill=MUTED, font=self.fonts["mono"])

        x, span, _ = spans["size"]
        c.create_text(x + span - FIELD_PAD, middle, text=row["size"],
                      anchor="e", fill=INK, font=self.fonts["small"])

        x, span, _ = spans["deleted"]
        c.create_text(x + FIELD_PAD, middle, text=row["deleted"], anchor="w",
                      fill=MUTED, font=self.fonts["small"])

        x, span, _ = spans["chance"]
        self._pill(x + span - FIELD_PAD, middle, row["chance"], row["kind"])

    def _pill(self, right, middle, label, kind):
        fill, ink, dot = PILL_STYLES.get(kind, PILL_STYLES["grey"])
        measure = tkfont.Font(font=self.fonts["small"])
        width = measure.measure(label) + 34
        left = right - width
        _round_rect(self.body, left, middle - 11, right, middle + 11, 11,
                    fill=fill, outline="")
        self.body.create_oval(left + 10, middle - 3, left + 16, middle + 3,
                              fill=dot, outline=dot)
        self.body.create_text(left + 22, middle, text=label, anchor="w",
                              fill=ink, font=self.fonts["small"])

    @staticmethod
    def _fit(text, room, font):
        """Cut a string to fit, with an ellipsis so the cut is visible."""
        measure = tkfont.Font(font=font)
        if room <= 8 or measure.measure(text) <= room:
            return text
        while text and measure.measure(text + "\u2026") > room:
            text = text[:-1]
        return text + "\u2026"

    # -- selection ----------------------------------------------------------
    def _body_click(self, event):
        index = int(self._offset) + (event.y + int(
            (self._offset - int(self._offset)) * ROW_H)) // ROW_H
        if not 0 <= index < len(self._rows):
            return
        if index in self._selected:
            self._selected.discard(index)
        else:
            self._selected.add(index)
        self.redraw()
        self._announce()

    def selection(self):
        return tuple(str(i) for i in sorted(self._selected))

    def selection_set(self, *items):
        flat = []
        for item in items:
            flat.extend(item if isinstance(item, (list, tuple)) else [item])
        self._selected = {int(i) for i in flat if int(i) < len(self._rows)}
        self.redraw()
        self._announce()

    def select_all(self):
        self._selected = set(range(len(self._rows)))
        self.redraw()
        self._announce()

    def get_children(self):
        return tuple(str(i) for i in range(len(self._rows)))

    def _announce(self):
        shown = len(self._rows)
        picked = len(self._selected)
        self.count_var.set(
            f"{shown:,} shown  \u00b7  {picked:,} selected" if picked
            else f"{shown:,} shown")
        if self.on_select:
            self.on_select()

    # -- scrolling ----------------------------------------------------------
    def _rows_on_screen(self):
        return max(self.body.winfo_height() // ROW_H, 1)

    def _max_offset(self):
        return max(0.0, len(self._rows) - self._rows_on_screen())

    def yview(self, *args):
        if not args:
            return self._fractions()
        how = args[0]
        if how == "moveto":
            self._offset = float(args[1]) * max(len(self._rows), 1)
        elif how == "scroll":
            step = int(args[1])
            self._offset += step * (self._rows_on_screen()
                                    if args[2] == "pages" else 1)
        self._offset = max(0.0, min(self._offset, self._max_offset()))
        self.redraw()
        return None

    def yview_scroll(self, step, what="units"):
        return self.yview("scroll", step, what)

    def _fractions(self):
        total = max(len(self._rows), 1)
        first = self._offset / total
        last = min((self._offset + self._rows_on_screen()) / total, 1.0)
        return first, last

    def _sync_bar(self):
        self.bar.set(*self._fractions())

    def _wheel(self, event):
        if getattr(event, "num", 0) in (4, 5):
            step = -1 if event.num == 4 else 1
        elif abs(event.delta) >= 120:
            step = -event.delta // 120
        else:
            step = -event.delta
        self.yview("scroll", int(step), "units")
        return "break"


# What the Chance column says. The word is the score, mapped - never a
# friendlier version of it - and where the file's own first bytes have
# something to say, they say it instead: "Overwritten" is a fact, and
# dressing it up as a low percentage helps nobody.
def chance_pill(found):
    """(label, pill style) for one result."""
    verdict = getattr(found, "content_check", None)
    if verdict == signatures.MISMATCH:
        return "Overwritten", "grey"
    if verdict == signatures.BLANK:
        return "Empty space", "grey"
    if verdict == signatures.MOVED:
        return "In Trash", "good"
    if verdict == signatures.IN_USE:
        return "Space reused", "fair"
    chance = found.chance if found.chance is not None else 100
    if chance >= 90:
        return "Excellent", "excellent"
    if chance >= 70:
        return "Good", "good"
    if chance >= 40:
        return "Fair", "fair"
    return "Poor", "poor"


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
        self.status_var = tk.StringVar(value="Nothing scanned yet")
        self._scan_started = None
        self._scan_ended = None
        self._scan_took = 0.0
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
        s.configure("Status.TLabel", background=SIDEBAR, foreground=FAINT,
                    font=self.font_small)

        # Nothing else in this window is a ttk widget any more: the table,
        # the fields, the buttons, the dropdown and the checkboxes are all
        # drawn by this app, because no platform theme will round a corner
        # or draw a one-pixel border. The progress bar is the last holdout.
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

        Every control in it is the full width of the rail and the same
        height. A rail of controls that each stop at a different place reads
        as an unfinished form; one flush column reads as a panel.
        """
        rail = tk.Frame(self.root, background=SIDEBAR, width=RAIL_W)
        rail.grid(row=0, column=0, sticky="nsw")
        rail.grid_propagate(False)
        rail.columnconfigure(0, weight=1)
        rail.rowconfigure(0, weight=1)

        tk.Frame(self.root, background=LINE, width=1).grid(
            row=0, column=0, sticky="nse")

        # The settings scroll; the scan button does not. In deep-scan mode
        # the file-type list makes the rail taller than a 720-pixel window,
        # and the button that starts the scan is the last thing that should
        # be pushed off the bottom of the screen to make room.
        view = tk.Canvas(rail, background=SIDEBAR, highlightthickness=0, bd=0)
        view.grid(row=0, column=0, sticky="nsew")
        self._rail_bar = ThinScrollbar(rail, command=view.yview,
                                       background=SIDEBAR)
        self._rail_bar.grid(row=0, column=1, sticky="ns", pady=6)
        view.configure(yscrollcommand=self._rail_bar.set)
        content = tk.Frame(view, background=SIDEBAR)
        slot = view.create_window(0, 0, window=content, anchor="nw")
        view.bind("<Configure>",
                  lambda e: view.itemconfigure(slot, width=e.width))
        content.bind("<Configure>",
                     lambda e: view.configure(scrollregion=view.bbox("all")))
        self._rail_view = view

        pad = GUTTER - 4
        text_width = RAIL_W - pad * 2
        content.columnconfigure(0, weight=1)
        self._rail_row = 0

        def place(widget, top=0, bottom=0, fill=True):
            widget.grid(row=self._rail_row, column=0,
                        sticky="we" if fill else "",
                        # The scrollbar lives in its own column beside the
                        # canvas, so the right pad gives that width back -
                        # otherwise everything in the scrolling part sits
                        # narrower than the button below it.
                        padx=(pad, pad - ThinScrollbar.WIDTH),
                        pady=(top, bottom))
            self._rail_row += 1
            return widget

        def section(text, top):
            """A heading, always the same size and always the same gap."""
            place(tk.Label(content, text=text, background=SIDEBAR,
                           foreground=FAINT, font=self.font_section,
                           anchor="w"), top=top, bottom=8)

        # --- brand and the promise the whole program rests on
        brand = tk.Frame(content, background=SIDEBAR)
        tk.Label(brand, text=APP_NAME, background=SIDEBAR, foreground=ACCENT,
                 font=self.font_title).pack(side="left")
        tk.Label(brand, text=VERSION, background=SIDEBAR, foreground=FAINT,
                 font=self.font_small).pack(side="left", padx=(7, 0),
                                            pady=(9, 0))
        place(brand, top=22, bottom=14)
        place(Badge(content,
                    "Read-only mode. Your source drive is never written to.",
                    self.font_small, background=SIDEBAR), bottom=20)

        # --- drive
        section("DRIVE", top=0)
        self.volumes = []
        self.drive_var = tk.StringVar()
        self.drive_box = place(
            Dropdown(content, textvariable=self.drive_var,
                     command=self._show_drive_detail, font=self.font_small,
                     background=SIDEBAR), bottom=8)
        # The device path lives under the box rather than inside it. A rail
        # this narrow truncates a long label without saying so, and the tail
        # is exactly the part that identifies the drive.
        self.drive_detail = tk.StringVar()
        self.drive_meta = tk.StringVar()
        self.detail_box = place(
            InfoBox(content, self.drive_detail, self.drive_meta,
                    self.font_mono, self.font_small, background=SIDEBAR),
            bottom=6)
        place(LinkButton(content, "Refresh drives", self._refresh_drives,
                         font=self.font_small, background=SIDEBAR),
              bottom=20, fill=False)

        # --- mode
        section("SCAN MODE", top=0)
        self.mode_var = tk.StringVar(value="undelete")
        self.mode_cards = {}
        for value, title, blurb, gap in (
                ("undelete", "Undelete", "Fast. Keeps original filenames.", 8),
                ("carve", "Deep scan", "Slow. Any drive, names may be lost.",
                 20)):
            card = ModeCard(content, title, blurb,
                            chosen=value == self.mode_var.get(),
                            title_font=self.font_body,
                            blurb_font=self.font_small,
                            background=SIDEBAR,
                            command=lambda v=value: self._choose_mode(v))
            self.mode_cards[value] = place(card, bottom=gap)

        # --- file types, only relevant to deep scan
        self.types_row = tk.Frame(content, background=SIDEBAR)
        tk.Label(self.types_row, text="FILE TYPES", background=SIDEBAR,
                 foreground=FAINT, font=self.font_section, anchor="w").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.type_vars = {}
        for column in range(3):
            self.types_row.columnconfigure(column, weight=1, uniform="types")
        for i, ext in enumerate(sorted(carve.SIGNATURES)):
            var = tk.BooleanVar(value=ext in ("jpg", "png", "pdf"))
            self.type_vars[ext] = var
            CheckBox(self.types_row, ext, var, font=self.font_small,
                     background=SIDEBAR).grid(
                row=1 + i // 3, column=i % 3, sticky="w", pady=1)
        place(self.types_row, bottom=20)

        # --- destination
        section("RECOVER TO", top=0)
        self.dest_var = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "Recovered"))
        place(Field(content, self.dest_var, self.font_small,
                    background=SIDEBAR, icon="folder"), bottom=6)
        place(LinkButton(content, "Choose folder\u2026", self._pick_dest,
                         font=self.font_small, background=SIDEBAR),
              bottom=18, fill=False)

        # --- the footer, outside the scrolling part
        footer = tk.Frame(rail, background=SIDEBAR)
        footer.grid(row=1, column=0, columnspan=2, sticky="we")
        footer.columnconfigure(0, weight=1)
        tk.Frame(footer, background=LINE, height=1).grid(
            row=0, column=0, sticky="we")
        self.scan_btn = PillButton(footer, "Start scan", command=self._start,
                                   kind="primary", font=self.font_body,
                                   background=SIDEBAR)
        self.scan_btn.grid(row=1, column=0, sticky="we", padx=pad,
                           pady=(16, 8))
        self.stop_btn = PillButton(footer, "Stop", command=self._stop,
                                   kind="ghost", font=self.font_small,
                                   background=SIDEBAR)
        self.stop_btn.grid(row=2, column=0, sticky="we", padx=pad,
                           pady=(0, 8))
        self.stop_btn["state"] = "disabled"
        self.stop_btn.grid_remove()          # only while a scan is running
        self.progress = ttk.Progressbar(
            footer, mode="determinate", style="Thin.Horizontal.TProgressbar")
        self.progress.grid(row=3, column=0, sticky="we", padx=pad,
                           pady=(0, 8))
        self.progress.grid_remove()
        tk.Label(footer, textvariable=self.status_var, background=SIDEBAR,
                 foreground=FAINT, font=self.font_small,
                 wraplength=text_width).grid(row=4, column=0, pady=(0, 18))

        self._bind_wheel(rail)
        self._refresh_drives()
        self._toggle_mode()

    def _bind_wheel(self, widget):
        """
        Make the wheel scroll the rail wherever the pointer is inside it.

        Tk sends a wheel event to the widget under the pointer and no
        further - it does not travel up to the parent - so a rail whose
        canvas alone is bound only scrolls in the gaps between the controls.
        """
        widget.bind("<MouseWheel>", self._wheel_rail, add="+")
        widget.bind("<Button-4>", self._wheel_rail, add="+")     # X11
        widget.bind("<Button-5>", self._wheel_rail, add="+")
        for child in widget.winfo_children():
            self._bind_wheel(child)

    def _wheel_rail(self, event):
        if getattr(event, "num", 0) in (4, 5):
            step = -1 if event.num == 4 else 1
        elif abs(event.delta) >= 120:            # Windows sends multiples
            step = -event.delta // 120
        else:                                    # macOS sends small numbers
            step = -event.delta
        self._rail_view.yview_scroll(int(step), "units")
        return "break"

    def _choose_mode(self, value):
        self.mode_var.set(value)
        for name, card in self.mode_cards.items():
            card.set_chosen(name == value)
        self._toggle_mode()

    def _build_main(self):
        main = tk.Frame(self.root, background=BG)
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        # --- header: what this screen is, and the one action that matters
        header = tk.Frame(main, background=BG)
        header.grid(row=0, column=0, sticky="we", padx=GUTTER, pady=(26, 2))
        header.columnconfigure(0, weight=1)
        header.rowconfigure(0, weight=1)
        header.rowconfigure(1, weight=1)
        ttk.Label(header, text="Deleted files", style="Title.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.subtitle_var,
                  style="Sub.TLabel").grid(row=1, column=0, sticky="w",
                                           pady=(4, 0))
        # Spanning both rows centres it against the title and its subtitle
        # together, rather than leaving it hanging off the top line.
        self.recover_btn = PillButton(header, "Recover selected",
                                      command=self._recover, kind="primary",
                                      font=self.font_body, width=ACTION_W)
        self.recover_btn["state"] = "disabled"
        self.recover_btn.grid(row=0, column=1, rowspan=2, sticky="e")

        # --- filter bar
        bar = tk.Frame(main, background=BG)
        bar.grid(row=1, column=0, sticky="we", padx=GUTTER, pady=(20, 12))
        bar.columnconfigure(0, weight=1)
        self.search_var = tk.StringVar()
        wrap = Field(bar, self.search_var, self.font_body, background=BG)
        wrap.grid(row=0, column=0, sticky="we")
        entry = wrap.entry
        entry.configure(foreground=FAINT)
        entry.bind("<Escape>", lambda e: self.search_var.set(""))
        # Tk has no placeholder, so it is one written in and taken back out
        # again. Kept in a variable the filter knows to ignore.
        self._placeholder = "Filter by name or folder"
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

        # add="+" both times: the field lights its own border on focus, and
        # a plain bind here would silently replace that binding rather than
        # join it.
        entry.bind("<FocusIn>", focus_in, add="+")
        entry.bind("<FocusOut>", focus_out, add="+")
        self.search_entry = entry
        self.only_good = tk.BooleanVar(value=False)
        CheckBox(bar, "Only likely recoverable", self.only_good,
                 command=self._apply_filter, font=self.font_small,
                 background=BG).grid(row=0, column=1, padx=(16, 16))
        self.select_all_btn = PillButton(
            bar, "Select all", kind="ghost", font=self.font_small,
            width=ACTION_W, command=self._select_all)
        self.select_all_btn.grid(row=0, column=2)

        # --- results
        self.tree = ResultTable(main, {"body": self.font_body,
                                       "small": self.font_small,
                                       "mono": self.font_mono,
                                       "section": self.font_section},
                                on_select=self._update_buttons,
                                on_sort=self._sort)
        self.tree.grid(row=2, column=0, sticky="nsew", padx=GUTTER)

        # Shown over the table whenever there is nothing in it. A blank white
        # rectangle tells the user nothing about whether the app is working,
        # still thinking, or finished and empty-handed.
        self.empty = tk.Frame(self.tree.body, background=PANEL)
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

        # --- the admin warning, which only appears when it applies
        self.warning = tk.Frame(main, background=BG)
        self.warning.grid(row=3, column=0, sticky="we", padx=GUTTER,
                          pady=(12, 18))
        mark = tk.Canvas(self.warning, width=16, height=16,
                         highlightthickness=0, bd=0, background=BG)
        mark.create_polygon(8, 1, 15, 14, 1, 14, fill="", outline=WARN,
                            width=1.4)
        mark.create_line(8, 5, 8, 9, fill=WARN, width=1.4)
        mark.create_line(8, 11, 8, 11.5, fill=WARN, width=1.6)
        mark.grid(row=0, column=0, padx=(0, 8))
        self.warning_var = tk.StringVar()
        tk.Label(self.warning, textvariable=self.warning_var, background=BG,
                 foreground=MUTED, font=self.font_small, anchor="w").grid(
            row=0, column=1, sticky="w")
        self.warning.grid_remove()

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
            self.warning_var.set(
                "Not running with admin rights \u2014 raw device scans will "
                "fail. " + ("Restart as Administrator to enable scanning."
                            if sys.platform == "win32"
                            else "Restart with  sudo  to enable scanning."))
            self.warning.grid()

    def _toggle_mode(self):
        for name, card in getattr(self, "mode_cards", {}).items():
            card.set_chosen(name == self.mode_var.get())
        if self.mode_var.get() == "carve":
            self.types_row.grid()
        else:
            self.types_row.grid_remove()
        # The rail just got shorter or taller. If everything fits again,
        # scroll back to the top - otherwise the settings sit half off the
        # top of a rail with empty space below them.
        view = getattr(self, "_rail_view", None)
        if view is not None:
            view.update_idletasks()
            first, last = view.yview()
            if last - first >= 1.0:
                view.yview_moveto(0)

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
                parts = label.split(diskio.PART)
                self.drive_meta.set(parts[1] if len(parts) > 1 else "")
                return
        self.drive_detail.set("")
        self.drive_meta.set("")

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
            # Not os.makedirs: under sudo this is where the recovery folder
            # first gets created, and it has to end up belonging to the user.
            recovery.make_folder(dest)
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
        self.tree.set_rows([])
        self.stop_flag.clear()
        self.scanning = True
        self.scan_btn["state"] = "disabled"
        self.scan_btn["text"] = "Scanning\u2026"
        self.stop_btn["state"] = "normal"
        self.stop_btn.grid()
        self.recover_btn["state"] = "disabled"
        self.progress["value"] = 0
        self.progress.grid()
        self._scan_started = time.time()

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
        # Which files were ticked, by identity rather than by row number.
        # Filtering and sorting both renumber the rows, and someone who has
        # ticked forty photographs and then types in the search box should
        # not come back to find the ticks gone.
        picked = {id(self.visible[int(i)]) for i in self.tree.selection()
                  if int(i) < len(self.visible)} if self.visible else set()

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

        rows = []
        for f in self.visible[:20000]:
            label, kind = chance_pill(f)
            folder = f.path or ""
            if folder in recovery.NOT_A_FOLDER:
                folder = "unknown (carved)"
            rows.append({
                "icon": icon_for(f.name),
                "name": f.name,
                "folder": folder,
                "size": human_size(f.size),
                "deleted": human_date(getattr(f, "deleted_at", None)) or "\u2013",
                "chance": label,
                "kind": kind,
            })
        self.tree.set_rows(rows)
        if picked:
            still_here = [str(i) for i, f in enumerate(self.visible[:20000])
                          if id(f) in picked]
            if still_here:
                self.tree.selection_set(still_here)

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
        mode = "Undelete" if self.mode_var.get() == "undelete" else "Deep"
        drive = self.drive_var.get().split(diskio.PART)[0].strip() or "this drive"
        if total and self.scanning:
            self.subtitle_var.set(
                f"{mode} scan of {drive} running \u2014 "
                f"{total:,} found so far{extra}")
        elif total:
            found = (f"{total:,} recoverable entries found" if shown == total
                     else f"showing {shown:,} of {total:,} found")
            self.subtitle_var.set(
                f"{mode} scan of {drive} finished \u2014 {found}{extra}")
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
            self.empty.place(relx=0.5, rely=0.42, anchor="center")
        else:
            self.empty.place_forget()

    def _sort(self, column):
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column, self.sort_reverse = column, False
        self._mark_sorted_column()
        self._apply_filter()

    def _mark_sorted_column(self):
        """
        Put an arrow on the column being sorted by.

        A table that reorders itself with no indication of what it just did
        looks like it lost the results rather than sorted them.
        """
        self.tree.mark_sorted(self.sort_column, self.sort_reverse)

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
        self.tree.select_all()

    def _update_buttons(self):
        # Deliberately not gated on the scan being finished. Deep scan streams
        # results in as it goes, and those carry their own data - making
        # someone watch a long scan finish before they can save a file that is
        # already in hand is just a locked door. Anything that would need to
        # read the drive again is turned away in _recover instead, where we
        # know which files were picked.
        picked = len(self.tree.selection())
        self.recover_btn["state"] = "normal" if picked else "disabled"
        # The button says how many, so nobody has to count the ticks back up
        # the list before pressing the one control that writes anything.
        self.recover_btn["text"] = (f"Recover {picked:,} selected" if picked
                                    else "Recover selected")

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
                    self.scan_btn["text"] = "Rescan drive"
                    self.stop_btn["state"] = "disabled"
                    self.stop_btn.grid_remove()
                    self.progress["value"] = 100
                    self.progress.grid_remove()
                    self._scan_took = time.time() - (self._scan_started or 0)
                    self._scan_ended = time.time()
                    self._update_buttons()
                    self._say_when_the_last_scan_was()
        except queue.Empty:
            pass
        if not self.scanning and self._scan_ended:
            self._say_when_the_last_scan_was()
        self.root.after(120, self._drain)

    def _say_when_the_last_scan_was(self):
        """
        The line under the scan button, once there is something to say.

        Someone who has been staring at a scan for twenty minutes wants to
        know it finished and how long it took, not to be told "Ready." as if
        nothing had happened.
        """
        ago = max(0, int(time.time() - self._scan_ended))
        if ago < 60:
            when = "just now" if ago < 10 else f"{ago}s ago"
        elif ago < 3600:
            when = f"{ago // 60} min ago"
        else:
            when = f"{ago // 3600}h ago"
        took = (f"{self._scan_took:.1f} s" if self._scan_took < 60
                else f"{self._scan_took / 60:.0f} min")
        self.status_var.set(f"last scan {when}  \u00b7  {took}")

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
