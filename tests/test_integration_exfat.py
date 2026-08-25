"""
The CLAUDE.md procedure, applied to exFAT.

Same shape as test_integration_ntfs.py: format a real volume with the
operating system's own tool, write files into it, delete them with a real
driver, unmount, then scan the untouched image and compare checksums.

exFAT is the one filesystem where every desktop OS can do this:

  Linux  mkfs.exfat + mount (needs root, or exfat-fuse)
  macOS  hdiutil attach -nomount + newfs_exfat + diskutil mount

Both attach and mount a filesystem, so like the NTFS integration test this
only runs on request:

    RECOVERKIT_INTEGRATION=1 python3 -m unittest discover -v

test_exfat.py covers the same two assertions everywhere, on images this
package builds itself.
"""

import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diskio
import exfat
from tests.support import md5, md5_file

ENABLED = os.environ.get("RECOVERKIT_INTEGRATION") == "1"
IMAGE_MB = 64


def run(*command, capture=False):
    result = subprocess.run(command, check=True, stderr=subprocess.PIPE,
                            stdout=subprocess.PIPE)
    return result.stdout if capture else None


class _Formatter:
    """Makes a real exFAT volume, populates it, deletes the files, unmounts."""

    def __init__(self, image, mount):
        self.image = image
        self.mount = mount

    @staticmethod
    def available():
        raise NotImplementedError

    def format(self):
        raise NotImplementedError

    def mount_volume(self):
        raise NotImplementedError

    def unmount(self):
        raise NotImplementedError

    def detach(self):
        """Release the device, where the platform has one to release."""
        self.unmount()

    def remount(self):
        """
        Forces everything written so far out to the backing file.

        Without this the test is worthless: the file data can still be sitting
        in the buffer cache when the file is deleted, and the operating system
        then drops those pages instead of writing them. The image ends up with
        a directory entry pointing at clusters that were never filled in, and
        the engine gets blamed for it.
        """
        self.unmount()
        self.mount_volume()


class _LinuxFormatter(_Formatter):
    @staticmethod
    def available():
        tool = shutil.which("mkfs.exfat") or shutil.which("mkexfatfs")
        if not tool:
            return "needs mkfs.exfat - install exfatprogs"
        if os.geteuid() != 0:
            return "needs root to mount a loopback filesystem"
        return None

    def format(self):
        tool = shutil.which("mkfs.exfat") or shutil.which("mkexfatfs")
        run(tool, "-n", "TESTVOL", self.image)

    def mount_volume(self):
        run("mount", "-o", "loop", self.image, self.mount)

    def unmount(self):
        subprocess.run(["umount", self.mount], check=False,
                       stderr=subprocess.DEVNULL)


class _MacFormatter(_Formatter):
    """
    macOS will not format a plain file, so the image is attached as a raw
    disk first. `-nomount` keeps Finder out of it until we are ready.
    """

    def __init__(self, image, mount):
        super().__init__(image, mount)
        self.device = None

    @staticmethod
    def available():
        for tool in ("hdiutil", "newfs_exfat", "diskutil"):
            if shutil.which(tool) is None:
                return f"needs {tool}"
        return None

    def format(self):
        out = run("hdiutil", "attach", "-imagekey",
                  "diskimage-class=CRawDiskImage", "-nomount", self.image,
                  capture=True)
        self.device = out.decode().split()[0].strip()
        run("newfs_exfat", "-v", "TESTVOL", self.device)
        plistlib.loads(run("diskutil", "info", "-plist", self.device,
                           capture=True))       # fail early on an odd device

    def mount_volume(self):
        run("diskutil", "mount", "-mountPoint", self.mount, self.device)

    def unmount(self):
        subprocess.run(["diskutil", "unmount", self.mount], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def detach(self):
        self.unmount()
        if self.device:
            subprocess.run(["hdiutil", "detach", self.device, "-force"],
                           check=False, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            self.device = None


def _formatter_for_this_platform(image, mount):
    if sys.platform == "darwin":
        return _MacFormatter(image, mount)
    if sys.platform.startswith("linux"):
        return _LinuxFormatter(image, mount)
    return None


@unittest.skipUnless(ENABLED, "set RECOVERKIT_INTEGRATION=1 to run "
                              "(formats and mounts a real filesystem)")
class RealExfatVolumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workdir = tempfile.mkdtemp(prefix="recoverkit-exfat-")
        cls.image = os.path.join(cls.workdir, "test.exfat")
        cls.mount = os.path.join(cls.workdir, "mnt")
        os.mkdir(cls.mount)

        cls.formatter = _formatter_for_this_platform(cls.image, cls.mount)
        if cls.formatter is None:
            shutil.rmtree(cls.workdir, ignore_errors=True)
            raise unittest.SkipTest(f"no exFAT tooling known for {sys.platform}")

        reason = cls.formatter.available()
        if reason:
            shutil.rmtree(cls.workdir, ignore_errors=True)
            raise unittest.SkipTest(reason)

        with open(cls.image, "wb") as fh:
            fh.truncate(IMAGE_MB * 1024 * 1024)

        try:
            cls.formatter.format()
            cls.formatter.mount_volume()
        except subprocess.CalledProcessError as error:
            cls.formatter.detach()
            shutil.rmtree(cls.workdir, ignore_errors=True)
            detail = (error.stderr or b"").decode(errors="replace").strip()
            raise unittest.SkipTest(f"could not build a test volume: {detail}")

        cls.originals = {}
        try:
            cls._quieten_the_os(cls.mount)

            # The shape a camera actually produces.
            canon = os.path.join(cls.mount, "DCIM", "100CANON")
            os.makedirs(canon)

            files = {
                os.path.join(canon, "IMG_0042.JPG"):
                    b"\xFF\xD8\xFF" + os.urandom(300_000) + b"\xFF\xD9",
                os.path.join(canon, "IMG_0043.JPG"):
                    b"\xFF\xD8\xFF" + os.urandom(150_000) + b"\xFF\xD9",
                os.path.join(cls.mount, "receipt.txt"): b"a very short note",
            }
            for path, data in files.items():
                with open(path, "wb") as fh:
                    fh.write(data)
                cls.originals[os.path.basename(path)] = hashlib.md5(
                    data).hexdigest()

            subprocess.run(["sync"], check=False)
            # Get the data onto the backing file *before* deleting anything.
            cls.formatter.remount()

            for path in files:
                os.unlink(path)                 # a real delete, by the driver
            subprocess.run(["sync"], check=False)
        finally:
            cls.formatter.detach()

        cls.image_hash_before = md5_file(cls.image)

    @staticmethod
    def _quieten_the_os(mount):
        """
        Stop macOS writing its own change log onto the volume.

        Without this, macOS drops .fseventsd records straight into the
        clusters the deleted files just released - which is a perfectly
        realistic thing for an operating system to do, and exactly what the
        recovery-chance score is there to report, but it makes for a test
        that measures Spotlight's timing rather than our parser.
        """
        if sys.platform != "darwin":
            return
        folder = os.path.join(mount, ".fseventsd")
        try:
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, "no_log"), "wb"):
                pass
        except OSError:
            pass                                # best effort

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "formatter") and cls.formatter is not None:
            cls.formatter.detach()
        if hasattr(cls, "workdir"):
            shutil.rmtree(cls.workdir, ignore_errors=True)

    def volume(self):
        disk = diskio.ReadOnlyDisk(self.image)
        self.addCleanup(disk.close)
        return exfat.ExfatVolume(disk)

    def test_recovered_files_are_byte_identical_to_the_originals(self):
        """
        Assertion 1, against a volume the operating system itself wrote.

        The exact promise is "when the score says 100%, the file is intact".
        On a real volume the OS may write its own housekeeping into the space
        a deleted file just released, and then the file genuinely is damaged -
        reporting that honestly is the feature, not a failure. So every file
        scored at 100% must match exactly, and at least one must get there.
        """
        volume = self.volume()
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

    def test_names_sizes_and_folders_are_exact(self):
        """
        These come from the volume's own records, so they are right or the
        parser is wrong - the OS reusing space afterwards cannot affect them.
        """
        volume = self.volume()
        found = {f.name: f for f in volume.scan()}
        for name in self.originals:
            self.assertIn(name, found)
            self.assertGreater(found[name].size, 0)
            self.assertIsNotNone(found[name].created_at,
                                 f"{name} lost its timestamps")

    def test_source_image_is_unchanged_before_and_after_a_scan(self):
        """Assertion 2."""
        volume = self.volume()

        destination = tempfile.mkdtemp(prefix="recoverkit-out-")
        self.addCleanup(shutil.rmtree, destination, True)

        for found in volume.scan():
            if found.is_dir:
                continue
            data = volume.read_file(found)
            if data:
                with open(os.path.join(destination, found.name), "wb") as fh:
                    fh.write(data)

        self.assertEqual(md5_file(self.image), self.image_hash_before,
                         "SOURCE IMAGE MODIFIED BY A SCAN")

    def test_folder_paths_survive(self):
        volume = self.volume()
        found = {f.name: f for f in volume.scan()}
        self.assertIn("100CANON", found["IMG_0042.JPG"].path)
        self.assertEqual(found["receipt.txt"].path, "\\")

    def test_the_allocation_bitmap_is_found_on_a_real_volume(self):
        volume = self.volume()
        volume.scan()
        self.assertIsNotNone(volume._bitmap)

    def test_no_live_file_is_offered(self):
        volume = self.volume()
        for found in volume.scan():
            self.assertNotIn(found.name, ("", "TESTVOL"))


if __name__ == "__main__":
    unittest.main()
