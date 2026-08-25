"""
Tests for the read-only device layer.

This is the module the whole safety story rests on, so the tests here are
about what it *cannot* do as much as what it can.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diskio
from tests.support import ImageTestCase


PATTERN = bytes(range(256)) * 64          # 16 KB of known bytes


class ReadOnlyDiskSafetyTests(ImageTestCase):
    def setUp(self):
        self.use_image(PATTERN)

    def test_no_write_methods_are_exposed(self):
        """The object must offer no way to modify the source, by any name."""
        forbidden = ("write", "writelines", "truncate", "flush", "seek_write",
                     "write_at", "pwrite", "resize", "erase", "zero")
        with self.open_disk() as disk:
            for name in forbidden:
                self.assertFalse(
                    hasattr(disk, name),
                    f"ReadOnlyDisk exposes {name!r} - the source must never "
                    f"be writable")

    def test_kernel_rejects_a_write_to_the_descriptor(self):
        """
        Even reaching past the API to the raw descriptor must fail.

        This is the guarantee SAFETY.md makes: read-only is enforced by the
        operating system, not by our own discipline.
        """
        with self.open_disk() as disk:
            with self.assertRaises(OSError):
                os.write(disk._fd, b"corruption")

    @unittest.skipIf(sys.platform == "win32", "fcntl is POSIX only")
    def test_descriptor_carries_the_read_only_access_mode(self):
        """Ask the OS directly what mode the descriptor was opened in."""
        import fcntl
        with self.open_disk() as disk:
            flags = fcntl.fcntl(disk._fd, fcntl.F_GETFL)
            self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY,
                             "the source device is not open read-only")


class ReadOnlyDiskReadTests(ImageTestCase):
    def setUp(self):
        self.use_image(PATTERN)

    def test_reads_are_exact_at_any_offset(self):
        """Windows needs sector-aligned reads; callers must not have to care."""
        with self.open_disk() as disk:
            for offset, length in ((0, 512), (1, 10), (511, 2), (513, 1000),
                                   (4095, 4097), (0, len(PATTERN))):
                self.assertEqual(disk.read(offset, length),
                                 PATTERN[offset:offset + length],
                                 f"bad read at offset {offset} len {length}")

    def test_zero_length_read(self):
        with self.open_disk() as disk:
            self.assertEqual(disk.read(0, 0), b"")
            self.assertEqual(disk.read(0, -5), b"")

    def test_read_past_the_end_returns_what_exists(self):
        with self.open_disk() as disk:
            tail = disk.read(len(PATTERN) - 10, 4096)
            self.assertEqual(tail, PATTERN[-10:])
            self.assertEqual(disk.read(len(PATTERN) + 1000, 512), b"")

    def test_size(self):
        with self.open_disk() as disk:
            self.assertEqual(disk.size(), len(PATTERN))

    def test_stream_yields_the_whole_image_in_order(self):
        with self.open_disk() as disk:
            collected = b""
            offsets = []
            for offset, chunk in disk.stream(1024):
                offsets.append(offset)
                collected += chunk
            self.assertEqual(collected, PATTERN)
            self.assertEqual(offsets, list(range(0, len(PATTERN), 1024)))

    def test_context_manager_closes(self):
        disk = diskio.ReadOnlyDisk(self.image_path)
        with disk:
            self.assertEqual(disk.read(0, 4), PATTERN[:4])
        with self.assertRaises(OSError):
            os.fstat(disk._fd)


class MissingDeviceTests(unittest.TestCase):
    def test_missing_device_is_reported_plainly(self):
        with self.assertRaises(FileNotFoundError) as caught:
            diskio.ReadOnlyDisk(os.path.join(tempfile.gettempdir(),
                                             "recoverkit-no-such-device"))
        self.assertIn("not found", str(caught.exception).lower())


class DestinationGuardTests(unittest.TestCase):
    """
    `same_physical_drive` is what stops a recovery from landing on the drive
    being scanned and eating the very data it is rescuing. It must fail
    closed: when it cannot tell, the answer is yes.
    """

    def test_unknown_source_is_treated_as_the_same_drive(self):
        self.assertTrue(diskio.same_physical_drive(
            "/dev/definitely-not-a-real-device", tempfile.gettempdir()))

    def test_missing_destination_is_treated_as_the_same_drive(self):
        self.assertTrue(diskio.same_physical_drive(
            "/dev/definitely-not-a-real-device",
            "/no/such/folder/anywhere/at/all"))

    @unittest.skipUnless(sys.platform == "win32", "Windows drive letters")
    def test_windows_matches_on_drive_letter(self):
        self.assertTrue(diskio.same_physical_drive(r"\\.\C:", r"C:\Recovered"))
        self.assertFalse(diskio.same_physical_drive(r"\\.\C:", r"D:\Recovered"))

    @unittest.skipIf(sys.platform == "win32", "POSIX device comparison")
    def test_posix_regular_file_source_is_not_the_destination_drive(self):
        """
        A disk image sitting in a folder is not the folder's device, so
        recovering next to an image file is allowed - that is how the tests
        themselves work.
        """
        fd, path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(os.unlink, path)
        self.assertFalse(diskio.same_physical_drive(
            path, os.path.dirname(path)))


class DevicePathTests(unittest.TestCase):
    def test_posix_paths_pass_through(self):
        if sys.platform != "win32":
            self.assertEqual(diskio.device_path("/dev/disk2"), "/dev/disk2")

    def test_volume_listing_is_a_list_of_pairs(self):
        for entry in diskio.list_volumes():
            self.assertEqual(len(entry), 2)
            self.assertTrue(all(isinstance(part, str) for part in entry))


if __name__ == "__main__":
    unittest.main()


class DeviceSizeTests(ImageTestCase):
    """
    A wrong answer here is not cosmetic. The deep scan only reports progress
    when it knows how big the drive is, so a size of zero leaves the bar
    frozen for the whole scan with no way to tell a five-minute job from an
    all-day one.
    """

    def setUp(self):
        self.use_image(b"z" * 8192)

    def test_size_of_a_regular_file(self):
        with self.open_disk() as disk:
            self.assertEqual(disk.size(), 8192)

    def test_the_answer_is_cached_not_recomputed(self):
        with self.open_disk() as disk:
            self.assertEqual(disk.size(), disk.size())

    def test_asking_the_size_leaves_reads_working(self):
        """size() seeks to the end; reading afterwards must still be right."""
        with self.open_disk() as disk:
            disk.size()
            self.assertEqual(disk.read(0, 4), b"zzzz")
            self.assertEqual(disk.read(8188, 8), b"zzzz")

    def test_the_ioctl_fallback_declines_politely_on_a_plain_file(self):
        """
        A regular file has no block-device driver to ask. The fallback must
        return zero rather than raising - it is a last resort, not a
        precondition.
        """
        with self.open_disk() as disk:
            self.assertEqual(disk._ioctl_size(), 0)
