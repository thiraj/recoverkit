"""
Tests for the "does this still look like the file it claims to be" check.

This exists because of a real recovery that went wrong. A 211 MB .mp4 on a USB
stick was listed at a confident 100% and recovered without complaint. It would
not play: the bytes were high-entropy noise with no MP4 structure anywhere in
them. The file table was read correctly - the cluster map was complete and
self-consistent - and the allocation bitmap honestly reported those clusters
as free. The space had simply been used and released again in the years since
the file was deleted, and nothing in the tool had looked at the actual bytes.

A bitmap says whether space is spoken for *today*. It cannot say whether the
data is still yours. The header can, and a mismatch outranks the bitmap.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import exfat
import ntfs
import signatures
from tests import exfat_image, ntfs_image
from tests.support import ImageTestCase

JPEG = b"\xFF\xD8\xFF\xE0"
MP4 = b"\x00\x00\x00\x20ftypisom"
NOISE = bytes(range(256)) * 40          # not a header for anything


class SignatureTableTests(unittest.TestCase):
    def test_recognises_correct_headers(self):
        for extension, head in (("jpg", JPEG), ("png", b"\x89PNG\r\n\x1a\n"),
                                ("pdf", b"%PDF-1.7"), ("mp4", MP4),
                                ("mp3", b"ID3\x03"), ("docx", b"PK\x03\x04"),
                                ("gif", b"GIF89a"), ("zip", b"PK\x03\x04")):
            self.assertEqual(signatures.check(extension, head + NOISE),
                             signatures.MATCH, extension)

    def test_catches_the_wrong_content(self):
        self.assertEqual(signatures.check("mp4", NOISE), signatures.MISMATCH)
        self.assertEqual(signatures.check("jpg", b"%PDF-1.7" + NOISE),
                         signatures.MISMATCH)

    def test_an_mp4_header_is_matched_at_its_real_offset(self):
        """
        `ftyp` sits after a four-byte box length that varies by encoder, so
        matching it at a fixed whole-file offset would reject valid files.
        """
        for length in (b"\x00\x00\x00\x18", b"\x00\x00\x00\x20",
                       b"\x00\x00\x00\x1C"):
            self.assertEqual(
                signatures.check("mp4", length + b"ftypmp42" + NOISE),
                signatures.MATCH)

    def test_an_mp3_may_start_with_a_tag_or_a_frame(self):
        self.assertEqual(signatures.check("mp3", b"ID3\x04\x00" + NOISE),
                         signatures.MATCH)
        self.assertEqual(signatures.check("mp3", b"\xFF\xFB\x90" + NOISE),
                         signatures.MATCH)

    def test_blank_space_is_reported_as_blank_not_as_a_mismatch(self):
        self.assertEqual(signatures.check("jpg", bytes(512)), signatures.BLANK)

    def test_unknown_extensions_get_no_opinion(self):
        """
        Silence is the honest answer for a format we do not know. Guessing
        would knock the score off perfectly recoverable files.
        """
        for extension in ("dat", "bin", "raw", "", None, "xyz"):
            self.assertEqual(signatures.check(extension, NOISE),
                             signatures.UNKNOWN, repr(extension))

    def test_no_data_gets_no_opinion(self):
        self.assertEqual(signatures.check("jpg", b""), signatures.UNKNOWN)

    def test_every_explanation_is_plain_english(self):
        for verdict in (signatures.MATCH, signatures.MISMATCH,
                        signatures.BLANK):
            text = signatures.explain(verdict, "jpg")
            self.assertTrue(text)
            for jargon in ("0x", "cluster", "bitmap", "magic", "offset",
                           "byte", "header"):
                self.assertNotIn(jargon, text.lower(), f"{verdict}: {text}")


class NtfsContentCheckTests(ImageTestCase):
    """
    The exact shape of the failure: the bitmap says the space is free, and it
    genuinely is - but the bytes there belong to nobody.
    """

    def setUp(self):
        image = ntfs_image.NtfsImage()
        self.good = JPEG + os.urandom(20_000)
        self.stale = NOISE * 60                    # no JPEG header at all

        image.add_file("intact.jpg", self.good, deleted=True)
        image.add_file("reused.jpg", self.stale, deleted=True)
        image.add_file("mystery.dat", NOISE * 60, deleted=True)
        image.add_file("empty_space.jpg", bytes(30_000), deleted=True)
        self.use_image(image.build(), suffix=".ntfs")

        volume = ntfs.NtfsVolume(self.disk())
        self.found = {f.name: f for f in volume.scan()}

    def test_a_file_that_still_looks_right_keeps_its_score(self):
        target = self.found["intact.jpg"]
        self.assertEqual(target.content_check, signatures.MATCH)
        self.assertGreaterEqual(target.chance, 90)

    def test_a_file_whose_space_was_reused_is_scored_zero(self):
        """
        The bitmap says these clusters are free, so the old scorer gave this
        file 100%. The bytes say otherwise, and the bytes win.
        """
        target = self.found["reused.jpg"]
        self.assertEqual(target.content_check, signatures.MISMATCH)
        self.assertEqual(target.chance, 0,
                         "a file whose content is plainly gone must not be "
                         "presented as recoverable")

    def test_blank_space_is_scored_zero_and_named_as_blank(self):
        target = self.found["empty_space.jpg"]
        self.assertEqual(target.content_check, signatures.BLANK)
        self.assertEqual(target.chance, 0)

    def test_an_unknown_extension_is_left_alone(self):
        """We cannot judge a .dat, so we must not pretend to."""
        target = self.found["mystery.dat"]
        self.assertEqual(target.content_check, signatures.UNKNOWN)
        self.assertGreaterEqual(target.chance, 90)

    def test_the_check_does_not_write_to_the_source(self):
        pass          # tearDown re-checks the image hash


class ExfatContentCheckTests(ImageTestCase):
    def setUp(self):
        image = exfat_image.ExfatImage()
        self.good = MP4 + os.urandom(20_000)
        image.add_file("holiday.mp4", self.good, deleted=True)
        image.add_file("reused.mp4", NOISE * 60, deleted=True)
        image.add_file("notes.dat", NOISE * 60, deleted=True)
        self.use_image(image.build(), suffix=".exfat")

        volume = exfat.ExfatVolume(self.disk())
        self.found = {f.name: f for f in volume.scan()}

    def test_a_file_that_still_looks_right_keeps_its_score(self):
        target = self.found["holiday.mp4"]
        self.assertEqual(target.content_check, signatures.MATCH)
        self.assertGreaterEqual(target.chance, 90)

    def test_a_file_whose_space_was_reused_is_scored_zero(self):
        target = self.found["reused.mp4"]
        self.assertEqual(target.content_check, signatures.MISMATCH)
        self.assertEqual(target.chance, 0)

    def test_an_unknown_extension_is_left_alone(self):
        self.assertEqual(self.found["notes.dat"].content_check,
                         signatures.UNKNOWN)


class CostTests(ImageTestCase):
    """
    The check must not turn a scan into a crawl. Files we cannot judge cost
    no read at all - the extension is inspected before the disk is touched.
    """

    def setUp(self):
        image = ntfs_image.NtfsImage(record_count=128)
        for i in range(20):
            image.add_file(f"file{i}.dat", os.urandom(4_000), deleted=True)
        self.use_image(image.build(), suffix=".ntfs")

    def test_unjudgeable_files_cost_no_extra_reads(self):
        disk = self.disk()
        reads = []
        original = disk.read
        disk.read = lambda off, length: (reads.append(1), original(off, length))[1]

        volume = ntfs.NtfsVolume(disk)
        before = len(reads)
        found = volume.scan()
        during = len(reads) - before

        self.assertEqual(len([f for f in found if f.name.endswith(".dat")]), 20)
        # One read per MFT record plus bootstrap; nothing per .dat file.
        self.assertLess(during, volume.record_count + 40,
                        "the content check is reading files it cannot judge")


if __name__ == "__main__":
    unittest.main()


class MovedNotDeletedTests(ImageTestCase):
    """
    The case that showed up on real hardware: a file dragged to the Trash.

    Moving a file to the Trash rewrites its directory entry - the old one is
    marked deleted and a new one is created pointing at the same clusters. So
    the scanner sees a deleted file whose space the bitmap correctly reports
    as in use, and scored it 0%. The data was completely intact and recovered
    byte-perfect; the score was the only thing wrong.

    Nobody should be told a file is unrecoverable when it is sitting in their
    Trash. They should be told where it is.
    """

    JPEG = b"\xFF\xD8\xFF\xE0"

    def setUp(self):
        self.payload = self.JPEG + os.urandom(30_000)

        image = exfat_image.ExfatImage()
        trash = image.add_dir("Trashes")
        # The live copy, now sitting in the Trash...
        runs = image.add_file("holiday.jpg", self.payload, parent=trash,
                              deleted=False)
        # ...and the entry it was moved from, still in the folder it used to
        # be in, marked deleted and pointing at those very same clusters.
        image.add_file("holiday.jpg", self.payload, deleted=True,
                       reuse_runs=runs)

        self.use_image(image.build(), suffix=".exfat")
        volume = exfat.ExfatVolume(self.disk())
        self.volume = volume
        self.found = {(f.path, f.name): f for f in volume.scan()}

    def test_the_file_is_not_written_off_as_unrecoverable(self):
        target = self.found[("\\", "holiday.jpg")]
        self.assertNotEqual(
            target.chance, 0,
            "a file sitting in the Trash was reported as unrecoverable")

    def test_it_is_reported_as_moved_and_says_where(self):
        target = self.found[("\\", "holiday.jpg")]
        self.assertEqual(target.content_check, signatures.MOVED)
        self.assertIn("Trashes", target.still_at or "")

    def test_the_data_really_is_intact(self):
        target = self.found[("\\", "holiday.jpg")]
        self.assertEqual(self.volume.read_file(target), self.payload)

    def test_the_explanation_tells_the_user_to_just_take_it_back(self):
        text = signatures.explain(signatures.MOVED, "jpg",
                                  still_at="\\Trashes\\holiday.jpg")
        self.assertIn("Trash", text)
        self.assertIn("Trashes\\holiday.jpg", text)


class ContestedSpaceTests(ImageTestCase):
    """
    Space taken by something else, but the bytes still look like the right
    kind of file. Neither 0% nor 100% is honest - we say so and score it in
    the middle rather than picking a confident answer at random.
    """

    def setUp(self):
        image = ntfs_image.NtfsImage()
        image.add_file("photo.jpg", b"\xFF\xD8\xFF\xE0" + os.urandom(20_000),
                       deleted=True, overwritten=True)
        self.use_image(image.build(), suffix=".ntfs")
        self.found = {f.name: f for f in
                      ntfs.NtfsVolume(self.disk()).scan()}

    def test_it_is_neither_written_off_nor_promised(self):
        target = self.found["photo.jpg"]
        self.assertEqual(target.content_check, signatures.IN_USE)
        self.assertGreater(target.chance, 0)
        self.assertLess(target.chance, 100)

    def test_the_explanation_admits_the_doubt(self):
        text = signatures.explain(signatures.IN_USE, "jpg")
        self.assertTrue(text)
        self.assertIn("may", text.lower())
