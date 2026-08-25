"""
The manual procedure from CLAUDE.md, automated.

    dd if=/dev/zero of=test.ntfs bs=1M count=64
    mkntfs -F -Q -L TESTVOL test.ntfs
    ntfs-3g test.ntfs mnt
    ... copy files in, record md5s, delete them, unmount ...
    scan, recover, compare md5s, compare the image checksum

This is the gold-standard test: the volume is made by Microsoft's own format
as implemented by ntfs-3g, and the files are deleted by a real driver, not by
us flipping a bit and hoping that is what deletion looks like.

It needs mkntfs and ntfs-3g (Linux, `ntfs-3g` package) and permission to mount
a filesystem, so it does not run by default:

    RECOVERKIT_INTEGRATION=1 python3 -m unittest discover -v

The hermetic equivalent in test_ntfs.py runs everywhere and covers the same
two assertions.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diskio
import ntfs
from tests.support import md5, md5_file

ENABLED = os.environ.get("RECOVERKIT_INTEGRATION") == "1"
IMAGE_MB = 64


def missing_tools(*names):
    return [name for name in names if shutil.which(name) is None]


def run(*command):
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE)


@unittest.skipUnless(ENABLED, "set RECOVERKIT_INTEGRATION=1 to run "
                              "(builds and mounts a real filesystem)")
class RealNtfsVolumeTests(unittest.TestCase):
    """Build a genuine NTFS volume, delete files from it, then recover them."""

    @classmethod
    def setUpClass(cls):
        absent = missing_tools("mkntfs", "ntfs-3g", "fusermount")
        if absent:
            raise unittest.SkipTest(
                f"needs {', '.join(absent)} - install ntfs-3g")

        cls.workdir = tempfile.mkdtemp(prefix="recoverkit-ntfs-")
        cls.image = os.path.join(cls.workdir, "test.ntfs")
        cls.mount = os.path.join(cls.workdir, "mnt")
        os.mkdir(cls.mount)

        with open(cls.image, "wb") as fh:
            fh.truncate(IMAGE_MB * 1024 * 1024)

        try:
            run("mkntfs", "-F", "-Q", "-L", "TESTVOL", cls.image)
            cls.mount_volume()
        except subprocess.CalledProcessError as error:
            shutil.rmtree(cls.workdir, ignore_errors=True)
            detail = (error.stderr or b"").decode(errors="replace").strip()
            raise unittest.SkipTest(f"could not build a test volume: {detail}")

        cls.originals = {}
        try:
            documents = os.path.join(cls.mount, "Documents")
            reports = os.path.join(documents, "Reports")
            os.makedirs(reports)

            files = {
                os.path.join(documents, "holiday.jpg"):
                    b"\xFF\xD8\xFF" + os.urandom(200_000) + b"\xFF\xD9",
                os.path.join(reports, "quarterly.xlsx"): os.urandom(120_000),
                os.path.join(cls.mount, "notes.txt"): b"a very short note",
            }
            for path, data in files.items():
                with open(path, "wb") as fh:
                    fh.write(data)
                cls.originals[os.path.basename(path)] = hashlib.md5(
                    data).hexdigest()

            subprocess.run(["sync"], check=False)
            # Get the data onto the backing file *before* deleting anything.
            # If the writes are still in the buffer cache when the file is
            # deleted, the OS drops those pages and never writes them: the
            # image ends up with a record pointing at clusters that were
            # never filled in, and the engine gets blamed for it.
            cls.unmount()
            cls.mount_volume()

            for path in files:
                os.unlink(path)                 # a real delete, by the driver
            subprocess.run(["sync"], check=False)
        finally:
            cls.unmount()

        cls.image_hash_before = md5_file(cls.image)

    @classmethod
    def mount_volume(cls):
        run("ntfs-3g", cls.image, cls.mount)

    @classmethod
    def unmount(cls):
        subprocess.run(["fusermount", "-u", cls.mount], check=False,
                       stderr=subprocess.DEVNULL)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "workdir"):
            cls.unmount()
            shutil.rmtree(cls.workdir, ignore_errors=True)

    def test_recovered_files_are_byte_identical_to_the_originals(self):
        """
        Assertion 1 from CLAUDE.md, on a volume a real driver wrote.

        The exact promise is "when the score says 100%, the file is intact".
        A real OS may write housekeeping into the space a deleted file just
        released, and then the file genuinely is damaged - reporting that
        honestly is the feature. So every file scored at 100% must match
        exactly, and at least one must get there.
        """
        disk = diskio.ReadOnlyDisk(self.image)
        self.addCleanup(disk.close)
        volume = ntfs.NtfsVolume(disk)
        found = {f.name: f for f in volume.scan()}

        intact = 0
        for name, expected in self.originals.items():
            self.assertIn(name, found, f"{name} was not recovered at all")
            result = found[name]
            recovered = volume.read_file(result)
            if result.chance == 100:
                self.assertEqual(
                    md5(recovered), expected,
                    f"{name} was scored 100% but did not come back "
                    f"byte-identical")
                intact += 1

        self.assertTrue(
            intact,
            "not one file survived intact, so nothing was really tested - "
            "the operating system reused every freed cluster before we "
            "unmounted")

    def test_source_image_is_unchanged_before_and_after_a_scan(self):
        """Assertion 2 from CLAUDE.md."""
        disk = diskio.ReadOnlyDisk(self.image)
        self.addCleanup(disk.close)
        volume = ntfs.NtfsVolume(disk)

        destination = tempfile.mkdtemp(prefix="recoverkit-out-")
        self.addCleanup(shutil.rmtree, destination, True)

        for found in volume.scan():
            if found.is_dir:
                continue
            data = volume.read_file(found)
            if not data:
                continue
            with open(os.path.join(destination,
                                   found.name.replace(os.sep, "_")), "wb") as fh:
                fh.write(data)

        self.assertEqual(md5_file(self.image), self.image_hash_before,
                         "SOURCE IMAGE MODIFIED BY A SCAN")

    def test_folder_paths_survive(self):
        disk = diskio.ReadOnlyDisk(self.image)
        self.addCleanup(disk.close)
        volume = ntfs.NtfsVolume(disk)
        found = {f.name: f for f in volume.scan()}
        self.assertIn("Documents", found["holiday.jpg"].path)
        self.assertIn("Reports", found["quarterly.xlsx"].path)

    def test_files_still_in_use_are_not_listed(self):
        """
        mkntfs leaves its own metadata files live on the volume. None of them
        should ever appear as recoverable.
        """
        disk = diskio.ReadOnlyDisk(self.image)
        self.addCleanup(disk.close)
        volume = ntfs.NtfsVolume(disk)
        for found in volume.scan():
            self.assertFalse(found.name.startswith("$"),
                             f"live metadata file {found.name} was listed")


if __name__ == "__main__":
    unittest.main()
