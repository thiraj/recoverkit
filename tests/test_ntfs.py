"""
Tests for the NTFS undelete engine.

The two assertions CLAUDE.md insists on are covered here, on a volume built
by tests/ntfs_image.py:

  1. Recovered files are byte-identical to the originals.
  2. The source image checksum is identical before and after a scan.

Assertion 2 is enforced for every test in the file, not just its own test:
ImageTestCase hashes the image in setUp and re-checks it in tearDown.
"""

import datetime
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ntfs
from tests import ntfs_image
from tests.support import ImageTestCase, md5, md5_file


def sample_volume():
    """
    A volume covering the cases that actually turn up in recovery:
    nested folders, a plain deleted file, a badly fragmented one, a tiny file
    stored inside its own MFT record, a file whose space has been handed to
    something else, and a live file that must never be touched.
    """
    image = ntfs_image.NtfsImage()
    documents = image.add_dir("Documents")
    reports = image.add_dir("Reports", parent=documents)

    # Payloads start with the real magic bytes for their extension: the
    # engine now checks that a recovered .jpg still looks like a .jpg, so a
    # fixture full of arbitrary bytes would be correctly scored as gone.
    originals = {
        "holiday.jpg": b"\xFF\xD8\xFF\xE0" + bytes(range(256)) * 300,
        "quarterly.xlsx": b"PK\x03\x04" + os.urandom(60_000),
        "notes.txt": b"a short note that fits inside the record",
        "keepme.txt": b"still in use, hands off" * 100,
        "overwritten.dat": os.urandom(9_000),
    }

    image.add_file("holiday.jpg", originals["holiday.jpg"],
                   parent=documents, deleted=True,
                   created=datetime.datetime(2023, 7, 14, 10, 15, 30),
                   modified=datetime.datetime(2023, 8, 1, 16, 45, 0))
    image.add_file("quarterly.xlsx", originals["quarterly.xlsx"],
                   parent=reports, deleted=True, fragments=5)
    image.add_file("notes.txt", originals["notes.txt"],
                   deleted=True, resident=True)
    image.add_file("keepme.txt", originals["keepme.txt"], deleted=False)
    image.add_file("overwritten.dat", originals["overwritten.dat"],
                   deleted=True, overwritten=True)

    return image.build(), originals


class NtfsScanTests(ImageTestCase):
    def setUp(self):
        data, self.originals = sample_volume()
        self.use_image(data, suffix=".ntfs")

    def scan(self, **kw):
        volume = ntfs.NtfsVolume(self.disk())
        return volume, volume.scan(**kw)

    # -- assertion 1: byte-identical recovery -------------------------------
    def test_recovered_files_are_byte_identical(self):
        volume, found = self.scan()
        by_name = {f.name: f for f in found}

        for name in ("holiday.jpg", "quarterly.xlsx", "notes.txt"):
            self.assertIn(name, by_name, f"{name} was not found by the scan")
            recovered = volume.read_file(by_name[name])
            original = self.originals[name]
            self.assertEqual(len(recovered), len(original),
                             f"{name} came back the wrong length")
            self.assertEqual(md5(recovered), md5(original),
                             f"{name} is not byte-identical to the original")

    def test_a_fragmented_file_is_stitched_back_together(self):
        """
        The whole point of reading the MFT rather than carving: a file split
        across five separate runs still comes back whole and in order.
        """
        volume, found = self.scan()
        target = next(f for f in found if f.name == "quarterly.xlsx")
        self.assertGreater(len(target.runs), 1, "the test file is not split")
        self.assertEqual(volume.read_file(target),
                         self.originals["quarterly.xlsx"])

    def test_a_resident_file_is_read_out_of_its_record(self):
        volume, found = self.scan()
        target = next(f for f in found if f.name == "notes.txt")
        self.assertIsNotNone(target.resident,
                             "the small file should be stored in its record")
        self.assertEqual(target.runs, [])
        self.assertEqual(volume.read_file(target), self.originals["notes.txt"])

    # -- assertion 2: the source is never written to ------------------------
    def test_source_image_is_unchanged_by_a_full_scan_and_recovery(self):
        """
        The explicit form of the check CLAUDE.md describes. Every other test
        in this file gets the same guarantee from tearDown, but this one
        states it outright, including the recovery step.
        """
        before = md5_file(self.image_path)

        volume, found = self.scan()
        for f in found:
            volume.read_file(f)

        after = md5_file(self.image_path)
        self.assertEqual(before, after, "SOURCE IMAGE MODIFIED")

    # -- honesty about what survived ----------------------------------------
    def test_a_file_whose_space_was_reused_scores_zero(self):
        _, found = self.scan()
        target = next(f for f in found if f.name == "overwritten.dat")
        self.assertEqual(target.chance, 0,
                         "a file sitting under allocated clusters must not be "
                         "presented as recoverable")

    def test_an_untouched_file_scores_high(self):
        _, found = self.scan()
        target = next(f for f in found if f.name == "holiday.jpg")
        self.assertGreaterEqual(target.chance, 90)

    # -- live files ---------------------------------------------------------
    def test_live_files_are_never_listed(self):
        _, found = self.scan()
        names = [f.name for f in found]
        self.assertNotIn("keepme.txt", names,
                         "a file still in use was offered for recovery")

    def test_metadata_files_are_not_listed(self):
        _, found = self.scan()
        for name in [f.name for f in found]:
            self.assertFalse(name.startswith("$"),
                             f"internal metadata file {name} was listed")

    # -- names, paths, timestamps -------------------------------------------
    def test_original_names_and_folders_are_recovered(self):
        _, found = self.scan()
        by_name = {f.name: f for f in found}
        self.assertEqual(by_name["holiday.jpg"].path, "Documents")
        self.assertEqual(by_name["quarterly.xlsx"].path,
                         "Documents\\Reports")
        self.assertEqual(by_name["notes.txt"].path, "\\")

    def test_timestamps_are_recovered(self):
        _, found = self.scan()
        target = next(f for f in found if f.name == "holiday.jpg")
        self.assertEqual(target.created_at,
                         datetime.datetime(2023, 7, 14, 10, 15, 30))
        self.assertEqual(target.deleted_at,
                         datetime.datetime(2023, 8, 1, 16, 45, 0))

    def test_sizes_are_recovered(self):
        _, found = self.scan()
        for f in found:
            if f.name in self.originals:
                self.assertEqual(f.size, len(self.originals[f.name]),
                                 f"{f.name} reported the wrong size")

    def test_extension_helper(self):
        _, found = self.scan()
        by_name = {f.name: f for f in found}
        self.assertEqual(by_name["holiday.jpg"].extension, "jpg")
        self.assertEqual(by_name["quarterly.xlsx"].extension, "xlsx")

    # -- scan plumbing ------------------------------------------------------
    def test_progress_is_reported(self):
        seen = []
        self.scan(progress=lambda done, total: seen.append((done, total)))
        self.assertTrue(seen, "progress was never reported")
        self.assertEqual(seen[-1][0], seen[-1][1],
                         "the final progress call should show completion")

    def test_stop_is_honoured(self):
        _, found = self.scan(should_stop=lambda: True)
        self.assertEqual(found, [])

    def test_include_dirs_lists_deleted_folders_too(self):
        _, without = self.scan()
        _, with_dirs = self.scan(include_dirs=True)
        self.assertGreaterEqual(len(with_dirs), len(without))


class NtfsRejectionTests(ImageTestCase):
    def test_a_non_ntfs_volume_is_rejected_in_plain_language(self):
        self.use_image(b"\x00" * (1024 * 1024))
        with self.open_disk() as disk:
            with self.assertRaises(ValueError) as caught:
                ntfs.NtfsVolume(disk)
        message = str(caught.exception)
        self.assertIn("not NTFS", message)
        # The target user is not technical: no hex, no struct names.
        self.assertNotIn("0x", message)

    def test_a_damaged_mft_is_reported_rather_than_crashing(self):
        data, _ = sample_volume()
        broken = bytearray(data)
        image = ntfs_image.NtfsImage()
        mft_at = image.mft_cluster * image.cluster_size
        broken[mft_at:mft_at + 4] = b"XXXX"          # destroy the MFT header
        self.use_image(bytes(broken))
        with self.open_disk() as disk:
            volume = ntfs.NtfsVolume(disk)
            with self.assertRaises(ValueError) as caught:
                volume.scan()
        self.assertIn("MFT", str(caught.exception))


class RunListTests(unittest.TestCase):
    """
    Data runs are the fiddliest part of the format: a packed header byte, an
    unsigned length, and a *signed* offset relative to the previous run. A
    round trip through the builder and the parser keeps both honest.
    """

    def test_round_trip(self):
        for runs in ([(100, 4)],
                     [(100, 4), (200, 8), (150, 2)],       # backwards jump
                     [(5, 1)],
                     [(70_000, 300), (69_000, 1)],
                     [(1, 1), (2, 1), (3, 1)]):
            encoded = ntfs_image.encode_runs(runs)
            self.assertEqual(ntfs._parse_runs(encoded, 0), runs,
                             f"run list {runs} did not survive a round trip")

    def test_sparse_runs_decode_as_holes(self):
        encoded = ntfs_image.encode_runs([(10, 2), (None, 3), (20, 1)])
        self.assertEqual(ntfs._parse_runs(encoded, 0),
                         [(10, 2), (None, 3), (20, 1)])

    def test_a_truncated_run_list_stops_cleanly(self):
        encoded = ntfs_image.encode_runs([(100, 4), (200, 8)])
        for cut in range(1, len(encoded)):
            ntfs._parse_runs(encoded[:cut], 0)      # must not raise


class FixupTests(unittest.TestCase):
    """
    NTFS overwrites the last two bytes of every sector in an MFT record with
    an update sequence number and keeps the originals in an array. Getting
    this wrong corrupts the tail of every record, quietly.
    """

    def test_fixups_are_applied(self):
        image = ntfs_image.NtfsImage()
        image.add_file("x.txt", b"hello", resident=True)
        raw = image.build()
        record_at = image.mft_cluster * image.cluster_size
        record = raw[record_at:record_at + image.record_size]

        self.assertEqual(record[510:512], b"\x02\x01")   # the USN, pre-fixup
        fixed = ntfs._apply_fixup(record, image.sector_size)
        self.assertNotEqual(fixed[510:512], b"\x02\x01")
        self.assertEqual(fixed[:4], b"FILE")


if __name__ == "__main__":
    unittest.main()
