"""
Tests for the exFAT undelete engine.

Same two assertions as the NTFS suite - byte-identical recovery, and a source
image that never changes - plus the cases specific to exFAT: contiguous files,
files whose FAT chain survived, and files whose chain did not, where the
engine has to admit it is guessing.
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import exfat
from tests import exfat_image
from tests.support import ImageTestCase, md5, md5_file


def sample_volume():
    """A card-shaped volume: photos in nested folders, plus the edge cases."""
    image = exfat_image.ExfatImage()
    dcim = image.add_dir("DCIM")
    canon = image.add_dir("100CANON", parent=dcim)

    # Payloads start with the real magic bytes for their extension - see the
    # note in test_ntfs.sample_volume.
    originals = {
        "IMG_0042.JPG": b"\xFF\xD8\xFF\xE0" + bytes(range(256)) * 200,
        "receipt.txt": b"a short note",
        "clip.mp4": b"\x00\x00\x00\x20ftypisom" + os.urandom(50_000),
        "kept_chain.bin": os.urandom(50_000),
        "live.dat": os.urandom(3_000),
        "gone.raw": os.urandom(9_000),
        "a long name with accents éèê and spaces.txt": b"unicode body",
    }

    image.add_file("IMG_0042.JPG", originals["IMG_0042.JPG"], parent=canon,
                   deleted=True,
                   created=datetime.datetime(2023, 7, 14, 10, 15, 30),
                   modified=datetime.datetime(2023, 8, 1, 16, 44, 0))
    image.add_file("receipt.txt", originals["receipt.txt"], deleted=True)
    # Fragmented and deleted: a real driver clears the FAT chain on delete.
    image.add_file("clip.mp4", originals["clip.mp4"], parent=dcim,
                   deleted=True, fragments=4, wipe_chain=True)
    # Fragmented but with the chain still readable.
    image.add_file("kept_chain.bin", originals["kept_chain.bin"],
                   deleted=True, fragments=4, wipe_chain=False)
    image.add_file("live.dat", originals["live.dat"], deleted=False)
    image.add_file("gone.raw", originals["gone.raw"], deleted=True,
                   overwritten=True)
    image.add_file("a long name with accents éèê and spaces.txt",
                   originals["a long name with accents éèê and spaces.txt"],
                   deleted=True)

    return image.build(), originals


class ExfatScanTests(ImageTestCase):
    def setUp(self):
        data, self.originals = sample_volume()
        self.use_image(data, suffix=".exfat")

    def scan(self, **kw):
        volume = exfat.ExfatVolume(self.disk())
        return volume, volume.scan(**kw)

    # -- assertion 1: byte-identical recovery -------------------------------
    def test_recovered_files_are_byte_identical(self):
        volume, found = self.scan()
        by_name = {f.name: f for f in found}

        for name in ("IMG_0042.JPG", "receipt.txt", "kept_chain.bin"):
            self.assertIn(name, by_name, f"{name} was not found by the scan")
            recovered = volume.read_file(by_name[name])
            self.assertEqual(md5(recovered), md5(self.originals[name]),
                             f"{name} is not byte-identical to the original")

    def test_a_contiguous_file_is_recovered_whole(self):
        volume, found = self.scan()
        target = next(f for f in found if f.name == "IMG_0042.JPG")
        self.assertEqual(len(target.runs), 1, "the photo should be contiguous")
        self.assertFalse(target.assumed_contiguous)
        self.assertEqual(volume.read_file(target),
                         self.originals["IMG_0042.JPG"])

    def test_a_surviving_fat_chain_is_followed(self):
        volume, found = self.scan()
        target = next(f for f in found if f.name == "kept_chain.bin")
        self.assertGreater(len(target.runs), 1,
                           "the test file is not fragmented")
        self.assertFalse(target.assumed_contiguous,
                         "the chain survived, so nothing needed guessing")
        self.assertEqual(volume.read_file(target),
                         self.originals["kept_chain.bin"])

    # -- assertion 2: the source is never written to ------------------------
    def test_source_image_is_unchanged_by_a_full_scan_and_recovery(self):
        before = md5_file(self.image_path)

        volume, found = self.scan()
        for f in found:
            volume.read_file(f)

        self.assertEqual(md5_file(self.image_path), before,
                         "SOURCE IMAGE MODIFIED")

    # -- honesty ------------------------------------------------------------
    def test_a_lost_fat_chain_is_admitted_not_hidden(self):
        """
        exFAT drops the chain when a fragmented file is deleted. We can still
        find where the data starts, but the tail is a guess, and the score
        has to say so rather than showing a confident green 100%.
        """
        _, found = self.scan()
        target = next(f for f in found if f.name == "clip.mp4")
        self.assertTrue(target.assumed_contiguous,
                        "a wiped chain should be flagged as guesswork")
        self.assertLessEqual(target.chance, 50,
                             "a guessed layout must not be scored as certain")

    def test_a_file_whose_space_was_reused_scores_zero(self):
        _, found = self.scan()
        target = next(f for f in found if f.name == "gone.raw")
        self.assertEqual(target.chance, 0)

    def test_an_untouched_file_scores_high(self):
        _, found = self.scan()
        target = next(f for f in found if f.name == "IMG_0042.JPG")
        self.assertGreaterEqual(target.chance, 90)

    def test_the_allocation_bitmap_is_actually_loaded(self):
        """Without it every score would silently default to optimistic."""
        volume, _ = self.scan()
        self.assertIsNotNone(volume._bitmap,
                             "the allocation bitmap was not found")

    # -- live files ---------------------------------------------------------
    def test_live_files_are_never_listed(self):
        _, found = self.scan()
        self.assertNotIn("live.dat", [f.name for f in found],
                         "a file still in use was offered for recovery")

    def test_volume_metadata_entries_are_not_listed_as_files(self):
        _, found = self.scan()
        for f in found:
            self.assertTrue(f.name, "an entry with no name was listed")

    # -- names, paths, timestamps -------------------------------------------
    def test_original_names_and_folders_are_recovered(self):
        _, found = self.scan()
        by_name = {f.name: f for f in found}
        self.assertEqual(by_name["IMG_0042.JPG"].path, "DCIM\\100CANON")
        self.assertEqual(by_name["clip.mp4"].path, "DCIM")
        self.assertEqual(by_name["receipt.txt"].path, "\\")

    def test_a_long_unicode_name_spanning_several_entries(self):
        """
        Each name entry holds only 15 characters, so anything longer is split
        across entries and has to be reassembled in order.
        """
        _, found = self.scan()
        name = "a long name with accents éèê and spaces.txt"
        by_name = {f.name: f for f in found}
        self.assertIn(name, by_name)
        self.assertEqual(by_name[name].extension, "txt")

    def test_timestamps_are_recovered(self):
        _, found = self.scan()
        target = next(f for f in found if f.name == "IMG_0042.JPG")
        self.assertEqual(target.created_at,
                         datetime.datetime(2023, 7, 14, 10, 15, 30))
        self.assertEqual(target.deleted_at,
                         datetime.datetime(2023, 8, 1, 16, 44, 0))

    def test_sizes_are_recovered(self):
        _, found = self.scan()
        for f in found:
            if f.name in self.originals:
                self.assertEqual(f.size, len(self.originals[f.name]))

    # -- scan plumbing ------------------------------------------------------
    def test_progress_is_reported(self):
        seen = []
        self.scan(progress=lambda done, total: seen.append((done, total)))
        self.assertTrue(seen)
        self.assertEqual(seen[-1][0], seen[-1][1])

    def test_stop_is_honoured(self):
        _, found = self.scan(should_stop=lambda: True)
        self.assertEqual(found, [])

    def test_include_dirs_lists_folders_too(self):
        _, without = self.scan()
        _, with_dirs = self.scan(include_dirs=True)
        self.assertGreaterEqual(len(with_dirs), len(without))


class DeletedFolderTests(ImageTestCase):
    """A deleted folder's contents are usually still sitting there intact."""

    def setUp(self):
        image = exfat_image.ExfatImage()
        folder = image.add_dir("Trip", deleted=True)
        self.payload = os.urandom(20_000)
        image.add_file("inside.raw", self.payload, parent=folder, deleted=True)
        self.use_image(image.build(), suffix=".exfat")

    def test_files_inside_a_deleted_folder_are_found_and_recovered(self):
        volume = exfat.ExfatVolume(self.disk())
        found = volume.scan()
        by_name = {f.name: f for f in found}
        self.assertIn("inside.raw", by_name)
        self.assertEqual(by_name["inside.raw"].path, "Trip")
        self.assertEqual(volume.read_file(by_name["inside.raw"]), self.payload)


class GarbageEntryTests(ImageTestCase):
    """
    Directory space gets reused. Random bytes that happen to start with 0x05
    must not become a file called something arbitrary - a plausible-looking
    wrong filename is worse than no result at all.
    """

    def setUp(self):
        image = exfat_image.ExfatImage()
        self.payload = b"a real recoverable file"
        image.add_file("real.txt", self.payload, deleted=True)

        # Append an entry set with a deliberately wrong checksum.
        junk = bytearray(exfat_image.build_entry_set(
            "ghost.txt", 4096, 900, True, False, True,
            datetime.datetime(2024, 1, 1), datetime.datetime(2024, 1, 1)))
        junk[2] ^= 0xFF                       # corrupt the stored checksum
        image.dirs[image.root_cluster] += junk

        self.use_image(image.build(), suffix=".exfat")

    def test_an_entry_set_that_fails_its_checksum_is_ignored(self):
        volume = exfat.ExfatVolume(self.disk())
        names = [f.name for f in volume.scan()]
        self.assertIn("real.txt", names)
        self.assertNotIn("ghost.txt", names,
                         "a corrupt entry set was presented as a real file")


class ExfatRejectionTests(ImageTestCase):
    def test_a_non_exfat_volume_is_rejected_in_plain_language(self):
        self.use_image(b"\x00" * (1024 * 1024))
        with self.open_disk() as disk:
            with self.assertRaises(ValueError) as caught:
                exfat.ExfatVolume(disk)
        message = str(caught.exception)
        self.assertIn("not exFAT", message)
        self.assertNotIn("0x", message)

    def test_an_ntfs_volume_is_rejected(self):
        from tests.ntfs_image import NtfsImage
        image = NtfsImage()
        image.add_file("x.txt", b"hello", resident=True)
        self.use_image(image.build())
        with self.open_disk() as disk:
            with self.assertRaises(ValueError):
                exfat.ExfatVolume(disk)

    def test_a_nonsense_layout_is_rejected(self):
        image = exfat_image.ExfatImage()
        broken = bytearray(image.build())
        broken[0x6C] = 99                       # impossible sector size
        self.use_image(bytes(broken))
        with self.open_disk() as disk:
            with self.assertRaises(ValueError):
                exfat.ExfatVolume(disk)


class TimestampTests(unittest.TestCase):
    def test_round_trip(self):
        for when in (datetime.datetime(1980, 1, 1, 0, 0, 0),
                     datetime.datetime(2024, 6, 2, 15, 40, 0),
                     datetime.datetime(2099, 12, 31, 23, 59, 58)):
            packed = exfat_image.to_exfat_time(when)
            self.assertEqual(exfat._exfat_time(packed), when)

    def test_a_nonsense_timestamp_reports_nothing_rather_than_guessing(self):
        self.assertIsNone(exfat._exfat_time(0))
        self.assertIsNone(exfat._exfat_time(0xFFFFFFFF))   # month 15, day 31

    def test_the_fractional_second_byte_is_applied(self):
        packed = exfat_image.to_exfat_time(
            datetime.datetime(2024, 6, 2, 15, 40, 0))
        self.assertEqual(
            exfat._exfat_time(packed, 150),
            datetime.datetime(2024, 6, 2, 15, 40, 1, 500_000))


class ChecksumTests(unittest.TestCase):
    def test_the_engine_and_the_builder_agree(self):
        entries = exfat_image.build_entry_set(
            "photo.jpg", 1234, 40, True, False, False,
            datetime.datetime(2024, 1, 1), datetime.datetime(2024, 1, 1))
        import struct
        stored = struct.unpack_from("<H", entries, 2)[0]
        self.assertEqual(exfat._set_checksum(entries), stored)


if __name__ == "__main__":
    unittest.main()
