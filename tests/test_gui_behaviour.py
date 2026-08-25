"""
Tests for the bits of the GUI that decide what the user is allowed to do.

Only the decision logic is covered - no clicking, no screenshots. The rule
that gates the Recover button is worth testing because getting it wrong
produces a window full of results with no way to act on them, which reads to
the user as "this tool is broken" rather than "this button has a condition".

WHY THE GUARD IS A SUBPROCESS
-----------------------------
`import tkinter` succeeding proves nothing. On some macOS builds the import
works and creating a window calls abort(), which kills the interpreter
outright - not an exception, so try/except cannot save the test run. The only
safe way to ask "does Tk actually work here" is to ask a process we can afford
to lose.
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def tk_works():
    try:
        result = subprocess.run(
            [sys.executable, "-c",
             "import tkinter; r = tkinter.Tk(); r.destroy()"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        return result.returncode == 0
    except Exception:
        return False


HAVE_WORKING_TK = tk_works()

if HAVE_WORKING_TK:
    import tkinter as tk
    import app as appmod
    import carve
    import ntfs
    import signatures


class NoDialogs:
    """
    Stops any test opening a real modal dialog.

    Learned the hard way: one test fell through into a code path that calls
    messagebox.showerror, and the suite went from nine seconds to five and a
    half minutes waiting for a dialog nobody could see, let alone dismiss.
    Recording what would have been shown is more useful for assertions
    anyway.
    """

    def silence_dialogs(self):
        self.dialogs = []
        for name, answer in (("showinfo", None), ("showerror", None),
                             ("showwarning", None), ("askyesno", False),
                             ("askokcancel", False)):
            original = getattr(appmod.messagebox, name)
            self.addCleanup(setattr, appmod.messagebox, name, original)
            setattr(appmod.messagebox, name,
                    (lambda title, text, _n=name, _a=answer, **kw:
                     (self.dialogs.append((_n, title, text)), _a)[1]))


@unittest.skipUnless(HAVE_WORKING_TK, "no working Tk on this interpreter")
class RecoverButtonTests(NoDialogs, unittest.TestCase):
    """
    The button was gated on `not self.scanning`, which is wrong for deep scan:
    that mode streams results in while it runs, so the list filled up while
    the button stayed dead for the whole scan.
    """

    def setUp(self):
        self.silence_dialogs()
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.app = appmod.App(self.root)

    def deleted(self, name="photo.jpg"):
        """An undelete result: no data in hand, needs a read to recover."""
        return ntfs.DeletedFile(name=name, path="Photos", size=1234,
                                chance=100, runs=[(100, 3)], is_dir=False,
                                content_check=signatures.MATCH)

    def carved(self, name="recovered_jpg_00001.jpg"):
        """A deep-scan result: records where the file is, not what it holds."""
        return carve.CarvedFile(name, "jpg", 4096, 900)

    def show(self, files, scanning=False):
        self.app.scanning = scanning
        self.app.events.put(("batch", files))
        self.app._drain()

    def select_first(self):
        children = self.app.tree.get_children()
        self.assertTrue(children, "nothing was listed")
        self.app.tree.selection_set(children[0])
        self.app._update_buttons()

    def state(self):
        return str(self.app.recover_btn["state"])

    def test_nothing_selected_means_nothing_to_recover(self):
        self.show([self.deleted()])
        self.assertEqual(self.state(), "disabled")

    def test_selecting_a_finished_result_enables_recovery(self):
        self.show([self.deleted()])
        self.select_first()
        self.assertEqual(self.state(), "normal")

    def test_deep_scan_results_can_be_recovered_while_the_scan_runs(self):
        """
        The regression. Results stream in during a deep scan, so gating the
        button on the scan being finished left the list full and the button
        dead for the whole run.
        """
        self.show([self.carved()], scanning=True)
        self.select_first()
        self.assertEqual(self.state(), "normal",
                         "deep-scan results were listed but could not be "
                         "recovered while the scan was still running")

    def test_the_button_survives_a_finished_scan(self):
        self.show([self.deleted()], scanning=True)
        self.app.events.put(("done", None))
        self.app._drain()
        self.assertFalse(self.app.scanning)
        self.select_first()
        self.assertEqual(self.state(), "normal")


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_WORKING_TK, "no working Tk on this interpreter")
class DialogSafetyTests(NoDialogs, unittest.TestCase):
    def setUp(self):
        self.silence_dialogs()
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.app = appmod.App(self.root)

    def test_a_scan_with_no_drive_chosen_complains_instead_of_hanging(self):
        self.app.drive_var.set("")
        self.app._start()
        self.assertTrue(self.dialogs, "no dialog was raised")
        self.assertEqual(self.dialogs[0][0], "showerror")
        self.assertFalse(self.app.scanning)


@unittest.skipUnless(HAVE_WORKING_TK, "no working Tk on this interpreter")
class PresentationTests(NoDialogs, unittest.TestCase):
    """
    The parts of the window that carry meaning rather than decoration.
    """

    def setUp(self):
        self.silence_dialogs()
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.app = appmod.App(self.root)

    def result(self, name="photo.jpg", chance=100,
               check=None):
        return ntfs.DeletedFile(name=name, path="Photos", size=1234,
                                chance=chance, runs=[(1, 1)], is_dir=False,
                                content_check=check or signatures.MATCH)

    def test_an_empty_table_explains_itself(self):
        """A blank white rectangle says nothing about what happened."""
        self.assertTrue(self.app.empty_title.get())
        self.assertTrue(self.app.empty.winfo_manager(),
                        "the empty state is not shown when there is nothing")

    def test_the_empty_state_goes_away_once_there_are_results(self):
        self.app._add([self.result()])
        self.assertFalse(self.app.empty.winfo_manager())

    def test_a_search_that_matches_nothing_says_so_specifically(self):
        self.app._add([self.result()])
        self.app._placeholder_on = False
        self.app.search_var.set("nothing-like-this")
        self.assertTrue(self.app.empty.winfo_manager())
        self.assertIn("search", self.app.empty_title.get().lower())

    def test_the_search_placeholder_is_not_treated_as_a_search(self):
        """
        Tk has no placeholder, so it is real text in the box. If the filter
        took it literally, the results would vanish before anyone typed.
        """
        self.app._add([self.result()])
        self.assertEqual(self.app.search_entry.get(), self.app._placeholder)
        self.assertEqual(len(self.app.visible), 1,
                         "the placeholder was filtered against")

    def test_the_drive_path_is_shown_apart_from_the_label(self):
        """
        The rail is too narrow for the full label, and the device path is the
        part that identifies the drive - so it gets its own line.
        """
        if not self.app.volumes:
            self.skipTest("no drives on this machine")
        label, path = self.app.volumes[0]
        self.assertNotIn(path, self.app._short_label(label))
        self.assertEqual(self.app.drive_detail.get(), path)

    def test_the_selected_drive_still_resolves_to_its_device(self):
        if not self.app.volumes:
            self.skipTest("no drives on this machine")
        self.assertEqual(self.app._source_path(), self.app.volumes[0][1])

    def test_progress_is_hidden_when_nothing_is_running(self):
        self.assertFalse(self.app.progress.winfo_manager())

    def test_a_button_is_bound_for_keyboard_use(self):
        """
        A hand-drawn button still has to behave like one: reachable by Tab,
        and pressed by Space or Return.

        Only the parts that are ours are asserted - that the bindings are
        registered and that takefocus is set. Whether Tk then delivers a
        keypress is Tk's business, and testing it needs a mapped, focused,
        on-screen window, which is a lot of flashing for no extra confidence.
        """
        button = appmod.PillButton(self.root, "Go", command=lambda: None)
        self.assertTrue(button.cget("takefocus"))
        bound = button.bind()
        for sequence in ("<Key-Return>", "<Key-space>", "<Button-1>"):
            self.assertIn(sequence, bound, f"{sequence} is not bound")

    def test_pressing_a_button_runs_its_command(self):
        pressed = []
        button = appmod.PillButton(self.root, "Go",
                                   command=lambda: pressed.append(True))
        button._press(None)
        self.assertEqual(len(pressed), 1)

    def test_a_disabled_button_does_nothing_when_pressed(self):
        pressed = []
        button = appmod.PillButton(self.root, "Go",
                                   command=lambda: pressed.append(True))
        button["state"] = "disabled"
        button._press(None)
        self.assertEqual(pressed, [],
                         "a disabled button ran its command anyway")

    def test_the_mode_control_drives_the_variable(self):
        self.app.mode_var.set("undelete")
        self.assertTrue(self.app.types_row.winfo_manager() == "",
                        "file types are only meaningful for deep scan")


@unittest.skipUnless(HAVE_WORKING_TK, "no working Tk on this interpreter")
class StartupTests(unittest.TestCase):
    """
    Tk reports an exception raised inside a callback and then carries on, so
    a broken callback leaves a working-looking window and a line of output
    nobody reads.

    Building the window fired the search filter before the widgets it reads
    existed, and it went unnoticed exactly that way. This catches the whole
    class of it.
    """

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.raised = []
        self.root.report_callback_exception = (
            lambda exc, value, tb: self.raised.append(f"{exc.__name__}: {value}"))

    def test_building_the_window_raises_nothing(self):
        appmod.App(self.root)
        self.root.update()
        self.assertEqual(self.raised, [],
                         "an exception was raised while building the window")

    def test_typing_in_the_search_box_raises_nothing(self):
        app = appmod.App(self.root)
        app._placeholder_on = False
        for text in ("a", "ab", "abc", ""):
            app.search_var.set(text)
            self.root.update()
        self.assertEqual(self.raised, [])

    def test_switching_mode_raises_nothing(self):
        app = appmod.App(self.root)
        for mode in ("carve", "undelete", "carve"):
            app.mode_var.set(mode)
            app._toggle_mode()
            self.root.update()
        self.assertEqual(self.raised, [])

    def test_sorting_every_column_raises_nothing(self):
        app = appmod.App(self.root)
        app._add([ntfs.DeletedFile(name="a.jpg", path="P", size=1, chance=100,
                                   runs=[(1, 1)], is_dir=False,
                                   content_check=signatures.MATCH)])
        for column in ("name", "folder", "size", "deleted", "chance",
                       "condition"):
            app._sort(column)
            app._sort(column)
            self.root.update()
        self.assertEqual(self.raised, [])


@unittest.skipUnless(HAVE_WORKING_TK, "no working Tk on this interpreter")
class LayoutTests(NoDialogs, unittest.TestCase):
    """
    The window's alignment, asserted in pixels.

    Spacing is the first thing to drift and the last thing anyone notices in
    a diff: a control given its own width, a button that keeps the size of
    its own label, a gutter typed as 26 where everything else says 24. These
    measure the finished window instead of trusting the source.

    They need the window on the screen - an unmapped Tk window reports every
    widget as one pixel by one - so unlike the rest of the suite these show
    it.
    """

    def setUp(self):
        self.silence_dialogs()
        self.root = tk.Tk()
        self.addCleanup(self.root.destroy)
        self.app = appmod.App(self.root)

    def show(self, size="1180x720"):
        self.root.geometry(size + "+60+60")
        self.root.deiconify()
        self.root.update()
        self.root.update_idletasks()

    def rect(self, widget):
        return (widget.winfo_rootx() - self.root.winfo_rootx(),
                widget.winfo_rooty() - self.root.winfo_rooty(),
                widget.winfo_width(), widget.winfo_height())

    def rail_controls(self, kinds=None):
        """Everything in the settings rail you can click or type into."""
        kinds = kinds or (appmod.PillButton, appmod.Dropdown, appmod.Field,
                          appmod.InfoBox, appmod.ModeCard)
        found = []

        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, kinds):
                    found.append(child)
                walk(child)

        walk(self.app.drive_box.master.master)      # the scrolling body
        found.extend(w for w in (self.app.scan_btn, self.app.stop_btn)
                     if isinstance(w, kinds))
        # A control that is not on screen has no geometry to compare - Tk
        # reports it as one pixel square. Stop is hidden until a scan runs.
        return [w for w in found if w.winfo_ismapped()]

    def test_every_control_in_the_rail_is_the_same_width(self):
        """
        Heights differ on purpose - a mode card carries a line of
        explanation under its name - but nothing in the rail may be a
        different width from the rest.
        """
        self.show()
        widths = {w.winfo_width() for w in self.rail_controls()}
        self.assertEqual(len(widths), 1,
                         f"the rail has {len(widths)} widths: {sorted(widths)}")

    def test_the_single_line_controls_are_all_one_height(self):
        self.show()
        singles = self.rail_controls((appmod.PillButton, appmod.Dropdown,
                                      appmod.Field, appmod.InfoBox))
        self.assertTrue(singles)
        self.assertEqual({w.winfo_height() for w in singles},
                         {appmod.CONTROL_H})

    def test_every_control_in_the_rail_starts_at_the_same_edge(self):
        self.show()
        left = {w.winfo_rootx() for w in self.rail_controls()}
        self.assertEqual(len(left), 1, "the rail is not flush down its left")

    def test_the_buttons_over_the_table_match_and_line_up_with_it(self):
        """
        Both are the same size, and their right edge is the table's right
        edge - the one vertical line the eye follows down that side.
        """
        self.show()
        recover = self.rect(self.app.recover_btn)
        select = self.rect(self.app.select_all_btn)
        table = self.rect(self.app.tree)
        self.assertEqual(recover[2:], select[2:])
        self.assertEqual(recover[0], select[0])
        self.assertEqual(recover[0] + recover[2], table[0] + table[2])

    def test_the_search_field_is_a_button_height_tall(self):
        self.show()
        self.assertEqual(self.app.search_entry.master.winfo_height(),
                         appmod.CONTROL_H)
        self.assertEqual(self.app.recover_btn.winfo_height(),
                         appmod.CONTROL_H)

    def test_the_fields_show_what_is_in_them(self):
        """
        The bug this covers: the field redrew its border with
        `delete("all")`, which deleted the Entry sitting inside it as well -
        a canvas window is an item like any other. The recovery folder was
        still in the variable and still used by the scan, but the box on
        screen was blank, so it read as "no folder chosen".
        """
        self.show()
        self.assertTrue(self.app.search_entry.winfo_ismapped(),
                        "the search field lost its entry")
        dest_field = self.app.dest_var
        entry = self._dest_entry()
        self.assertTrue(entry.winfo_ismapped(),
                        "the recovery folder field lost its entry")
        self.assertEqual(entry.get(), dest_field.get())
        self.assertTrue(dest_field.get(), "there should be a default folder")

    def test_a_field_keeps_its_entry_through_a_resize(self):
        self.show()
        entry = self._dest_entry()
        self.show("1100x680")
        self.root.update()
        self.assertTrue(entry.winfo_ismapped())
        self.assertEqual(entry.get(), self.app.dest_var.get())

    def _dest_entry(self):
        """The Entry inside the RECOVER TO field."""
        for field in self._fields():
            if field.entry.get() == self.app.dest_var.get():
                return field.entry
        self.fail("no field is showing the recovery folder")

    def _fields(self):
        found = []

        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, appmod.Field):
                    found.append(child)
                walk(child)

        walk(self.root)
        return found

    def test_the_scan_button_survives_the_smallest_window(self):
        """
        Deep-scan mode adds the file-type list to the rail, which makes it
        taller than the window. The button that starts the scan is the last
        thing that may be pushed off the bottom, so the settings scroll and
        it does not.
        """
        self.app.mode_var.set("carve")
        self.app._toggle_mode()
        self.show("1000x600")
        _, top, _, height = self.rect(self.app.scan_btn)
        self.assertLessEqual(top + height, 600,
                             "Start scan is off the bottom of the window")
        _, top, _, height = self.rect(self.app.stop_btn)
        self.assertLessEqual(top + height, 600)
        first, last = self.app._rail_view.yview()
        self.assertLess(last - first, 1.0, "the rail should be scrolling")

    def test_the_rail_scrolls_under_the_pointer_anywhere_in_it(self):
        """Tk delivers a wheel event to one widget, not to its parents."""
        self.app.mode_var.set("carve")
        self.app._toggle_mode()
        self.show("1000x600")
        self.assertTrue(self.app.drive_box.bind("<MouseWheel>"))
        self.assertTrue(self.app.scan_btn.bind("<MouseWheel>"))
        before = self.app._rail_view.yview()[0]
        self.app._rail_view.yview_scroll(3, "units")
        self.root.update()
        self.assertGreater(self.app._rail_view.yview()[0], before)


class ResultTableTests(NoDialogs, unittest.TestCase):
    """
    The results table, which this app draws itself.

    ttk's Treeview colours a whole row or nothing, so the chance of getting a
    file back could only ever be a word in a column. These cover the parts
    that a table widget used to do for us and now have to be right here:
    ticking rows, keeping those ticks, and not drawing eighty thousand rows
    to show fifteen.
    """

    def setUp(self):
        self.silence_dialogs()
        self.root = tk.Tk()
        self.addCleanup(self.root.destroy)
        self.app = appmod.App(self.root)
        self.root.geometry("1180x720+60+60")
        self.root.deiconify()
        self.root.update()

    def add(self, count=6, chance=100, check=signatures.MATCH):
        found = [ntfs.DeletedFile(
            record=i, name=f"file{i:03}.jpg", path="\\Photos", size=1024 * i,
            runs=[(i, 1)], chance=chance, deleted_at=None) for i in range(count)]
        for f in found:
            f.content_check = check
        self.app._add(found)
        self.root.update()
        return found

    def click_row(self, index, x=30):
        """Click a row the way a person does, in the tick column."""
        y = index * appmod.ROW_H + appmod.ROW_H // 2
        self.app.tree.body.event_generate("<Button-1>", x=x, y=y)
        self.root.update()

    def test_clicking_a_row_ticks_it_and_clicking_again_unticks(self):
        self.add(4)
        self.click_row(1)
        self.assertEqual(self.app.tree.selection(), ("1",))
        self.click_row(1)
        self.assertEqual(self.app.tree.selection(), ())

    def test_the_recover_button_counts_what_is_ticked(self):
        self.add(5)
        self.click_row(0)
        self.click_row(2)
        self.assertEqual(self.app.recover_btn["text"], "Recover 2 selected")
        self.assertEqual(self.app.recover_btn["state"], "normal")

    def test_ticks_survive_a_search(self):
        """
        Rows are addressed by number and a search renumbers them. Ticking
        forty photographs and then narrowing the list must not quietly throw
        the ticks away - the Recover button acts on them.
        """
        found = self.add(6)
        self.click_row(3)
        picked = found[3]
        self.app._placeholder_on = False
        self.app.search_var.set(picked.name[:5])
        self.root.update()
        self.assertTrue(self.app.tree.selection(), "the tick was dropped")
        still = self.app.visible[int(self.app.tree.selection()[0])]
        self.assertIs(still, picked)

    def test_ticks_survive_a_sort(self):
        found = self.add(6)
        self.click_row(0)
        picked = self.app.visible[0]
        self.app._sort("size")
        self.root.update()
        chosen = [self.app.visible[int(i)]
                  for i in self.app.tree.selection()]
        self.assertEqual(chosen, [picked])
        self.assertIn(picked, found)

    def test_select_all_ticks_every_row_that_is_showing(self):
        self.add(7)
        self.app._select_all()
        self.assertEqual(len(self.app.tree.selection()), 7)

    def test_only_the_rows_on_screen_are_drawn(self):
        """
        A deep scan can turn up tens of thousands of files. Drawing them all
        and letting a scrollbar move the viewport is how a canvas table
        becomes unusable.
        """
        self.add(4000)
        drawn = len(self.app.tree.body.find_all())
        self.assertLess(drawn, 400,
                        f"{drawn} canvas items for a screen of ~15 rows")
        self.assertEqual(len(self.app.tree.get_children()), 4000)

    def test_the_pill_says_what_the_evidence_says(self):
        """
        The word is the score, mapped. Where the file's own first bytes have
        something to say, they say it instead of a number.
        """

        class Result:
            def __init__(self, chance, check):
                self.chance, self.content_check = chance, check

        cases = [
            (100, signatures.MATCH, ("Excellent", "excellent")),
            (75, signatures.MATCH, ("Good", "good")),
            (50, signatures.MATCH, ("Fair", "fair")),
            (10, signatures.MATCH, ("Poor", "poor")),
            (0, signatures.MISMATCH, ("Overwritten", "grey")),
            (0, signatures.BLANK, ("Empty space", "grey")),
            (100, signatures.MOVED, ("In Trash", "good")),
            (50, signatures.IN_USE, ("Space reused", "fair")),
        ]
        for chance, check, expected in cases:
            self.assertEqual(appmod.chance_pill(Result(chance, check)),
                             expected, f"{chance}% / {check}")

    def test_a_carved_file_says_where_it_came_from_plainly(self):
        found = self.add(1)
        found[0].path = "(no folder - carved)"
        self.app._apply_filter()
        self.assertEqual(self.app.tree._rows[0]["folder"], "unknown (carved)")

    def test_the_icon_follows_the_file_type(self):
        for name, kind in (("holiday.JPG", "image"), ("clip.mov", "video"),
                           ("song.mp3", "audio"), ("books.xlsx", "sheet"),
                           ("stuff.zip", "archive"), ("notes.pdf", "doc"),
                           ("noextension", "doc")):
            self.assertEqual(appmod.icon_for(name), kind, name)


class DrawnWidgetTests(unittest.TestCase):
    """
    The window uses drawn replacements for the ttk widgets whose look comes
    from the platform theme - checkbox, scrollbar, dropdown. They have to
    keep behaving like the things they replaced.
    """

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def test_a_checkbox_drives_its_variable_both_ways(self):
        var = tk.BooleanVar(value=False)
        fired = []
        box = appmod.CheckBox(self.root, "Only good", var,
                              command=lambda: fired.append(True))
        box._toggle()
        self.assertTrue(var.get())
        self.assertEqual(len(fired), 1)
        box._toggle()
        self.assertFalse(var.get())

    def test_a_checkbox_follows_the_variable_when_set_elsewhere(self):
        var = tk.BooleanVar(value=False)
        appmod.CheckBox(self.root, "x", var)
        var.set(True)                     # no exception from the redraw
        self.root.update()

    def test_the_dropdown_keeps_the_combobox_api_the_app_uses(self):
        var = tk.StringVar()
        drop = appmod.Dropdown(self.root, textvariable=var)
        drop["values"] = ["first", "second", "third"]
        self.assertEqual(drop["values"], ["first", "second", "third"])
        drop.current(1)
        self.assertEqual(var.get(), "second")
        self.assertEqual(drop.current(), 1)

    def test_the_dropdown_survives_being_given_nothing(self):
        var = tk.StringVar()
        drop = appmod.Dropdown(self.root, textvariable=var)
        drop["values"] = []
        drop.current(0)                   # out of range, must not raise
        self.assertEqual(drop.current(), -1)

    def test_choosing_tells_the_caller_straight_away(self):
        """
        Not via the virtual event: Tk puts those through the event loop, so a
        caller has no guarantee of having heard anything by the time the
        choice returns. The direct callback is what the window relies on.
        """
        var = tk.StringVar()
        heard = []
        drop = appmod.Dropdown(self.root, textvariable=var,
                               command=lambda: heard.append(var.get()))
        drop["values"] = ["one", "two"]
        drop._choose("two")
        self.assertEqual(heard, ["two"])

    def show_window(self):
        """
        Tk does not deliver a synthetic click to a window that is not on the
        screen, so any test that clicks rather than calling a method has to
        put the window up first. The other tests keep it withdrawn.
        """
        self.root.geometry("240x120+80+80")
        self.root.deiconify()
        self.root.update()

    def test_the_dropdown_opens_again_after_a_choice(self):
        """
        The bug this covers: the list opened once, and every click on the box
        after the first drive had been picked did nothing. Picking a drive,
        scanning, then wanting to look at another drive is the ordinary way
        this tool is used, so the box has to survive being used.
        """
        var = tk.StringVar()
        drop = appmod.Dropdown(self.root, textvariable=var)
        drop.pack()
        drop["values"] = ["first", "second"]
        self.show_window()
        for _ in range(3):
            drop.event_generate("<Button-1>", x=10, y=10)
            self.root.update()
            self.assertIsNotNone(drop._popup, "the list stopped opening")
            drop._choose("second")
            self.root.update()
            self.assertIsNone(drop._popup)

    def test_a_click_elsewhere_closes_the_open_list(self):
        """
        And leaves the rest of the window usable. While the popup held a
        grab, Tk threw away every click outside it, so the window looked
        frozen until something was picked.
        """
        var = tk.StringVar()
        drop = appmod.Dropdown(self.root, textvariable=var)
        drop.pack()
        elsewhere = tk.Label(self.root, text="not the dropdown")
        elsewhere.pack()
        drop["values"] = ["first", "second"]
        self.show_window()

        drop.event_generate("<Button-1>", x=10, y=10)
        self.root.update()
        self.assertIsNotNone(drop._popup)
        elsewhere.event_generate("<Button-1>", x=2, y=2)
        self.root.update()
        self.assertIsNone(drop._popup)

        drop.event_generate("<Button-1>", x=10, y=10)
        self.root.update()
        self.assertIsNotNone(drop._popup, "closing it must not disable it")

    def test_escape_closes_the_open_list(self):
        var = tk.StringVar()
        drop = appmod.Dropdown(self.root, textvariable=var)
        drop.pack()
        drop["values"] = ["first", "second"]
        self.show_window()
        drop.event_generate("<Button-1>", x=10, y=10)
        self.root.update()
        self.root.event_generate("<Escape>")
        self.root.update()
        self.assertIsNone(drop._popup)

    def test_choosing_updates_the_drive_detail_in_the_window(self):
        app = appmod.App(self.root)
        if len(app.volumes) < 2:
            self.skipTest("needs at least two drives")
        second = app._short_label(app.volumes[1][0])
        app.drive_box._choose(second)
        self.assertEqual(app.drive_detail.get(), app.volumes[1][1])

    def test_the_scrollbar_accepts_what_a_treeview_sends_it(self):
        moved = []
        bar = appmod.ThinScrollbar(self.root, command=lambda *a:
                                   moved.append(a))
        bar.set(0.0, 0.5)                 # the yscrollcommand contract
        bar.set("0.25", "0.75")           # Tk sends these as strings
        self.root.update()

    def test_a_fully_visible_list_draws_no_thumb(self):
        """Nothing to scroll should show nothing, not a full-height bar."""
        bar = appmod.ThinScrollbar(self.root, command=lambda *a: None)
        bar.set(0.0, 1.0)
        self.root.update()
        self.assertEqual(bar.find_all(), ())
