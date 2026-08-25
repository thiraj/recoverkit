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
