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


@unittest.skipUnless(HAVE_WORKING_TK, "no working Tk on this interpreter")
class RecoverButtonTests(unittest.TestCase):
    """
    The button was gated on `not self.scanning`, which is wrong for deep scan:
    that mode streams results in while it runs, so the list filled up while
    the button stayed dead for the whole scan.
    """

    def setUp(self):
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
        """A deep-scan result: its bytes are already in memory."""
        return carve.CarvedFile(name, "jpg", 4096, 900, b"x" * 900)

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
        The regression. Carved files carry their own bytes, so there is no
        reason to make someone wait out a long scan before saving one.
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


@unittest.skipUnless(HAVE_WORKING_TK, "no working Tk on this interpreter")
class ConcurrentReadRefusalTests(unittest.TestCase):
    """
    Enabling the button is only half of it. A file that still has to be read
    off the drive cannot be recovered while the scanning thread is using that
    same descriptor - both would seek it out from under each other and the
    saved file would be quietly wrong.
    """

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.app = appmod.App(self.root)
        self.messages = []
        self.original = appmod.messagebox.showinfo
        appmod.messagebox.showinfo = lambda title, text: self.messages.append(
            (title, text))
        self.addCleanup(setattr, appmod.messagebox, "showinfo", self.original)

    def test_a_mid_scan_recovery_that_needs_the_drive_is_refused(self):
        self.app.scanning = True
        self.app.events.put(("batch", [
            ntfs.DeletedFile(name="photo.jpg", path="", size=10, chance=100,
                             runs=[(1, 1)], is_dir=False,
                             content_check=signatures.MATCH)]))
        self.app._drain()
        self.app.tree.selection_set(self.app.tree.get_children()[0])

        self.app._recover()

        self.assertTrue(self.messages, "the user was told nothing")
        title, text = self.messages[0]
        self.assertIn("scanning", title.lower())
        self.assertIn("Stop", text)
        for jargon in ("descriptor", "thread", "lseek", "race"):
            self.assertNotIn(jargon, text.lower())


if __name__ == "__main__":
    unittest.main()
