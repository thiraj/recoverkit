"""
Tests for the FAT32 undelete engine.

FAT32 is what nearly every SD card of 32GB or less is formatted with, and
what a great many cameras write regardless of size. It is also the format
that loses the most on a delete, and the tests here are mostly about being
straight with the user about that:

  * the first letter of an 8.3 name is destroyed, not recoverable
  * the cluster chain is wiped, so anything past the first fragment is a guess
  * a long name survives, and is the reason most files still come back named
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fat32
import signatures
from tests import fat32_image
from tests.support import ImageTestCase, md5, md5_file, sample_jpeg


def sample_card():
    """A camera card: photos in DCIM, some deleted, one still there."""
    image = fat32_image.Fat32Image()
    dcim = image.add_dir("DCIM")
    canon = image.add_dir("100CANON", parent=dcim)

    originals = {
        "IMG_0042.JPG": sample_jpeg(b"P" * 40_000),
        "IMG_0043.JPG": sample_jpeg(b"Q" * 8_000),
        "notes.txt": b"a short note",
        "clip.mp4": b"\x00\x00\x00\x20ftypisom" + os.urandom(50_000),
        "keepme.jpg": sample_jpeg(b"R" * 5_000),
    }
    image.add_file("IMG_0042.JPG", originals["IMG_0042.JPG"], parent=canon,
                   deleted=True,
                   created=datetime.datetime(2023, 7, 14, 10, 15, 30),
                   modified=datetime.datetime(2023, 8, 1, 16, 44, 0))
    image.add_file("IMG_0043.JPG", originals["IMG_0043.JPG"], parent=canon,
                   deleted=True)
    image.add_file("notes.txt", originals["notes.txt"], deleted=True)
    image.add_file("clip.mp4", originals["clip.mp4"], parent=dcim,
                   deleted=True, fragments=4)
    image.add_file("keepme.jpg", originals["keepme.jpg"], deleted=False)
    return image.build(), originals


class Fat32ScanTests(ImageTestCase):
    def setUp(self):
        data, self.originals = sample_card()
        self.use_image(data, suffix=".fat32")
        self.volume = fat32.Fat32Volume(self.disk())
        self.found = {f.name: f for f in self.volume.scan()}

    # -- the two assertions that matter -----------------------------------
    def test_a_deleted_photo_comes_back_byte_identical(self):
        for name in ("IMG_0042.JPG", "IMG_0043.JPG", "notes.txt"):
            self.assertIn(name, self.found, f"{name} was not found")
            self.assertEqual(md5(self.volume.read_file(self.found[name])),
                             md5(self.originals[name]), name)

    def test_the_card_is_unchanged_by_a_scan_and_recovery(self):
        before = md5_file(self.image_path)
        for f in self.volume.scan():
            self.volume.read_file(f)
        self.assertEqual(md5_file(self.image_path), before,
                         "SOURCE IMAGE MODIFIED")

    # -- names --------------------------------------------------------------
    def test_the_long_name_is_what_survives(self):
        """
        The 8.3 entry loses its first character to the deletion marker. The
        long-name entries lose their first byte too, but that byte is a
        sequence number rather than a letter, so the name itself is intact.
        """
        found = self.found["IMG_0042.JPG"]
        self.assertEqual(found.name, "IMG_0042.JPG")
        self.assertFalse(found.first_letter_lost)

    def test_folders_are_rebuilt(self):
        self.assertEqual(self.found["IMG_0042.JPG"].path, "DCIM\\100CANON")
        self.assertEqual(self.found["clip.mp4"].path, "DCIM")
        self.assertEqual(self.found["notes.txt"].path, "\\")

    def test_the_dot_entries_are_not_files(self):
        for name in self.found:
            self.assertNotIn(name, (".", "..", "_", "_."))

    def test_the_volume_label_is_not_a_file(self):
        self.assertNotIn("TESTCARD", self.found)

    # -- live files ---------------------------------------------------------
    def test_live_files_are_never_listed(self):
        self.assertNotIn("keepme.jpg", self.found,
                         "a file still on the card was offered for recovery")

    # -- honesty about the chain -------------------------------------------
    def test_a_file_in_one_cluster_involves_no_guesswork(self):
        """
        There is nothing to assume about a file that fits in a single
        cluster: the starting cluster is the whole of it.
        """
        found = self.found["notes.txt"]
        self.assertFalse(found.assumed_contiguous)
        self.assertEqual(found.chance, 100)

    def test_a_longer_file_admits_the_layout_is_assumed(self):
        """
        A delete zeroes every link in the chain, so where the file continued
        is genuinely unknown. Recovering it contiguously is a guess and must
        not be scored as a certainty.
        """
        found = self.found["IMG_0042.JPG"]
        self.assertTrue(found.assumed_contiguous)
        self.assertLessEqual(found.chance, 50)

    def test_timestamps_survive(self):
        found = self.found["IMG_0042.JPG"]
        self.assertEqual(found.created_at,
                         datetime.datetime(2023, 7, 14, 10, 15, 30))
        self.assertEqual(found.deleted_at,
                         datetime.datetime(2023, 8, 1, 16, 44, 0))

    def test_sizes_survive(self):
        for name, data in self.originals.items():
            if name in self.found:
                self.assertEqual(self.found[name].size, len(data), name)


class ShortNameTests(ImageTestCase):
    """
    A file with no long name loses its first letter for good. Anyone showing
    you a complete short name for a deleted FAT file has guessed at it.
    """

    def setUp(self):
        image = fat32_image.Fat32Image()
        self.payload = sample_jpeg(b"S" * 3000)
        image.add_file("PHOTO.JPG", self.payload, deleted=True)
        self.use_image(image.build(), suffix=".fat32")
        self.volume = fat32.Fat32Volume(self.disk())

    def test_the_name_is_still_recovered_when_a_long_name_exists(self):
        found = {f.name: f for f in self.volume.scan()}
        self.assertIn("PHOTO.JPG", found)

    def test_a_missing_first_letter_is_marked_not_invented(self):
        entry = bytearray(b" " * 32)
        entry[0:11] = b"\xE5HOTO   JPG"
        self.assertEqual(fat32._short_name(entry), "_HOTO.JPG")
        self.assertNotIn("P", fat32._short_name(entry)[:1])


class ContentCheckTests(ImageTestCase):
    def setUp(self):
        image = fat32_image.Fat32Image()
        self.good = sample_jpeg(b"G" * 20_000)
        image.add_file("good.jpg", self.good, deleted=True)
        image.add_file("reused.jpg", bytes(range(256)) * 100, deleted=True)
        self.use_image(image.build(), suffix=".fat32")
        self.found = {f.name: f for f in
                      fat32.Fat32Volume(self.disk()).scan()}

    def test_a_file_that_still_looks_right_keeps_its_score(self):
        self.assertEqual(self.found["good.jpg"].content_check,
                         signatures.MATCH)

    def test_a_file_whose_space_was_reused_is_scored_zero(self):
        found = self.found["reused.jpg"]
        self.assertEqual(found.content_check, signatures.MISMATCH)
        self.assertEqual(found.chance, 0)


class RejectionTests(ImageTestCase):
    def test_a_non_fat_volume_is_rejected_in_plain_language(self):
        self.use_image(b"\x00" * (1024 * 1024))
        with self.open_disk() as disk:
            with self.assertRaises(ValueError) as caught:
                fat32.Fat32Volume(disk)
        message = str(caught.exception)
        self.assertIn("not FAT32", message)
        self.assertNotIn("0x", message)

    def test_an_ntfs_volume_is_not_mistaken_for_fat32(self):
        from tests.ntfs_image import NtfsImage
        image = NtfsImage()
        image.add_file("x.txt", b"hello", resident=True)
        self.use_image(image.build())
        with self.open_disk() as disk:
            with self.assertRaises(ValueError):
                fat32.Fat32Volume(disk)

    def test_an_exfat_volume_is_not_mistaken_for_fat32(self):
        from tests.exfat_image import ExfatImage
        image = ExfatImage()
        image.add_file("x.txt", b"hello")
        self.use_image(image.build())
        with self.open_disk() as disk:
            with self.assertRaises(ValueError):
                fat32.Fat32Volume(disk)


class TimestampTests(unittest.TestCase):
    def test_round_trip(self):
        for when in (datetime.datetime(1980, 1, 1, 0, 0, 0),
                     datetime.datetime(2024, 6, 2, 15, 40, 0),
                     datetime.datetime(2099, 12, 31, 23, 59, 58)):
            self.assertEqual(
                fat32._fat_time(fat32_image.to_fat_date(when),
                                fat32_image.to_fat_time(when)), when)

    def test_nonsense_reports_nothing_rather_than_guessing(self):
        self.assertIsNone(fat32._fat_time(0, 0))
        self.assertIsNone(fat32._fat_time(0xFFFF, 0xFFFF))


if __name__ == "__main__":
    unittest.main()
