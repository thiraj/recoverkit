"""
The two undelete engines must stay interchangeable.

app.py picks an engine at scan time and then treats the result identically -
same fields in the results table, same read_file call to recover. If exfat.py
drifts from ntfs.py the GUI breaks in ways that only show up on someone's
broken SD card, so the shape of the two is asserted here rather than trusted.
"""

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carve
import exfat
import fat32
import ntfs
from tests import exfat_image, ntfs_image
from tests.support import ImageTestCase


class VolumeInterfaceTests(unittest.TestCase):
    def test_the_same_public_methods(self):
        def public(cls):
            return {name for name, _ in inspect.getmembers(
                cls, inspect.isfunction) if not name.startswith("_")}

        self.assertEqual(public(exfat.ExfatVolume), public(ntfs.NtfsVolume))
        self.assertEqual(public(fat32.Fat32Volume), public(ntfs.NtfsVolume))

    def test_scan_takes_the_same_arguments(self):
        for engine in (exfat.ExfatVolume, fat32.Fat32Volume):
            self.assertEqual(inspect.signature(engine.scan),
                             inspect.signature(ntfs.NtfsVolume.scan),
                             engine.__name__)

    def test_read_file_takes_the_same_arguments(self):
        for engine in (exfat.ExfatVolume, fat32.Fat32Volume):
            self.assertEqual(inspect.signature(engine.read_file),
                             inspect.signature(ntfs.NtfsVolume.read_file),
                             engine.__name__)

    def test_both_are_constructed_from_a_disk_alone(self):
        for cls in (exfat.ExfatVolume, fat32.Fat32Volume, ntfs.NtfsVolume):
            parameters = list(
                inspect.signature(cls.__init__).parameters)
            self.assertEqual(parameters, ["self", "disk"])

    def test_both_expose_the_geometry_the_gui_reads(self):
        for attribute in ("sector_size", "cluster_size", "total_clusters"):
            for engine in (ntfs.NtfsVolume, exfat.ExfatVolume,
                           fat32.Fat32Volume):
                self.assertIn(attribute, engine.__init__.__code__.co_names,
                              engine.__name__)


class ResultInterfaceTests(unittest.TestCase):
    """
    Every field app.py reads off a result must exist on all three result
    types - the two undelete engines and the carver.
    """

    GUI_FIELDS = ("name", "path", "size", "chance", "deleted_at",
                  "created_at", "is_dir", "extension")

    def test_every_engine_result_has_the_same_core_fields(self):
        """
        Extras are allowed - exFAT and FAT32 both need to say when a layout
        was assumed, and FAT32 alone can lose a filename's first letter. The
        fields the interface reads must be identical.
        """
        extras = {"assumed_contiguous", "first_letter_lost"}
        core = set(ntfs.DeletedFile.__slots__) - extras
        for engine in (exfat.DeletedFile, fat32.DeletedFile):
            self.assertEqual(set(engine.__slots__) - extras, core,
                             engine.__module__)

    def test_every_result_type_has_what_the_gui_reads(self):
        results = [
            ntfs.DeletedFile(name="a.txt", size=1, runs=[], chance=100),
            exfat.DeletedFile(name="a.txt", size=1, runs=[], chance=100),
            fat32.DeletedFile(name="a.txt", size=1, runs=[], chance=100),
            carve.CarvedFile("recovered_jpg_00001.jpg", "jpg", 0, 1),
        ]
        for result in results:
            for field in self.GUI_FIELDS:
                self.assertTrue(
                    hasattr(result, field),
                    f"{type(result).__name__} is missing {field!r}, which the "
                    f"results table reads")


class EngineSwapTests(ImageTestCase):
    """
    The real proof: the same calling code drives either engine and gets the
    same file back.
    """

    @staticmethod
    def recover_everything(volume):
        """Deliberately written without knowing which engine it was handed."""
        out = {}
        for found in volume.scan():
            if found.is_dir:
                continue
            out[(found.path, found.name)] = volume.read_file(found)
        return out

    def test_both_engines_answer_the_same_calls(self):
        payload = os.urandom(30_000)

        ntfs_build = ntfs_image.NtfsImage()
        folder = ntfs_build.add_dir("Photos")
        ntfs_build.add_file("shared.jpg", payload, parent=folder, deleted=True)

        exfat_build = exfat_image.ExfatImage()
        exfat_folder = exfat_build.add_dir("Photos")
        exfat_build.add_file("shared.jpg", payload, parent=exfat_folder,
                             deleted=True)

        # Both images are hashed by use_image and re-checked in cleanup.
        self.use_image(ntfs_build.build(), suffix=".ntfs")
        from_ntfs = self.recover_everything(ntfs.NtfsVolume(self.disk()))

        self.use_image(exfat_build.build(), suffix=".exfat")
        from_exfat = self.recover_everything(exfat.ExfatVolume(self.disk()))

        self.assertEqual(from_ntfs[("Photos", "shared.jpg")], payload)
        self.assertEqual(from_exfat[("Photos", "shared.jpg")], payload)
        self.assertEqual(from_ntfs, from_exfat)


class NoGuiImportsTests(unittest.TestCase):
    """
    CLAUDE.md: both engines must be importable on their own. A stray tkinter
    import would break every headless and scripted use.
    """

    def test_engines_do_not_import_the_gui(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for module in ("ntfs.py", "exfat.py", "fat32.py", "carve.py",
                       "diskio.py", "signatures.py", "verify.py"):
            with open(os.path.join(root, module), encoding="utf-8") as fh:
                source = fh.read()
            for banned in ("import tkinter", "from tkinter", "import app"):
                self.assertNotIn(banned, source,
                                 f"{module} pulls in the GUI")

    def test_engines_use_only_the_standard_library(self):
        """
        The list is explicit rather than clever on purpose: a new name here
        should be a deliberate decision by a person, because `python app.py`
        has to just work on a machine someone is trying to rescue.
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        allowed = {
            "datetime", "struct", "os", "sys", "re",
            "ctypes", "string",          # Windows volume enumeration
            "subprocess", "plistlib",    # macOS volume enumeration (diskutil)
            "zlib",                      # PNG chunk checksums in verify.py
            "array", "fcntl",            # asking a raw device its size
            "threading",                 # one lock, for platforms with no pread
            "json",                      # the service's line protocol
            "posixpath",                 # mount tables are POSIX everywhere
        }
        # Modules of this project are not dependencies.
        allowed |= {name[:-3] for name in os.listdir(root)
                    if name.endswith(".py")}
        for module in ("ntfs.py", "exfat.py", "fat32.py", "carve.py",
                       "diskio.py", "signatures.py", "verify.py",
                       "recovery.py", "service.py"):
            with open(os.path.join(root, module), encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("import ") and " " not in line[7:]:
                        self.assertIn(line[7:], allowed,
                                      f"{module} imports {line[7:]}")


class SourceOpeningTests(unittest.TestCase):
    """
    CLAUDE.md's hardest rule: diskio.py is the only module allowed to open a
    source device. This is the test that catches a well-meaning shortcut added
    somewhere else months from now.
    """

    def test_only_diskio_opens_devices(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        openers = ("os.open(", "open(", "io.open(", "os.fdopen(")
        # Only the modules that read a source drive are checked. verify.py,
        # recovery.py and service.py open files in the *destination*, which
        # is their job - the rule is that nothing but diskio.py may open a
        # source device, not that nothing may open anything.
        for module in ("ntfs.py", "exfat.py", "fat32.py", "carve.py",
                       "signatures.py"):
            with open(os.path.join(root, module), encoding="utf-8") as fh:
                for number, line in enumerate(fh, 1):
                    code = line.split("#")[0]
                    for opener in openers:
                        self.assertNotIn(
                            opener, code,
                            f"{module}:{number} opens something directly - "
                            f"only diskio.py may touch a source device")

    def test_diskio_opens_read_only_and_nothing_else(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "diskio.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("os.O_RDONLY", source)
        for banned in ("os.O_WRONLY", "os.O_RDWR", "os.O_CREAT", "os.O_TRUNC",
                       "os.write("):
            self.assertNotIn(banned, source,
                             f"diskio.py contains {banned} - the source drive "
                             f"must never be writable")


if __name__ == "__main__":
    unittest.main()
