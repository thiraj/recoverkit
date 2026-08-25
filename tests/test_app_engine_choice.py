"""
Tests for the one piece of app.py that can be exercised without a display:
choosing an undelete engine for the drive in front of it.

The GUI itself is not tested here - it needs a Tk event loop and a person to
look at it - but picking the wrong engine, or reporting the wrong reason when
neither fits, is a bug a user would meet on their worst day.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import exfat_image, ntfs_image
from tests.support import ImageTestCase

try:
    import tkinter                                  # noqa: F401
    import app
    HAVE_TK = True
except Exception:                                   # pragma: no cover
    HAVE_TK = False


@unittest.skipUnless(HAVE_TK, "app.py needs tkinter, which is not available")
class EngineChoiceTests(ImageTestCase):
    def test_an_ntfs_drive_gets_the_ntfs_engine(self):
        image = ntfs_image.NtfsImage()
        image.add_file("x.txt", b"hello", resident=True)
        self.use_image(image.build(), suffix=".ntfs")

        volume = app.App._open_volume(self.disk())
        self.assertEqual(type(volume).__name__, "NtfsVolume")

    def test_an_exfat_drive_gets_the_exfat_engine(self):
        image = exfat_image.ExfatImage()
        image.add_file("x.txt", b"hello")
        self.use_image(image.build(), suffix=".exfat")

        volume = app.App._open_volume(self.disk())
        self.assertEqual(type(volume).__name__, "ExfatVolume")

    def test_an_unknown_drive_is_explained_in_plain_language(self):
        """
        An APFS or ext4 drive lands here. The message has to say what to do
        next without using the word "filesystem" at someone mid-crisis.
        """
        self.use_image(os.urandom(1024 * 1024), suffix=".apfs")

        with self.assertRaises(ValueError) as caught:
            app.App._open_volume(self.disk())

        message = str(caught.exception)
        self.assertIn("NTFS", message)
        self.assertIn("exFAT", message)
        for jargon in ("boot sector", "0x", "struct", "MFT record",
                       "cluster heap"):
            self.assertNotIn(jargon, message)

    def test_choosing_an_engine_does_not_touch_the_drive(self):
        image = exfat_image.ExfatImage()
        image.add_file("x.txt", b"hello")
        self.use_image(image.build(), suffix=".exfat")
        app.App._open_volume(self.disk())
        # tearDown re-checks the image hash.


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_TK, "app.py needs tkinter, which is not available")
class DriveRefreshTests(unittest.TestCase):
    """
    Removable drives come and go, and macOS reuses device paths. A list built
    when the window opened is stale the moment a card is swapped.
    """

    def test_refresh_clears_the_cached_disk_map(self):
        """
        diskio caches the APFS container-to-hardware map. Across a replug
        that map can be wrong, and it feeds the same-drive safety check.
        """
        import diskio
        diskio._physical_store_cache = {"disk1": "disk0"}
        diskio.refresh()
        self.assertIsNone(diskio._physical_store_cache,
                          "stale disk information survived a refresh")

    def test_the_app_exposes_a_refresh_and_a_staleness_check(self):
        for name in ("_refresh_drives", "_confirm_drive_still_there"):
            self.assertTrue(callable(getattr(app.App, name, None)),
                            f"App.{name} is missing")

    def test_a_scan_checks_the_drive_is_still_present_first(self):
        """
        The staleness check must run before the destination guard: the guard
        compares against the source path, so a stale path would have it
        checking the wrong device.
        """
        import inspect
        source = inspect.getsource(app.App._start)
        self.assertIn("_confirm_drive_still_there", source)
        self.assertLess(source.index("_confirm_drive_still_there"),
                        source.index("_guard_destination"),
                        "the staleness check must come before the same-drive "
                        "check, not after it")
