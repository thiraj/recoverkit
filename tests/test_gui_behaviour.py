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
