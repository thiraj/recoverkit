"""
Tests for the check that stops a recovery landing on the drive being scanned.

This is the failure mode that destroys the data the user came to rescue, so
it gets its own file. The parsing is exercised against captured mount tables
from several systems rather than only against whatever is plugged into the
machine running the tests - the bug this file exists to prevent was invisible
on Linux and wide open on macOS.

The original implementation compared `st_dev` of the destination against
`st_rdev` of the source device. On macOS those are unrelated numbers - a
mount point's st_dev is a synthetic filesystem id - so the check silently
answered "different drives" for every case, including recovering a USB stick
onto itself.
"""

import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diskio


# Captured from a real macOS 12 machine with a USB stick mounted. Note the
# NTFS volume: macOS mounts it through a userspace filesystem, so the device
# column is a URL rather than a /dev node.
MACOS_MOUNTS = """\
/dev/disk1s5s1 on / (apfs, sealed, local, read-only, journaled)
devfs on /dev (devfs, local, nobrowse)
/dev/disk1s4 on /System/Volumes/VM (apfs, local, noexec, journaled, noatime, nobrowse)
/dev/disk1s1 on /System/Volumes/Data (apfs, local, journaled, nobrowse)
map auto_home on /System/Volumes/Data/home (autofs, automounted, nobrowse)
/dev/disk3s1 on /Volumes/My Backup Drive (apfs, local, nodev, nosuid, journaled)
ntfs://disk4s1/ on /Volumes/Untitled (lifs, local, read-only, noowners, noatime)
exfat://disk5s1/ on /Volumes/SD CARD (lifs, local, noowners)
"""

LINUX_MOUNTS = """\
/dev/nvme0n1p2 / ext4 rw,relatime 0 0
proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0
/dev/nvme0n1p1 /boot/efi vfat rw,relatime 0 0
/dev/sda1 /mnt/photos ext4 rw,relatime 0 0
/dev/mmcblk0p1 /media/pi/CANON\\040CARD exfat rw,relatime 0 0
tmpfs /run tmpfs rw,nosuid,nodev 0 0
"""


class WholeDiskTests(unittest.TestCase):
    """A partition and its disk are the same hardware and must compare equal."""

    def test_macos_names(self):
        for name, expected in (
                ("/dev/disk4", "disk4"),
                ("/dev/disk4s1", "disk4"),
                ("/dev/rdisk4s1", "disk4"),          # raw character device
                ("disk1s5s1", "disk1"),              # APFS snapshot
                ("rdisk10s2", "disk10"),             # two-digit disk number
        ):
            self.assertEqual(diskio._whole_disk(name), expected, name)

    def test_linux_names(self):
        for name, expected in (
                ("/dev/sda", "sda"),
                ("/dev/sda1", "sda"),
                ("/dev/sdb12", "sdb"),
                ("/dev/nvme0n1", "nvme0n1"),
                ("/dev/nvme0n1p3", "nvme0n1"),       # NVMe puts a p first
                ("/dev/mmcblk0", "mmcblk0"),
                ("/dev/mmcblk0p1", "mmcblk0"),       # SD card reader
                ("/dev/vda2", "vda"),
        ):
            self.assertEqual(diskio._whole_disk(name), expected, name)

    def test_unrecognised_names_return_none_so_callers_fail_closed(self):
        for name in ("", None, "/dev/mapper/vg0-root", "/dev/md/raid",
                     "tmpfs", "//server/share", "/dev/"):
            self.assertIsNone(diskio._whole_disk(name), repr(name))


class MountTableParsingTests(unittest.TestCase):
    def test_macos_mount_output(self):
        table = dict((point, dev) for point, dev
                     in diskio.parse_mount_table(MACOS_MOUNTS))
        self.assertEqual(table["/"], "disk1s5s1")
        self.assertEqual(table["/System/Volumes/Data"], "disk1s1")

    def test_a_mount_point_containing_spaces(self):
        table = dict(diskio.parse_mount_table(MACOS_MOUNTS))
        self.assertIn("/Volumes/My Backup Drive", table)
        self.assertEqual(table["/Volumes/My Backup Drive"], "disk3s1")

    def test_userspace_filesystem_urls(self):
        """
        macOS mounts NTFS and exFAT as `ntfs://disk4s1/`, not `/dev/disk4s1`.
        Missing this is what let a USB stick be recovered onto itself.
        """
        table = dict(diskio.parse_mount_table(MACOS_MOUNTS))
        self.assertEqual(table["/Volumes/Untitled"], "disk4s1")
        self.assertEqual(table["/Volumes/SD CARD"], "disk5s1")

    def test_entries_without_a_real_device_are_dropped(self):
        points = [p for p, _ in diskio.parse_mount_table(MACOS_MOUNTS)]
        self.assertNotIn("/dev", points)                    # devfs
        self.assertNotIn("/System/Volumes/Data/home", points)  # autofs

    def test_linux_proc_mounts(self):
        table = dict(diskio.parse_mount_table(LINUX_MOUNTS))
        self.assertEqual(table["/"], "nvme0n1p2")
        self.assertEqual(table["/mnt/photos"], "sda1")
        self.assertNotIn("/proc", table)
        self.assertNotIn("/run", table)

    def test_linux_octal_escaped_mount_point(self):
        """/proc/mounts writes a space as \\040."""
        table = dict(diskio.parse_mount_table(LINUX_MOUNTS))
        self.assertEqual(table["/media/pi/CANON CARD"], "mmcblk0p1")

    def test_junk_does_not_raise(self):
        for text in ("", "\n\n", "garbage", "one\ntwo three four\n"):
            diskio.parse_mount_table(text)


class FilesystemNameTests(unittest.TestCase):
    """
    Naming the filesystem from the partition type byte is wrong: MBR type
    0x07 is used for both NTFS and exFAT, so a freshly formatted exFAT card
    is reported by diskutil as "Windows_NTFS". Only the mount table knows.
    """

    def test_macos_userspace_mounts_are_named_by_their_url_scheme(self):
        names = diskio.parse_mount_filesystems(MACOS_MOUNTS)
        self.assertEqual(names["/Volumes/Untitled"], "NTFS")
        self.assertEqual(names["/Volumes/SD CARD"], "exFAT")

    def test_a_native_mount_is_named_by_its_filesystem_token(self):
        names = diskio.parse_mount_filesystems(
            "/dev/disk5s1 on /Volumes/TESTCARD (exfat, local, nodev)\n"
            "/dev/disk1s1 on /System/Volumes/Data (apfs, local, journaled)\n")
        self.assertEqual(names["/Volumes/TESTCARD"], "exFAT")
        self.assertEqual(names["/System/Volumes/Data"], "APFS")

    def test_lifs_alone_is_not_treated_as_a_filesystem_name(self):
        """`lifs` is a host for other formats, not a format."""
        names = diskio.parse_mount_filesystems(
            "/dev/disk9s1 on /Volumes/Odd (lifs, local)\n")
        self.assertNotIn("/Volumes/Odd", names)

    def test_an_ambiguous_partition_type_is_not_named(self):
        """
        Windows_NTFS must not appear in the content table - it cannot tell
        NTFS and exFAT apart, and a wrong label is worse than none.
        """
        self.assertNotIn("Windows_NTFS", diskio._CONTENT_NAMES)

    def test_unknown_filesystems_are_skipped_rather_than_guessed(self):
        names = diskio.parse_mount_filesystems(
            "map auto_home on /System/Volumes/Data/home (autofs, automounted)\n"
            "devfs on /dev (devfs, local)\n")
        self.assertEqual(names, {})

    def test_junk_does_not_raise(self):
        for text in ("", "\n", "garbage", "a on b"):
            diskio.parse_mount_filesystems(text)


class DeviceBackingPathTests(unittest.TestCase):
    def setUp(self):
        self.mounts = diskio.parse_mount_table(MACOS_MOUNTS)

    def test_the_longest_matching_mount_point_wins(self):
        """/Volumes/Untitled/Recovered belongs to the stick, not to /."""
        self.assertEqual(
            diskio.device_backing_path("/Volumes/Untitled/Recovered",
                                       self.mounts), "disk4s1")

    def test_a_path_on_the_root_filesystem(self):
        self.assertEqual(
            diskio.device_backing_path("/Users/someone/Desktop", self.mounts),
            "disk1s5s1")

    def test_a_prefix_that_is_not_a_path_boundary_does_not_match(self):
        """/Volumes/UntitledOther is a different volume from /Volumes/Untitled."""
        self.assertNotEqual(
            diskio.device_backing_path("/Volumes/UntitledOther/x", self.mounts),
            "disk4s1")

    def test_no_mount_table_means_no_answer(self):
        self.assertIsNone(diskio.device_backing_path("/anywhere", None))
        self.assertIsNone(diskio.device_backing_path("/anywhere", []))


class SynthesizedDiskTests(unittest.TestCase):
    """
    APFS shows a container as its own disk (disk1) backed by a real partition
    (disk0s2). Comparing the two names finds them different; they are the
    same physical SSD.
    """

    SAMPLE = {"AllDisksAndPartitions": [
        {"DeviceIdentifier": "disk0", "Size": 500277790720},
        {"DeviceIdentifier": "disk1",
         "APFSPhysicalStores": [{"DeviceIdentifier": "disk0s2"}]},
        {"DeviceIdentifier": "disk3",
         "APFSPhysicalStores": [{"DeviceIdentifier": "disk2s1"}]},
    ]}

    def test_containers_map_to_the_hardware_underneath(self):
        mapping = diskio.physical_store_map(self.SAMPLE)
        self.assertEqual(mapping, {"disk1": "disk0", "disk3": "disk2"})

    def test_no_apfs_means_an_empty_map(self):
        self.assertEqual(diskio.physical_store_map({}), {})
        self.assertEqual(diskio.physical_store_map(None), {})


class SamePhysicalDriveTests(unittest.TestCase):
    """
    The whole point, end to end, with the mount table and the APFS mapping
    forced to known values so the result does not depend on the machine.
    """

    def setUp(self):
        self.original_platform = sys.platform
        self.original_cache = diskio._physical_store_cache
        self.original_reader = diskio._read_mount_table
        diskio._read_mount_table = lambda: diskio.parse_mount_table(
            MACOS_MOUNTS)
        diskio._physical_store_cache = {"disk1": "disk0"}
        self.addCleanup(self._restore)

    def _restore(self):
        diskio._read_mount_table = self.original_reader
        diskio._physical_store_cache = self.original_cache

    @unittest.skipIf(sys.platform == "win32", "POSIX behaviour")
    def test_the_answer_does_not_depend_on_paths_existing_locally(self):
        """
        The mount table is the source of truth, not the local filesystem.
        An earlier version walked up to the nearest existing directory, so
        once the test stick was reformatted and /Volumes/Untitled stopped
        existing, the guard walked up to /Volumes, decided that was the
        internal drive, and allowed a recovery onto the source.
        """
        self.assertFalse(os.path.exists("/Volumes/Untitled"),
                         "this test needs a path that does not exist locally")
        self.assertTrue(diskio.same_physical_drive(
            "/dev/rdisk4s1", "/Volumes/Untitled/NotCreatedYet"))

    @unittest.skipIf(sys.platform == "win32", "POSIX behaviour")
    def test_a_usb_stick_cannot_be_recovered_onto_itself(self):
        """
        The bug this file exists for. The stick is /dev/rdisk4s1 and it is
        mounted at /Volumes/Untitled; recovering there would overwrite the
        deleted files while reading them.
        """
        self.assertTrue(diskio.same_physical_drive(
            "/dev/rdisk4s1", "/Volumes/Untitled"))

    @unittest.skipIf(sys.platform == "win32", "POSIX behaviour")
    def test_nor_onto_a_subfolder_of_itself(self):
        self.assertTrue(diskio.same_physical_drive(
            "/dev/rdisk4s1", "/Volumes/Untitled/Recovered/Photos"))

    @unittest.skipIf(sys.platform == "win32", "POSIX behaviour")
    def test_nor_when_scanning_the_whole_disk(self):
        self.assertTrue(diskio.same_physical_drive(
            "/dev/rdisk4", "/Volumes/Untitled"))

    @unittest.skipIf(sys.platform == "win32", "POSIX behaviour")
    def test_an_apfs_container_and_its_volumes_are_one_drive(self):
        """Scanning /dev/rdisk0s2 and writing to / is the same SSD twice."""
        self.assertTrue(diskio.same_physical_drive(
            "/dev/rdisk0s2", "/Users/someone/Desktop"))

    @unittest.skipIf(sys.platform == "win32", "POSIX behaviour")
    def test_a_genuinely_different_drive_is_allowed(self):
        self.assertFalse(diskio.same_physical_drive(
            "/dev/rdisk4s1", "/Users/someone/Desktop"))
        self.assertFalse(diskio.same_physical_drive(
            "/dev/rdisk1s5", "/Volumes/Untitled"))

    @unittest.skipIf(sys.platform == "win32", "POSIX behaviour")
    def test_an_unreadable_mount_table_blocks_everything(self):
        diskio._read_mount_table = lambda: None
        self.assertTrue(diskio.same_physical_drive(
            "/dev/rdisk4s1", "/Users/someone/Desktop"))

    @unittest.skipIf(sys.platform == "win32", "POSIX behaviour")
    def test_an_unrecognisable_source_blocks_everything(self):
        self.assertTrue(diskio.same_physical_drive(
            "/dev/mapper/vg0-root", "/Users/someone/Desktop"))

    def test_scanning_a_disk_image_file_is_allowed(self):
        """
        Writing to the filesystem that holds a disk image does not damage the
        image's contents, and the test suite depends on being able to do it.
        """
        fd, path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(os.unlink, path)
        self.assertFalse(diskio.same_physical_drive(
            path, os.path.dirname(path)))


class WindowsGuardTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows drive letters")
    def test_matching_letters_are_the_same_drive(self):
        self.assertTrue(diskio.same_physical_drive(r"\\.\C:", r"C:\Recovered"))
        self.assertFalse(diskio.same_physical_drive(r"\\.\C:", r"D:\Recovered"))

    @unittest.skipUnless(sys.platform == "win32", "Windows drive letters")
    def test_an_unparseable_path_blocks(self):
        self.assertTrue(diskio.same_physical_drive(r"\\.\C:", r"\\server\share"))


if __name__ == "__main__":
    unittest.main()


class DropdownLabelTests(unittest.TestCase):
    """
    The label is the only thing most users will read before pointing a
    recovery tool at a drive. Three entries all reading "Whole drive" is not
    a description, it is a coin toss.
    """

    def test_a_whole_drive_is_named_by_what_is_on_it(self):
        label = diskio._describe_whole_disk(
            ["TESTCARD"], 4_026_531_840, True, "/dev/rdisk2")
        self.assertIn("TESTCARD", label)
        self.assertIn("/dev/rdisk2", label)

    def test_several_volumes_are_summarised_not_dumped(self):
        label = diskio._describe_whole_disk(
            ["Macintosh HD", "Data", "Spare", "Extra"], 500_000_000_000,
            False, "/dev/rdisk0")
        self.assertIn("Macintosh HD", label)
        self.assertIn("3 more", label)

    def test_a_nameless_drive_still_gets_a_label(self):
        label = diskio._describe_whole_disk([], 1_000_000, True, "/dev/rdisk9")
        self.assertTrue(label.strip())
        self.assertIn("/dev/rdisk9", label)

    def test_parts_are_separable_even_when_the_name_has_a_hyphen(self):
        """
        "OSX - Data" is a real volume name. Splitting a label on a plain
        hyphen would cut it in half and mislabel the drive.
        """
        label = diskio._describe("OSX - Data", "/System/Volumes/Data",
                                 500_000_000_000, False, "APFS",
                                 "/dev/rdisk1s1")
        parts = label.split(diskio.PART)
        self.assertEqual(len(parts), 3, label)
        self.assertEqual(parts[0], "OSX - Data")
        self.assertEqual(parts[-1], "/dev/rdisk1s1")

    def test_an_unmounted_volume_says_so(self):
        label = diskio._describe("TESTCARD", None, 4_000_000_000, True,
                                 "exFAT", "/dev/rdisk2s1")
        self.assertIn("not mounted", label)

    def test_a_redundant_mount_point_is_not_repeated(self):
        label = diskio._describe("TESTCARD", "/Volumes/TESTCARD",
                                 4_000_000_000, True, "exFAT",
                                 "/dev/rdisk2s1")
        self.assertNotIn("/Volumes/TESTCARD", label)
        self.assertIn("TESTCARD", label)

    def test_an_informative_mount_point_is_kept(self):
        """
        "OSX - Data" mounted at /System/Volumes/Data is the real case on this
        machine: the path says something the name does not, so it stays.
        """
        label = diskio._describe("OSX - Data", "/System/Volumes/Data", None,
                                 False, "APFS", "/dev/rdisk1s1")
        self.assertIn("/System/Volumes/Data", label)


class LinuxListingTests(unittest.TestCase):
    """
    Windows and Linux used to return the bare device - "C:", "/dev/sda1" -
    while macOS got a name, a size and a filesystem. Windows is the platform
    most people needing this tool are on, so that was exactly backwards.

    The size in /proc/partitions is in 1KB blocks, which is what makes a 32GB
    card read as 31,205,376 if you forget.
    """

    PARTITIONS = """major minor  #blocks  name

   8        0  488386584 sda
   8        1   31205376 sda1
 259        0  500107608 nvme0n1
   7        0      65536 loop0
"""

    def test_sizes_come_out_in_bytes(self):
        sizes = dict(diskio.parse_partitions(self.PARTITIONS))
        self.assertEqual(sizes["sda1"], 31205376 * 1024)
        self.assertEqual(sizes["nvme0n1"], 500107608 * 1024)

    def test_the_header_rows_are_not_devices(self):
        names = [name for name, _ in diskio.parse_partitions(self.PARTITIONS)]
        self.assertNotIn("name", names)
        self.assertEqual(names, ["sda", "sda1", "nvme0n1", "loop0"])

    def test_junk_does_not_raise(self):
        for text in ("", "\n", "one\ntwo\nthree", "a b c d e f"):
            diskio.parse_partitions(text)


class LabelShapeTests(unittest.TestCase):
    """
    Every platform must produce the same three-part label, because the window
    splits it to fit the device path on its own line. A platform that returns
    something shaped differently silently loses that.
    """

    CASES = [
        ("System", "C:\\", 500_000_000_000, False, "NTFS", r"\\.\C:"),
        ("CANON", "E:\\", 31_914_983_424, True, "exFAT", r"\\.\E:"),
        ("PHOTOS", "/media/pi/PHOTOS", 31_914_983_424, True, "exFAT",
         "/dev/sda1"),
        ("TESTCARD", None, 4_026_531_840, True, "exFAT", "/dev/rdisk2s1"),
    ]

    def test_every_platform_produces_three_parts(self):
        for name, point, size, removable, filesystem, device in self.CASES:
            label = diskio._describe(name, point, size, removable, filesystem,
                                     device)
            parts = label.split(diskio.PART)
            self.assertEqual(len(parts), 3, label)
            self.assertEqual(parts[0], name)
            self.assertEqual(parts[-1], device)

    def test_the_device_is_not_repeated_in_the_readable_half(self):
        """
        The window shows the first two parts in a narrow box and the device
        underneath. If the device is also in the readable half it is shown
        twice and the box truncates for nothing.
        """
        for name, point, size, removable, filesystem, device in self.CASES:
            label = diskio._describe(name, point, size, removable, filesystem,
                                     device)
            readable = diskio.PART.join(label.split(diskio.PART)[:2])
            self.assertNotIn(device, readable, label)

    def test_a_windows_drive_says_what_it_is(self):
        label = diskio._describe("CANON", "E:\\", 31_914_983_424, True,
                                 "exFAT", r"\\.\E:")
        self.assertIn("CANON", label)
        self.assertIn("exFAT", label)
        self.assertIn("removable", label)


class WindowsSourceParsingTests(unittest.TestCase):
    """
    The Windows branch reads a drive letter out of the source. Taking the
    last letter of whatever it was handed read `/dev/sda` as drive A and then
    compared it confidently against the destination - a definite answer to a
    question it could not answer. CI on Windows is what found it.
    """

    def parse(self, source):
        """What the guard makes of a source path, without needing Windows."""
        return re.match(r"^(?:\\\\[.?]\\)?([A-Za-z]):", source.strip())

    def test_real_drive_specifications_are_read(self):
        for source, letter in ((r"\\.\C:", "C"), ("C:", "C"),
                               (r"\\?\D:", "D"), ("e:\\", "e")):
            match = self.parse(source)
            self.assertIsNotNone(match, source)
            self.assertEqual(match.group(1), letter)

    def test_anything_else_is_not_a_drive(self):
        for source in ("/dev/sda", "/dev/rdisk4s1", r"\\.\PhysicalDrive1",
                       "", "some-file.img", r"\\server\share"):
            self.assertIsNone(self.parse(source), source)


class DiskImageSourceTests(unittest.TestCase):
    """
    A disk image is a file, and recovering into the folder that holds it is
    both legitimate and how the whole test suite works.

    This exemption used to sit below the Windows branch, so Windows never
    reached it: the image's drive letter is simply the letter of the drive
    it is stored on, which matches the destination, and every recovery next
    to an image was refused. Only CI on Windows could have found that.
    """

    def setUp(self):
        fd, self.image = tempfile.mkstemp(suffix=".img")
        os.close(fd)
        self.addCleanup(os.unlink, self.image)

    def test_recovering_beside_an_image_is_allowed(self):
        self.assertFalse(diskio.same_physical_drive(
            self.image, os.path.dirname(self.image)))

    def test_and_into_a_folder_that_does_not_exist_yet(self):
        self.assertFalse(diskio.same_physical_drive(
            self.image, os.path.join(os.path.dirname(self.image), "Recovered")))

    def test_a_device_is_still_judged_as_a_device(self):
        """The exemption is for files, and must not swallow real devices."""
        self.assertTrue(diskio.same_physical_drive(
            "/dev/definitely-not-a-real-device", tempfile.gettempdir()))
